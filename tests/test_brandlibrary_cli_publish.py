"""What the publish CLI writes to `sync_runs`, especially when it does nothing.

The bug these pin: `cmd_publish` checked the library from *inside* its
`sync_run` and bailed with `return 1` when the check failed. A `return` leaves
a context manager normally, so `sync_run` — which infers failure from an
escaping exception — recorded the run as a success that read nothing. The
shell got 1 and the database got `success`, and the database is the half the
panel and the staleness alert read.

That made the alert unreachable rather than merely late. The threshold for
this kind is nine days measured from the last *successful* run, so every
failed Tuesday refreshed the watermark it was supposed to age past. The job
could fail every week forever and stay green.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from pim_studio.brandlibrary import __main__ as main_module
from pim_studio.brandlibrary.client import AuthFailed, BrandLibraryError
from pim_studio.brandlibrary.targets import Target, TargetSet
from pim_studio.db import session_scope
from pim_studio.models import SyncRun as SyncRunRow
from pim_studio.sync import last_successful_run
from pim_studio.sync.runs import STATUS_FAILED, STATUS_SUCCESS

pytestmark = pytest.mark.usefixtures("temp_db")

#: The certificate error the server actually produced, abbreviated. Kept
#: verbatim because the point is that an unreachable library arrives as an
#: ordinary `BrandLibraryError` message, not as anything the CLI can pattern
#: match on.
UNREACHABLE = (
    "GET /api/v4/media/ failed after 5 attempts: [SSL: CERTIFICATE_VERIFY_FAILED] "
    "certificate verify failed: self-signed certificate (_ssl.c:1010)"
)


def args(dry_run: bool = True, limit: int | None = None) -> argparse.Namespace:
    return argparse.Namespace(dry_run=dry_run, limit=limit)


def some_targets() -> TargetSet:
    """A non-empty target set, so an empty one is never the reason for a zero."""
    return TargetSet(
        targets=(Target("TSM1YS.33X"), Target("PH9619.001")),
        missing=2,
        banned=0,
        published=0,
        unclassified=0,
        image_source_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


class Library:
    """A stand-in whose `check` either answers or raises, as the real one does."""

    mode = "oauth"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scanned = False

    def check(self) -> str:
        if self.error:
            raise self.error
        return "48 210 photos visible · signs itself in"

    def test(self) -> tuple[bool, str]:
        """The reporting form, mirroring the real client exactly.

        Present so that reverting the fix exercises the actual bug — a run
        filed as a success — rather than tripping over a missing attribute.
        """
        try:
            return True, self.check()
        except BrandLibraryError as exc:
            return False, str(exc)

    def catalog_index(self, years, on_progress=None):  # noqa: ANN001, ARG002
        self.scanned = True
        return []


def run_publish(monkeypatch, library: Library, **kwargs) -> int:
    monkeypatch.setattr(main_module, "find_targets", lambda _db, limit=None: some_targets())
    monkeypatch.setattr(main_module, "_client", lambda _db: library)
    return main_module.cmd_publish(args(**kwargs))


def only_run() -> SyncRunRow:
    with session_scope() as db:
        rows = list(db.scalars(select(SyncRunRow).order_by(SyncRunRow.id)))
    assert len(rows) == 1, f"expected one run row, got {len(rows)}"
    return rows[0]


# ── the regression ─────────────────────────────────────────────────────────


def test_an_unreachable_library_is_recorded_as_a_failed_run(monkeypatch) -> None:
    rc = run_publish(monkeypatch, Library(BrandLibraryError(UNREACHABLE)))

    assert rc == 1
    row = only_run()
    assert row.status == STATUS_FAILED
    assert row.rows_seen == 0
    # The message has to survive onto the row: a failed run whose reason is not
    # written down sends the next reader back to the container logs.
    assert "CERTIFICATE_VERIFY_FAILED" in (row.error_summary or "")


def test_a_failed_run_does_not_refresh_the_success_watermark(monkeypatch) -> None:
    """The half of the bug with consequences beyond a misleading tile."""
    run_publish(monkeypatch, Library(BrandLibraryError(UNREACHABLE)))

    assert last_successful_run("brandlibrary_publish") is None


def test_a_dead_credential_is_also_a_failed_run(monkeypatch) -> None:
    """Exit 2 is the keeper's signal to log in again; the row still says failed.

    The exit code and the run status answer different questions, and the fix
    must not have collapsed them into one.
    """
    rc = run_publish(monkeypatch, Library(AuthFailed("the cookie has expired")))

    assert rc == 2
    assert only_run().status == STATUS_FAILED


def test_the_library_is_never_scanned_when_the_check_fails(monkeypatch) -> None:
    """The check is a preflight, not a formality — a failure stops the run."""
    library = Library(BrandLibraryError(UNREACHABLE))
    run_publish(monkeypatch, library)

    assert not library.scanned


# ── and the path that should still be green ────────────────────────────────


def test_a_reachable_library_still_records_success(monkeypatch) -> None:
    rc = run_publish(monkeypatch, Library())

    assert rc == 0
    row = only_run()
    assert row.status == STATUS_SUCCESS
    assert last_successful_run("brandlibrary_publish") is not None


def test_nothing_to_do_opens_no_run_at_all(monkeypatch) -> None:
    """An empty target set is not a failure, and must not leave a zeroed row.

    `publish` returns early on empty input without touching the counters, so a
    run opened around it would be the same misleading success this module is
    about. The guard sits before `sync_run` instead.
    """
    empty = TargetSet(
        targets=(),
        missing=0,
        banned=0,
        published=0,
        unclassified=0,
        image_source_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(main_module, "find_targets", lambda _db, limit=None: empty)

    assert main_module.cmd_publish(args()) == 0
    with session_scope() as db:
        assert db.scalars(select(SyncRunRow)).all() == []
