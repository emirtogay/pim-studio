"""Run bookkeeping — that every sync leaves an honest record of itself.

The behaviours worth pinning are the ones chosen deliberately in `runs.py`:
the row is committed before the work starts, a failure is recorded and
re-raised rather than swallowed, dropping rows downgrades success to partial,
and the watermark overlaps rather than gaps.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import SyncRun as SyncRunRow
from pim_studio.sync import last_successful_run, sync_run
from pim_studio.sync.runs import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    STATUS_SUCCESS,
)

pytestmark = pytest.mark.usefixtures("temp_db")


def rows() -> list[SyncRunRow]:
    with session_scope() as db:
        return list(db.scalars(select(SyncRunRow).order_by(SyncRunRow.id)))


def only_row() -> SyncRunRow:
    got = rows()
    assert len(got) == 1, f"expected one run row, got {len(got)}"
    return got[0]


# ── the happy path ─────────────────────────────────────────────────────────


def test_a_clean_run_is_recorded_as_success() -> None:
    with sync_run("omnitron_attributes") as run:
        run.seen(149)
        run.imported(149)

    row = only_row()
    assert row.kind == "omnitron_attributes"
    assert row.status == STATUS_SUCCESS
    assert (row.rows_seen, row.rows_imported, row.rows_dropped) == (149, 149, 0)
    assert row.finished_at is not None
    assert row.error_summary is None


def test_the_row_exists_and_is_running_before_the_body_finishes() -> None:
    """A process killed mid-sync must leave evidence behind.

    If the row were written on exit, a crashed run would be indistinguishable
    from one that never started — and the staleness check could not tell a hung
    job from an unscheduled one.
    """
    with sync_run("omnitron_products"):
        row = only_row()
        assert row.status == STATUS_RUNNING
        assert row.finished_at is None


# ── failure ────────────────────────────────────────────────────────────────


def test_a_failing_run_is_recorded_and_the_error_re_raised() -> None:
    with pytest.raises(ValueError, match="boom"), sync_run("omnitron_attributes") as run:
        run.seen(10)
        run.imported(4)
        raise ValueError("boom")

    row = only_row()
    assert row.status == STATUS_FAILED
    assert row.finished_at is not None
    # Counts up to the failure are kept: a run that died half way did still
    # read ten rows, and zeroing that would hide how far it got.
    assert (row.rows_seen, row.rows_imported) == (10, 4)
    assert "ValueError: boom" in (row.error_summary or "")


def test_keyboard_interrupt_still_closes_the_row() -> None:
    """Caught as BaseException on purpose — an interrupted long sync that stays
    `running` forever would read as a permanent hang."""
    with pytest.raises(KeyboardInterrupt), sync_run("omnitron_products"):
        raise KeyboardInterrupt

    assert only_row().status == STATUS_FAILED


def test_a_payload_rollback_does_not_erase_the_run_record() -> None:
    """The run row is written in its own transaction precisely so that the
    stage's own failed write cannot take the evidence down with it."""
    with pytest.raises(RuntimeError), sync_run("omnitron_attributes"), session_scope() as db:
        db.add(SyncRunRow(kind="payload-side-write", status=STATUS_RUNNING))
        raise RuntimeError("payload blew up")

    remaining = rows()
    assert [r.kind for r in remaining] == ["omnitron_attributes"]
    assert remaining[0].status == STATUS_FAILED


# ── partial ────────────────────────────────────────────────────────────────


def test_dropping_rows_downgrades_success_to_partial() -> None:
    """A sync that silently drops 3% of its input and reports success is how a
    coverage number goes quietly wrong."""
    with sync_run("omnitron_products") as run:
        run.seen(100)
        run.imported(97)
        run.dropped(2, reason="no attribute in registry")
        run.dropped(1, reason="unparseable sku")

    row = only_row()
    assert row.status == STATUS_PARTIAL
    assert row.rows_dropped == 3
    assert "no attribute in registry: 2" in (row.error_summary or "")
    assert "unparseable sku: 1" in (row.error_summary or "")


def test_drop_reasons_are_tallied_not_listed() -> None:
    with sync_run("omnitron_products") as run:
        for _ in range(500):
            run.dropped(reason="no attribute in registry")
        assert run.drop_reasons == {"no attribute in registry": 500}


# ── the watermark ──────────────────────────────────────────────────────────


def test_no_watermark_before_the_first_run() -> None:
    assert last_successful_run("omnitron_attributes") is None


def test_watermark_is_the_start_time_not_the_finish_time() -> None:
    """Anything modified *during* a long run must be caught by the next one.
    Starting the next window at `started_at` guarantees an overlap; using
    `finished_at` would leave a gap the width of the run, and rows falling into
    it would never be read again."""
    with sync_run("omnitron_attributes") as run:
        run.imported(1)

    row = only_row()
    assert last_successful_run("omnitron_attributes") == row.started_at
    assert row.finished_at is not None and row.finished_at >= row.started_at


def test_a_failed_run_does_not_advance_the_watermark() -> None:
    with pytest.raises(ValueError), sync_run("omnitron_attributes"):
        raise ValueError("boom")

    assert last_successful_run("omnitron_attributes") is None


def test_a_partial_run_does_advance_the_watermark() -> None:
    """It still read everything up to its finish time. Treating a partial as a
    failure would re-read the whole window forever on one unparseable row."""
    with sync_run("omnitron_attributes") as run:
        run.seen(10)
        run.imported(9)
        run.dropped(1, reason="unparseable")

    assert last_successful_run("omnitron_attributes") is not None


def test_the_watermark_is_per_kind() -> None:
    with sync_run("omnitron_attributes") as run:
        run.imported(1)

    assert last_successful_run("omnitron_attributes") is not None
    assert last_successful_run("omnitron_products") is None


# ── staleness is a claim about cadence ─────────────────────────────────────


def test_a_nightly_kind_uses_the_default_threshold() -> None:
    from pim_studio.config import settings
    from pim_studio.sync.runs import stale_after_hours

    assert stale_after_hours("omnitron_catalogue") == settings.stale_sync_alert_hours


def test_a_weekly_kind_is_not_judged_by_the_nightly_threshold() -> None:
    """One global number is a claim about cadence, and a wrong one once a
    second cadence exists.

    Image publishing runs weekly. Under the nightly threshold it would report
    stale six days out of every seven — a permanently red badge, which teaches
    people to stop reading the row that is genuinely red.
    """
    from pim_studio.config import settings
    from pim_studio.sync.runs import stale_after_hours

    weekly = stale_after_hours("brandlibrary_publish")

    assert weekly > settings.stale_sync_alert_hours
    assert weekly >= 7 * 24  # a full week must never read as stale
    # ...and one missed run should be visible before it is an alarm.
    assert weekly < 14 * 24


def test_a_kind_nothing_schedules_has_no_threshold() -> None:
    """The push runs when a person starts it, and at no other time.

    Judged by the nightly default it read as stale whenever nobody had pushed
    for a day and a half — 17 pushes in its first 17 days, every one started
    by hand — and the banner then told the page that everything on it was as
    old as the last push, about a stage that feeds nothing the page reads.
    There is no age at which an on-demand job is late.

    Asked by `push.RUN_KIND` rather than a literal, so renaming the kind
    without moving it here fails this test instead of quietly restoring the
    alarm.
    """
    from pim_studio.sync import push
    from pim_studio.sync.runs import stale_after_hours

    assert stale_after_hours(push.RUN_KIND) is None
