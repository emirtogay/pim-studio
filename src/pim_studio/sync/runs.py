"""Run bookkeeping — every sync stage records itself in `sync_runs`.

The table has existed since the baseline migration and until now held exactly
one row, written by a smoke test. This module is what makes it real.

The shape is deliberately the price app's `scrape_runs`, so the operational
habits carry over unchanged: a status page reads the same columns, and the
staleness alert asks the same question ("when did this kind last succeed?").
That question is also what drives incremental sync — `last_successful_run` is
where the `modified_date__gte` watermark comes from, which is why it lives here
next to the thing that defines success rather than in the stage that uses it.
"""

from __future__ import annotations

import traceback
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from ..db import session_scope
from ..logging import log
from ..models import SyncRun as SyncRunRow
from ..models import utcnow

#: `sync_runs.status` values. `partial` means the stage finished but dropped
#: rows it could not handle — a distinction worth keeping, because a sync that
#: silently drops 3% of its input and reports success is how a coverage number
#: goes quietly wrong.
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

#: `sync_runs.mode` values. See `SyncMode`.
MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"

#: Kinds whose correctness depends on a periodic full pass, because each one
#: owns a sweep that only a full read may run — catalogue departures, and
#: price/stock retirement. Lives here rather than in the CLI so the admin API
#: can ask the same question without importing `__main__` and executing it.
RECONCILED_KINDS = ("omnitron_catalogue", "omnitron_liveness")

#: How long a kind may go without a full reconciliation before it is called
#: overdue.
#:
#: Three days, against the nightly full cadence in `deploy/crontab.snippet`:
#: two consecutive missed nights should be visible, and a single late or slow
#: run should not cry wolf. It was nine, set against a weekly pass that turned
#: out never to have run — a threshold generous enough to stay quiet through a
#: fortnight of silent drift, which is most of what a threshold is for.
#:
#: One number rather than one per kind: `catalogue` and `liveness` are the only
#: reconciled kinds and the same nightly entry does both, so a per-kind table
#: would be two ways of saying "3" and a place for them to disagree later.
RECONCILE_OVERDUE_DAYS = 3


def reconcile_overdue_days(kind: str) -> int:
    """How stale this kind's last full pass may get before it is overdue.

    A function rather than the bare constant at the call sites, so a kind that
    genuinely earns its own cadence has somewhere to go without another rewrite
    of everything that asks the question.
    """
    del kind
    return RECONCILE_OVERDUE_DAYS

#: Kinds that do not run nightly, and how long each may go without succeeding
#: before it is genuinely stale. Anything not named here, and not in
#: `ON_DEMAND_KINDS` below, uses `settings.stale_sync_alert_hours`, which is
#: set against a daily cadence.
#:
#: This exists because one global threshold is a claim about cadence, and the
#: moment a second cadence arrived it became a wrong one. `brandlibrary_publish`
#: runs weekly, so under the daily threshold it would report stale six days out
#: of every seven — a permanent red badge that trains people to ignore the row
#: that is genuinely red.
#:
#: Nine days for a weekly job: one missed run should be visible, and should not
#: be an alarm until it is a second missed run. Unrelated to
#: `RECONCILE_OVERDUE_DAYS` above, which is a different question about a
#: different cadence — they were briefly the same number, which read as though
#: one followed from the other.
CADENCE_STALE_HOURS: dict[str, int] = {
    "brandlibrary_publish": 9 * 24,
    # The session keeper runs every four hours. Nine, on the same reasoning as
    # the line above and deliberately not the same number: one missed run
    # should be visible without being an alarm, and the second should alarm.
    # Under the daily default a keeper that died at breakfast would look
    # healthy until the following morning — by which time the weekly publish
    # may already have failed for want of the cookie it was keeping alive,
    # which is exactly how this went unnoticed before.
    "brandlibrary_session": 9,
    # Two-hourly during trading, so a gap of half a day is already several
    # missed runs. It is also the cheapest stage to retry — three minutes, no
    # credentials, no Omnitron — so there is no reason to be patient with it.
    # Set against the measured churn: 7 options left the PL listing in two
    # hours, ~0.17%, which is what makes hours the right unit and minutes a
    # waste of requests.
    "storefront": 6,
}

