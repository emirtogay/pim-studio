"""Snapshots — a nightly copy of the database, kept in tiers.

The break-glass half of the two-part answer to "we made a mistake and want it
back". The other half is per-value change history, which answers *what* changed
and when; this answers the disasters history cannot rebuild you from — a bad
migration, a dropped table, a corrupted volume.

## What is in the file

`pg_dump` of `northwind_prices`, which is **both applications**. The two share a
database on purpose — `users` is shared so a person signs in once, and
PostgreSQL cannot join across databases — so a snapshot taken here also
restores the price-comparison app. That is a feature and a warning: restoring
one of them restores the other.

## Why tiers rather than a flat window

A flat thirty dailies costs thirty files and reaches back one month. Fourteen
daily, eight weekly and twenty-four monthly costs forty-six and reaches back
two years, because what you want from an old snapshot is an *anchor* — a
known-good point before the bad thing — not the exact 200th day. The numbers
are settings rather than constants for the honest reason that nobody has yet
measured a real dump; when the first one lands, widen them against the real
size instead of against my guess.

## Two things that would make this worse than nothing

A half-written dump that looks like a good one. The dump goes to a `.partial`
name and is renamed only on success, so a process killed mid-write leaves
something retention ignores rather than something it counts as this month's
anchor.

A job that silently stops. `pim-studio-keeper.sh` renewed nothing for weeks
because nothing watched it, so this records itself in `sync_runs` like every
other stage: it appears on the Sync Runs page, and it goes stale and says so if
it stops running.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import URL, make_url

from .config import settings
from .logging import log
from .models import utcnow

#: Finished snapshots. Dated rather than sequential so the filename answers
#: "when is this from" without a database.
SNAPSHOT_GLOB = "northwind-*.dump"

#: In-progress ones. A separate suffix rather than a temp directory so a
#: killed run leaves its debris beside the thing it failed to become, where
#: whoever looks will find it.
PARTIAL_SUFFIX = ".partial"

#: `pg_dump --format=custom`: compressed, and restorable table-by-table with
#: `pg_restore`, which matters when the mistake was one table rather than all
#: of them.
_DUMP_ARGS = ("--format=custom", "--no-owner", "--no-privileges")


class SnapshotUnsupported(RuntimeError):
    """The configured database cannot be dumped this way.

    Raised for SQLite, which is the development default. Not an error worth
    failing a developer's machine over — the CLI reports it and exits — but
    emphatically not something to paper over with an empty file that would sit
    in the retention window looking like a backup.
    """


class SnapshotFailed(RuntimeError):
    """`pg_dump` ran and did not succeed. Carries its stderr."""


def _timestamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d-%H%M%S")


def snapshot_name(when: datetime) -> str:
    return f"northwind-{_timestamp(when)}.dump"


def taken_at(path: Path) -> datetime | None:
    """When this snapshot was taken, from its name.

    From the filename rather than the file's mtime: a copied or restored file
    keeps its name and loses its mtime, and retention that reads mtime would
    quietly re-date every snapshot the first time somebody moved the directory.
    """
    stem = path.name.removeprefix("northwind-").removesuffix(".dump")
    try:
        return datetime.strptime(stem, "%Y-%m-%d-%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def existing(root: Path) -> list[tuple[Path, datetime]]:
    """Every readable snapshot in `root`, newest first.

    A file whose name will not parse is not returned, and so is never deleted
    either — retention only removes things it understands.
    """
    found: list[tuple[Path, datetime]] = []
    for path in root.glob(SNAPSHOT_GLOB):
        when = taken_at(path)
        if when is not None:
            found.append((path, when))
    return sorted(found, key=lambda pair: pair[1], reverse=True)


def _newest_per(
    snapshots: Sequence[tuple[Path, datetime]],
    key_of: Callable[[datetime], object],
    limit: int,
) -> set[Path]:
    """The newest snapshot in each of the most recent `limit` periods.

    `snapshots` must be newest first, which is what makes the early exit
    correct: once `limit` distinct periods have been collected, everything
    remaining is older than all of them.
    """
    kept: dict[object, Path] = {}
    for path, when in snapshots:
        key = key_of(when)
        if key in kept:
            continue
        if len(kept) >= limit:
            break
        kept[key] = path
    return set(kept.values())


def plan_retention(
    snapshots: Sequence[tuple[Path, datetime]],
    *,
    daily: int,
    weekly: int,
    monthly: int,
) -> tuple[list[Path], list[Path]]:
    """Split snapshots into those to keep and those to drop.

    Grandfather-father-son: the most recent `daily` days, plus the newest
    snapshot of each of the last `weekly` weeks, plus the newest of each of the
    last `monthly` months. A file is kept if any tier wants it.

    Pure, and separate from the deleting, because this is the part that is
    worth being sure about — a retention bug deletes the thing the whole
    feature exists to protect, and does it silently.
    """
    ordered = sorted(snapshots, key=lambda pair: pair[1], reverse=True)
    keep = (
        _newest_per(ordered, lambda w: w.date(), daily)
        | _newest_per(ordered, lambda w: w.isocalendar()[:2], weekly)
        | _newest_per(ordered, lambda w: (w.year, w.month), monthly)
    )
    return (
        [path for path, _ in ordered if path in keep],
        [path for path, _ in ordered if path not in keep],
    )


def _connection(database_url: str) -> tuple[str, str | None]:
    """A libpq URL `pg_dump` accepts, and the password kept out of it.

    The password travels in `PGPASSWORD` rather than in the URL because the URL
    becomes argv, and argv is world-readable in `ps` for as long as the dump
    runs — which on this database is not a short time.
    """
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise SnapshotUnsupported(
            f"snapshots need PostgreSQL; this is {url.drivername!r}. "
            "The development default is SQLite, where the database is a single "
            "file you can copy."
        )
    # Built field by field rather than with `url.set(password=None)`, which is
    # a no-op: `set()` reads `None` as "leave this alone", so that spelling
    # silently kept the password and put it in argv. A test pins it.
    bare = URL.create(
        drivername="postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    )
    return bare.render_as_string(hide_password=False), url.password


def take(root: Path, *, when: datetime | None = None) -> Path:
    """Write one snapshot and return its path.

    Renamed into place only after `pg_dump` exits cleanly, so nothing that
    retention can see is ever half a file.
    """
    when = when or utcnow()
    connection, password = _connection(settings.database_url)

    root.mkdir(parents=True, exist_ok=True)
    final = root / snapshot_name(when)
    partial = final.with_name(final.name + PARTIAL_SUFFIX)

    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["pg_dump", *_DUMP_ARGS, "--file", str(partial), connection],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SnapshotFailed(
            "pg_dump is not on PATH. It ships with the postgresql-client "
            "package; the database itself runs elsewhere, so the client has to "
            "be installed on whatever host runs this job."
        ) from exc

    if completed.returncode != 0:
        partial.unlink(missing_ok=True)
        raise SnapshotFailed(
            f"pg_dump exited {completed.returncode}: "
            f"{completed.stderr.strip()[:500] or 'no stderr'}"
        )

    partial.replace(final)
    return final


def sweep_partials(root: Path) -> list[Path]:
    """Delete debris from runs that died mid-dump.

    Separate from `prune` because these are not snapshots and were never
    subject to retention — they are simply files nothing will ever read.
    """
    removed = []
    for path in root.glob(SNAPSHOT_GLOB + PARTIAL_SUFFIX):
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def prune(
    root: Path,
    *,
    daily: int | None = None,
    weekly: int | None = None,
    monthly: int | None = None,
) -> list[Path]:
    """Apply the retention plan, returning what was deleted."""
    _, drop = plan_retention(
        existing(root),
        daily=daily if daily is not None else settings.snapshot_keep_daily,
        weekly=weekly if weekly is not None else settings.snapshot_keep_weekly,
        monthly=monthly if monthly is not None else settings.snapshot_keep_monthly,
    )
    for path in drop:
        path.unlink(missing_ok=True)
    return drop


def total_bytes(paths: Iterable[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.exists())


# ── the nightly job ────────────────────────────────────────────────────────


#: `sync_runs.kind`. Recorded like any other stage so the Sync Runs page shows
#: it and the staleness check covers it. A backup nobody watches is the failure
#: this codebase has already had once, in the keeper.
SNAPSHOT_KIND = "snapshot"


def _settle(run, root: Path, path: Path | None) -> None:
    """Count what is on disk and prune, given a dump that succeeded.

    Shared by both entry points rather than written twice. The two differ only
    in who ran `pg_dump`; everything after it — when pruning is allowed, what
    the run row says — must not be able to drift between them.
    """
    if path is not None:
        run.imported(1)
        log.info("snapshot_taken", path=str(path), bytes=path.stat().st_size)

    dropped = prune(root)
    if dropped:
        # `retired`, not `dropped`: the runs page draws retirements as the
        # number worth a second look, and a night that deletes twenty
        # snapshots is either a widened window or a retention bug. Buried in
        # `dropped` it would be invisible.
        run.retired(len(dropped), reason="outside the retention window")

    kept = existing(root)
    run.seen(len(kept))
    log.info(
        "snapshot_pruned",
        kept=len(kept),
        dropped=len(dropped),
        total_bytes=total_bytes(p for p, _ in kept),
    )


def adopt(
    name: str,
    root: Path | None = None,
    *,
    failed_with: str | None = None,
    triggered_by: str | None = None,
) -> Path | None:
    """Record a dump taken by something outside this process.

    `pg_dump` must not be older than the server, and installing a new enough
    client into this image broke the image build — which blocks every deploy,
    not just this job. So the dump is taken by the official `postgres:18`
    container instead, where the client matches the server by construction and
    no package repository can take a release down. See
    `deploy/pim-studio-snapshot.sh`.

    What stays here is everything that has to be got right: the rename that
    makes a dump visible only once it is whole, the rule that pruning happens
    after a successful dump and never before, and the run row. The shell knows
    how to invoke a dump; it does not get to decide when an old backup may be
    deleted.

    `name` is validated rather than trusted. The shell builds it from `date`,
    so a format drift between the two would produce files retention cannot
    read — which is not a crash, it is a directory that silently grows forever
    while reporting that it kept nothing.
    """
    from .sync.runs import sync_run

    root = root or settings.snapshots_root
    if taken_at(Path(name)) is None:
        raise SnapshotFailed(
            f"{name!r} is not a snapshot name this build can read back. "
            "Expected `northwind-YYYY-MM-DD-HHMMSS.dump` — retention parses the "
            "timestamp out of the filename, so a name it cannot read is a file "
            "it will never delete."
        )

    final = root / name
    partial = final.with_name(final.name + PARTIAL_SUFFIX)

    with sync_run(SNAPSHOT_KIND, source_file=name, triggered_by=triggered_by) as run:
        if failed_with:
            # The debris goes, the run row stays. Nothing is pruned: a job that
            # could not write a new copy must not be allowed to delete an old
            # one, and that is the whole reason this branch exists rather than
            # letting the shell decide.
            partial.unlink(missing_ok=True)
            run.seen(len(existing(root)))
            raise SnapshotFailed(failed_with)

        if not partial.exists():
            raise SnapshotFailed(
                f"{partial} is missing though the dump reported success. The "
                "usual cause is a container writing to a path that was never "
                "bind-mounted, which deletes the output with the container."
            )
        partial.replace(final)
        _settle(run, root, final)

    return final


def run_snapshot(root: Path | None = None, triggered_by: str | None = None) -> Path:
    """Take tonight's snapshot, prune the old ones, record the run.

    The order matters: dump first, prune second. Pruning first would, on the
    night the dump fails, delete an old snapshot and replace it with nothing.
    """
    # Imported here rather than at module scope: `sync.runs` reaches the
    # database on import-adjacent paths, and this module is also imported by
    # the CLI purely to print a plan.
    from .sync.runs import sync_run

    root = root or settings.snapshots_root
    when = utcnow()

    # Checked before the run row is opened, not inside it. A developer on the
    # SQLite default would otherwise leave a `failed` snapshot run behind on
    # every attempt, and a runs page with permanent red on it is a runs page
    # people stop reading.
    _connection(settings.database_url)

    with sync_run(
        SNAPSHOT_KIND, source_file=snapshot_name(when), triggered_by=triggered_by
    ) as run:
        stale = sweep_partials(root)
        if stale:
            log.warning("snapshot_partials_swept", count=len(stale))

        path = take(root, when=when)
        _settle(run, root, path)

    return path


def main() -> int:
    """`python -m pim_studio.snapshots`, run from cron."""
    import argparse

    parser = argparse.ArgumentParser(description="Take a database snapshot.")
    parser.add_argument(
        "--root", type=Path, default=None, help="Where snapshots live."
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Show what retention would keep and drop, and take nothing.",
    )
    parser.add_argument(
        "--adopt",
        metavar="NAME",
        default=None,
        help=(
            "Record a dump taken by something else, already written to "
            "<root>/NAME.partial. Used by pim-studio-snapshot.sh, which takes "
            "the dump in a postgres container."
        ),
    )
    parser.add_argument(
        "--failed-with",
        metavar="TEXT",
        default=None,
        help=(
            "With --adopt: the dump failed, and this is why. Records the run "
            "as failed and prunes nothing."
        ),
    )
    args = parser.parse_args()
    root = args.root or settings.snapshots_root

    if args.plan:
        keep, drop = plan_retention(
            existing(root),
            daily=settings.snapshot_keep_daily,
            weekly=settings.snapshot_keep_weekly,
            monthly=settings.snapshot_keep_monthly,
        )
        print(f"{root}\n  keep {len(keep)}  ({total_bytes(keep) / 1e6:.0f} MB)")
        for path in drop:
            print(f"  drop {path.name}")
        return 0

    if args.adopt:
        try:
            path = adopt(args.adopt, root, failed_with=args.failed_with)
        except SnapshotFailed as exc:
            # Already recorded as a failed run by `adopt` — this is the exit
            # code and the line in the log, not the record of it.
            print(f"failed: {exc}")
            return 1
        print(f"{path}  ({path.stat().st_size / 1e6:.0f} MB)")
        return 0

    try:
        path = run_snapshot(root)
    except SnapshotUnsupported as exc:
        print(f"skipped: {exc}")
        return 0
    except SnapshotFailed as exc:
        print(f"failed: {exc}")
        return 1

    print(f"{path}  ({path.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
