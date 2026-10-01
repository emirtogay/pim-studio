"""Snapshot naming, retention and refusal.

Retention gets the most attention here by a wide margin, and deliberately: it
is the only part of this feature that *deletes* anything. A dump that fails is
loud and recoverable — tomorrow's runs. A retention bug throws away the
snapshot you were going to restore from, and does it quietly, months before
anybody looks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pim_studio.snapshots import (
    PARTIAL_SUFFIX,
    SnapshotFailed,
    SnapshotUnsupported,
    _connection,
    adopt,
    existing,
    plan_retention,
    prune,
    snapshot_name,
    sweep_partials,
    taken_at,
)


def at(text: str) -> datetime:
    """`2026-09-07` or `2026-09-07 03:00` → an aware datetime."""
    fmt = "%Y-%m-%d %H:%M" if " " in text else "%Y-%m-%d"
    return datetime.strptime(text, fmt).replace(tzinfo=UTC)


def nightly(first: str, days: int, *, hour: int = 3) -> list[tuple[Path, datetime]]:
    """One snapshot a night, oldest first, the way the cron job makes them."""
    start = at(first).replace(hour=hour)
    return [
        (Path(snapshot_name(start + timedelta(days=n))), start + timedelta(days=n))
        for n in range(days)
    ]


TIERS = {"daily": 14, "weekly": 8, "monthly": 24}


# ── naming ─────────────────────────────────────────────────────────────────


def test_a_name_round_trips_to_the_moment_it_was_taken():
    when = at("2026-09-07 02:30")
    assert taken_at(Path(snapshot_name(when))) == when


def test_a_name_that_does_not_parse_is_not_a_snapshot():
    """And therefore never deleted. Retention only removes what it understands
    — a stray file in the directory is somebody's, not ours."""
    assert taken_at(Path("northwind-backup-final-FINAL.dump")) is None
    assert taken_at(Path("notes.txt")) is None


def test_a_partial_is_not_a_snapshot(tmp_path: Path):
    """The half-written file a killed dump leaves behind. Counting it would let
    a truncated file become the month's anchor."""
    (tmp_path / (snapshot_name(at("2026-09-07")) + PARTIAL_SUFFIX)).write_bytes(b"xx")
    assert existing(tmp_path) == []


# ── retention ──────────────────────────────────────────────────────────────


def test_nothing_is_dropped_before_any_tier_fills():
    keep, drop = plan_retention(nightly("2026-09-01", 10), **TIERS)
    assert len(keep) == 10
    assert drop == []


def test_the_daily_tier_keeps_the_last_fourteen_days():
    """Sixty nightlies. The most recent fourteen are kept as dailies whatever
    the other tiers decide."""
    snapshots = nightly("2026-07-01", 60)
    keep, _ = plan_retention(snapshots, **TIERS)
    newest = [path for path, _ in sorted(snapshots, key=lambda p: p[1], reverse=True)]
    assert set(newest[:14]) <= set(keep)


def test_a_second_snapshot_on_one_day_does_not_consume_two_days_of_window():
    """Two dumps in a day — a manual one beside the cron one. The tier is
    fourteen *days*, not fourteen files, or a day with three runs would silently
    shorten the window to twelve."""
    snapshots = nightly("2026-08-01", 30)
    extra = at("2026-08-30 21:00")
    snapshots.append((Path(snapshot_name(extra)), extra))
    keep, _ = plan_retention(snapshots, daily=14, weekly=0, monthly=0)
    assert len({when.date() for path, when in snapshots if path in keep}) == 14


def test_beyond_the_daily_window_one_anchor_per_week_survives():
    snapshots = nightly("2026-06-01", 90)
    keep, _ = plan_retention(snapshots, daily=14, weekly=8, monthly=0)
    kept_when = [when for path, when in snapshots if path in keep]
    old = [w for w in kept_when if w < max(kept_when) - timedelta(days=14)]
    # Every survivor outside the daily window is the newest of its own week.
    assert len({w.isocalendar()[:2] for w in old}) == len(old)


def test_monthly_anchors_reach_back_years():
    """The point of tiers. Two years of nightlies is 730 files; this keeps
    dozens and can still answer 'what did it look like last September'."""
    snapshots = nightly("2024-09-08", 730)
    keep, drop = plan_retention(snapshots, **TIERS)
    assert len(keep) < 60
    assert len(drop) > 660
    kept_months = {when.strftime("%Y-%m") for path, when in snapshots if path in keep}
    assert "2025-09" in kept_months


def test_the_newest_snapshot_is_never_dropped():
    """The one invariant that matters most: whatever else retention decides,
    last night's copy stays."""
    for days in (1, 15, 100, 400):
        snapshots = nightly("2025-01-01", days)
        keep, _ = plan_retention(snapshots, **TIERS)
        newest = max(snapshots, key=lambda pair: pair[1])[0]
        assert newest in keep


def test_keep_and_drop_partition_the_input():
    """No file is both kept and dropped, and none goes missing from the plan —
    a file absent from both lists would be one nothing ever deletes."""
    snapshots = nightly("2025-01-01", 400)
    keep, drop = plan_retention(snapshots, **TIERS)
    assert set(keep) | set(drop) == {path for path, _ in snapshots}
    assert not set(keep) & set(drop)