#: Kinds that run when a person starts them, and on no schedule at all.
#:
#: A threshold says how often something is meant to run, and nothing says how
#: often these are. The push writes approved values to the live catalogue from
#: the Imports page or `sync push`; the nightly pipeline never carries one and
#: no crontab entry starts one. Under the daily default it read as stale
#: whenever nobody had pushed for a day and a half, and the banner then called
#: the whole panel as old as the last push — a stage that feeds nothing the
#: panel reads. Its age says when somebody last used it, which is not a health
#: question.
#:
#: Spelled out rather than imported from `push`, which imports this module. The
#: test asks by `push.RUN_KIND`, so a rename cannot slip past.
ON_DEMAND_KINDS = frozenset({"omnitron_push"})


def stale_after_hours(kind: str) -> int | None:
    """How long this kind may go without succeeding before it is stale.

    `None` for a kind nothing schedules: there is no age at which it is late.
    """
    from ..config import settings

    if kind in ON_DEMAND_KINDS:
        return None
    return CADENCE_STALE_HOURS.get(kind, settings.stale_sync_alert_hours)


class IncrementalSweepViolation(RuntimeError):
    """A reconciliation sweep was attempted during an incremental pass.

    If you are reading this in a traceback, the fix is not to relax the guard.
    An incremental read cannot distinguish "removed upstream" from "not
    modified since the watermark", so a sweep running against its results would
    retire almost everything we hold. Either run the stage with `--full`, or
    move the sweep behind `mode.full`.
    """


@dataclass(frozen=True, slots=True)
class SyncMode:
    """Whether this pass is entitled to conclude that something is absent.

    The whole point of the type is that the entitlement travels with the read
    instead of living in the caller's head. A stage that reads a filtered
    window and then reconciles against the full table is the single most
    destructive mistake available here — it does not error, it silently retires
    every row it did not happen to look at.

    `since` is the watermark to pass as `modified_date__gte`. It is `None` in
    full mode, which is what makes `if mode.since:` a safe way to build params.
    """

    #: False for an incremental pass. Sweeps check this and nothing else.
    full: bool
    #: Watermark for the filtered read. Always None when `full` is True.
    since: datetime | None = None

    @property
    def label(self) -> str:
        return MODE_FULL if self.full else MODE_INCREMENTAL

    def require_full(self, what: str) -> None:
        """Guard a reconciliation sweep. Raises unless this is a full pass."""
        if not self.full:
            raise IncrementalSweepViolation(
                f"{what} needs a full pass: an incremental read cannot tell "
                f"'removed upstream' from 'unmodified since {self.since}', so "
                "the sweep would retire rows that are perfectly fine."
            )

    @classmethod
    def resolve(cls, kind: str, full: bool) -> SyncMode:
        """Pick the mode for a stage, falling back to full when it must.

        A stage with no successful run behind it has no watermark, so there is
        nothing to be incremental *from*. Silently reading "everything since
        None" would either fetch nothing or fetch everything depending on how
        the filter is built, and both are bugs. Promoting to full is the only
        answer that is right either way, and it is logged because a run that
        was asked to be quick and was not should say so.
        """
        if full:
            return cls(full=True)
        watermark = last_successful_run(kind)
        if watermark is None:
            log.info("sync_mode_promoted_to_full", kind=kind, reason="no watermark yet")
            return cls(full=True)
        return cls(full=False, since=watermark)


#: Truncation limit for the stored traceback. The column is Text so this is not
#: a storage constraint — it is so the runs page stays readable. The full
#: traceback is in the JSON log either way.
_ERROR_SUMMARY_CHARS = 2000


class SyncRun:
    """A single sync execution, counting what it saw as it goes.

    Stages call `seen`/`imported`/`dropped` as they work rather than computing
    totals at the end, so a run that crashes half way still leaves an honest
    partial count behind instead of zeros.
    """

    def __init__(self, row_id: int, kind: str, mode: SyncMode | None = None) -> None:
        self.id = row_id
        self.kind = kind
        #: Full unless told otherwise, so a stage that never learned about
        #: modes keeps its old meaning rather than silently becoming a
        #: partial read that claims to be complete.
        self.mode = mode or SyncMode(full=True)
        self.rows_seen = 0
        self.rows_imported = 0
        self.rows_dropped = 0
        self.rows_retired = 0
        self._drop_reasons: dict[str, int] = {}
        self._retire_reasons: dict[str, int] = {}

    # ── counters ───────────────────────────────────────────────────────────

    def seen(self, n: int = 1) -> None:
        """Records rows read from the source."""
        self.rows_seen += n

    def imported(self, n: int = 1) -> None:
        """Records rows written to our side."""
        self.rows_imported += n

    def dropped(self, n: int = 1, reason: str = "unspecified") -> None:
        """Records rows deliberately not written, and why.

        The reason is tallied rather than logged per row: a sync dropping
        thousands of rows for one reason should say so once with a count, not
        flood the log and bury the other reasons.
        """
        self.rows_dropped += n
        self._drop_reasons[reason] = self._drop_reasons.get(reason, 0) + n

    def retired(self, n: int = 1, reason: str = "unspecified") -> None:
        """Records rows removed because a full pass proved upstream lost them.

        Deliberately not folded into `dropped`. A retirement is the sync
        deleting something we previously held, which is the one class of change
        worth being able to spot at a glance on the runs page — a night that
        retires 40,000 rows is either a real catalogue purge or a bug, and
        either way somebody needs to look. Buried inside `rows_dropped`, which
        sits in the thousands on a good day, it would be invisible.
        """
        self.rows_retired += n
        self._retire_reasons[reason] = self._retire_reasons.get(reason, 0) + n

    @property
    def drop_reasons(self) -> dict[str, int]:
        return dict(self._drop_reasons)

    @property
    def retire_reasons(self) -> dict[str, int]:
        return dict(self._retire_reasons)

    def __repr__(self) -> str:
        return (
            f"<SyncRun id={self.id} kind={self.kind!r} mode={self.mode.label} "
            f"seen={self.rows_seen} imported={self.rows_imported} "
            f"dropped={self.rows_dropped} retired={self.rows_retired}>"
        )


@contextmanager
def sync_run(
    kind: str,
    source_file: str | None = None,
    triggered_by: str | None = None,
    mode: SyncMode | None = None,
) -> Generator[SyncRun, None, None]:
    """Wrap a sync stage, recording it in `sync_runs` either way.

    Usage::

        with sync_run("omnitron_attributes") as run:
            for record in api.iter_records("/api/v1/attributes/"):
                run.seen()
                ...
                run.imported()

    The run row is created **and committed** before the body executes, in its
    own transaction. That matters twice over. A process killed mid-sync leaves
    a `running` row behind rather than no evidence at all, which is the only
    way a staleness check can tell "hung" from "never started". And because the
    row is not part of the stage's transaction, a payload rollback cannot erase
    the record of the attempt.

    Exceptions are recorded as `failed` and re-raised — this swallows nothing.
    """
    started = utcnow()
    resolved = mode or SyncMode(full=True)
    with session_scope() as db:
        row = SyncRunRow(
            kind=kind,
            source_file=source_file,
            started_at=started,
            status=STATUS_RUNNING,
            mode=resolved.label,
            uploaded_by=triggered_by,
        )
        db.add(row)
        db.flush()
        run = SyncRun(row_id=row.id, kind=kind, mode=resolved)

    log.info(
        "sync_started",
        sync_run_id=run.id,
        kind=kind,
        source_file=source_file,
        mode=resolved.label,
        since=str(resolved.since) if resolved.since else None,
    )

    try:
        yield run
    except BaseException as exc:
        # BaseException, not Exception: a KeyboardInterrupt during a long sync
        # should still close the row out rather than leave it `running`
        # forever, which would otherwise read as a hang to the staleness check.
        _finalise(
            run,
            status=STATUS_FAILED,
            error_summary=_summarise_error(exc),
        )
        log.error(
            "sync_failed",
            sync_run_id=run.id,
            kind=kind,
            error=f"{type(exc).__name__}: {exc}",
            rows_seen=run.rows_seen,
            rows_imported=run.rows_imported,
            rows_dropped=run.rows_dropped,
        )
        raise

    status = STATUS_PARTIAL if run.rows_dropped else STATUS_SUCCESS
    _finalise(run, status=status, error_summary=_summarise_drops(run))
    log.info(
        "sync_finished",
        sync_run_id=run.id,
        kind=kind,
        status=status,
        mode=run.mode.label,
        rows_seen=run.rows_seen,
        rows_imported=run.rows_imported,
        rows_dropped=run.rows_dropped,
        rows_retired=run.rows_retired,
        drop_reasons=run.drop_reasons or None,
        retire_reasons=run.retire_reasons or None,
        duration_s=round((utcnow() - started).total_seconds(), 1),
    )