# ── the deleting half ──────────────────────────────────────────────────────


def test_prune_deletes_exactly_what_the_plan_said(tmp_path: Path):
    for path, _ in nightly("2026-01-01", 200):
        (tmp_path / path.name).write_bytes(b"x")
    before = {p.name for p in tmp_path.iterdir()}
    dropped = prune(tmp_path, **TIERS)
    after = {p.name for p in tmp_path.iterdir()}
    assert {p.name for p in dropped} == before - after
    assert after


def test_prune_leaves_files_it_does_not_understand(tmp_path: Path):
    (tmp_path / "please-do-not-delete.sql").write_bytes(b"x")
    for path, _ in nightly("2026-01-01", 200):
        (tmp_path / path.name).write_bytes(b"x")
    prune(tmp_path, **TIERS)
    assert (tmp_path / "please-do-not-delete.sql").exists()


def test_sweeping_partials_removes_only_partials(tmp_path: Path):
    good = tmp_path / snapshot_name(at("2026-09-07"))
    good.write_bytes(b"x")
    (tmp_path / (snapshot_name(at("2026-09-06")) + PARTIAL_SUFFIX)).write_bytes(b"x")
    assert len(sweep_partials(tmp_path)) == 1
    assert good.exists()


# ── refusing rather than pretending ────────────────────────────────────────


def test_sqlite_is_refused_not_faked():
    """The development default. An empty file here would sit in the retention
    window looking exactly like a backup."""
    with pytest.raises(SnapshotUnsupported):
        _connection("sqlite:////tmp/pim_studio.db")


def test_the_password_is_kept_out_of_the_command_line():
    """argv is world-readable in `ps` for as long as the dump runs."""
    connection, password = _connection(
        "postgresql+psycopg://northwind:hunter2@127.0.0.1:5433/northwind_prices"
    )
    assert password == "hunter2"
    assert "hunter2" not in connection
    # And `pg_dump` cannot parse the SQLAlchemy driver suffix.
    assert connection.startswith("postgresql://")
    assert "psycopg" not in connection


# ── adopting a dump taken elsewhere ────────────────────────────────────────
#
# `pg_dump` runs in the postgres image, not ours, because installing a new
# enough client into ours broke the image build and a broken build blocks every
# deploy. What stays in Python is everything that has to be got right, and the
# most important of those is that a failed dump deletes nothing.


def partial_for(root: Path, name: str) -> Path:
    path = root / (name + PARTIAL_SUFFIX)
    path.write_bytes(b"pretend this is a custom-format dump")
    return path


def test_a_dump_becomes_visible_only_once_it_is_whole(temp_db, tmp_path: Path):
    name = snapshot_name(at("2026-09-07 01:30"))
    partial = partial_for(tmp_path, name)
    adopt(name, tmp_path)
    assert not partial.exists()
    assert (tmp_path / name).exists()


def test_a_failed_dump_deletes_nothing(temp_db, tmp_path: Path):
    """The invariant this whole feature rests on. A job that could not write a
    new copy must not be allowed to remove an old one — and with 200 nightlies
    on disk, a prune that ran anyway would take the newest survivors' place."""
    for path, _ in nightly("2026-01-01", 200):
        (tmp_path / path.name).write_bytes(b"x")
    before = {p.name for p in tmp_path.iterdir()}

    name = snapshot_name(at("2026-09-07 01:30"))
    partial_for(tmp_path, name)
    with pytest.raises(SnapshotFailed):
        adopt(name, tmp_path, failed_with="pg_dump exited 1: connection refused")

    after = {p.name for p in tmp_path.iterdir()}
    assert after == before, "a failed dump pruned snapshots"


def test_a_failed_dump_leaves_no_debris(temp_db, tmp_path: Path):
    name = snapshot_name(at("2026-09-07 01:30"))
    partial = partial_for(tmp_path, name)
    with pytest.raises(SnapshotFailed):
        adopt(name, tmp_path, failed_with="pg_dump exited 1")
    assert not partial.exists()
    assert not (tmp_path / name).exists()


def test_a_dump_that_reported_success_but_wrote_nothing_is_refused(
    temp_db, tmp_path: Path
):
    """The bind-mount trap: a container writing to a path that was never
    mounted deletes its output with itself, and reports success doing it."""
    name = snapshot_name(at("2026-09-07 01:30"))
    with pytest.raises(SnapshotFailed, match="bind-mounted"):
        adopt(name, tmp_path)


def test_a_name_retention_cannot_read_is_refused(temp_db, tmp_path: Path):
    """Not a crash if allowed through — worse. Retention parses the timestamp
    out of the filename, so a name it cannot read is a file it will never
    delete, and the directory grows forever while reporting it kept nothing."""
    (tmp_path / "backup-final.dump.partial").write_bytes(b"x")
    with pytest.raises(SnapshotFailed, match="read back"):
        adopt("backup-final.dump", tmp_path)


def test_adopting_prunes_to_the_tiers(temp_db, tmp_path: Path):
    for path, _ in nightly("2026-01-01", 200):
        (tmp_path / path.name).write_bytes(b"x")
    name = snapshot_name(at("2026-09-07 01:30"))
    partial_for(tmp_path, name)
    adopt(name, tmp_path)
    assert len(existing(tmp_path)) < 60
    assert (tmp_path / name).exists()