def _finalise(run: SyncRun, status: str, error_summary: str | None) -> None:
    """Close out the run row. Its own transaction, for the reason in `sync_run`."""
    with session_scope() as db:
        row = db.get(SyncRunRow, run.id)
        if row is None:  # pragma: no cover — only if someone deleted it mid-run
            log.warning("sync_run_row_missing", sync_run_id=run.id, kind=run.kind)
            return
        row.status = status
        row.finished_at = utcnow()
        row.rows_seen = run.rows_seen
        row.rows_imported = run.rows_imported
        row.rows_dropped = run.rows_dropped
        row.rows_retired = run.rows_retired
        row.error_summary = error_summary


def _summarise_error(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return text[-_ERROR_SUMMARY_CHARS:]


def _summarise_drops(run: SyncRun) -> str | None:
    """Put the drop and retirement tallies where the runs page will show them.

    A `partial` status with no explanation is only marginally better than a
    silent success, so the reasons go in `error_summary` even though nothing
    errored — that column is what the status page renders.

    Retirements are included even when the status is a clean `success`,
    because a run that deleted rows is worth reading about whether or not it
    also dropped any.
    """
    parts: list[str] = []
    if run.rows_dropped:
        detail = ", ".join(f"{r}: {n}" for r, n in sorted(run.drop_reasons.items()))
        parts.append(f"dropped {run.rows_dropped} row(s) — {detail}")
    if run.rows_retired:
        detail = ", ".join(f"{r}: {n}" for r, n in sorted(run.retire_reasons.items()))
        parts.append(f"retired {run.rows_retired} row(s) — {detail}")
    return "; ".join(parts) or None


def last_successful_run(kind: str) -> datetime | None:
    """When this kind of sync last completed without failing.

    The watermark for incremental reads: pass it as `modified_date__gte` and
    the next run is a delta rather than a full mirror. Verified against the
    live instance on 2026-08-14 — `modified_date__gte=2099-01-01` returns zero
    rows, so the filter is genuinely applied and not silently discarded.

    `partial` counts as successful on purpose. A run that dropped some rows
    still read everything up to its finish time, so advancing the watermark is
    correct; treating it as a failure would re-read the whole window forever
    on the strength of one unparseable row.

    Returns `started_at`, not `finished_at`: anything modified *during* a long
    run must be caught by the next one, and starting the window at `started_at`
    guarantees an overlap rather than a gap. Re-reading a handful of rows is
    free; missing one is silent.
    """
    with session_scope() as db:
        return db.scalar(
            select(SyncRunRow.started_at)
            .where(
                SyncRunRow.kind == kind,
                SyncRunRow.status.in_((STATUS_SUCCESS, STATUS_PARTIAL)),
            )
            .order_by(SyncRunRow.started_at.desc())
            .limit(1)
        )


def days_since_full_run(kind: str) -> int | None:
    """Whole days since this kind last reconciled, or None if it never has."""
    last = last_full_run(kind)
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (utcnow() - last).days


def reconciliation_overdue() -> list[tuple[str, int | None]]:
    """Kinds whose last full pass is too old, as (kind, days or None).

    Shared by the CLI and the admin API on purpose. Two implementations of
    "is the data reconciled" would eventually disagree, and the one people see
    in the panel is not the one that would get fixed.
    """
    overdue: list[tuple[str, int | None]] = []
    for kind in RECONCILED_KINDS:
        days = days_since_full_run(kind)
        if days is None or days >= reconcile_overdue_days(kind):
            overdue.append((kind, days))
    return overdue


def last_full_run(kind: str) -> datetime | None:
    """When this kind last completed a pass that read everything.

    Distinct from `last_successful_run` and the distinction is the point. A
    chain of green incremental nights says nothing about whether departures and
    retirements have been reconciled; only a full pass does that. This is the
    number the staleness check should judge reconciliation against, and the one
    the runs page shows as "last full".
    """
    with session_scope() as db:
        return db.scalar(
            select(SyncRunRow.started_at)
            .where(
                SyncRunRow.kind == kind,
                SyncRunRow.mode == MODE_FULL,
                SyncRunRow.status.in_((STATUS_SUCCESS, STATUS_PARTIAL)),
            )
            .order_by(SyncRunRow.started_at.desc())
            .limit(1)
        )
