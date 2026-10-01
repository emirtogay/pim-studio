"""The session keeper, made visible.

The keeper renews the library cookie every four hours and recorded nothing
anywhere. When it stopped working the first symptom was the *weekly* publish
failing days later, on the runs page, against a job that was not the problem.
0.27.2 records exactly that: a keeper renewing nothing from the day it was
installed, noticed only through the publish.

So `check --record` files the result as its own run kind. What these pin is the
shape of that: a live session is green, a dead one is red rather than a
success that read nothing, and the *probe* the keeper runs first stays
unrecorded — because a cookie expiring is the normal case the keeper exists to
handle, and filing it would put a red row on the page every time the thing
worked.
"""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import select

from pim_studio.brandlibrary import __main__ as main_module
from pim_studio.brandlibrary.client import AuthFailed
from pim_studio.db import session_scope
from pim_studio.models import SyncRun as SyncRunRow
from pim_studio.sync.runs import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    stale_after_hours,
)

pytestmark = pytest.mark.usefixtures("temp_db")


class Library:
    """A stand-in whose `test` answers or reports failure, as the real one does."""

    mode = "oauth"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False

    def check(self) -> str:
        if self.error:
            raise self.error
        return "48 210 photos visible · signs itself in"

    def test(self) -> tuple[bool, str]:
        try:
            return True, self.check()
        except Exception as exc:  # noqa: BLE001 - mirrors the real client
            return False, str(exc)

    def close(self) -> None:
        self.closed = True


def run_check(monkeypatch, library: Library, *, record: bool) -> int:
    monkeypatch.setattr(main_module, "_client", lambda _db: library)
    return main_module.cmd_check(argparse.Namespace(record=record))


def runs() -> list[SyncRunRow]:
    with session_scope() as db:
        return list(db.scalars(select(SyncRunRow).order_by(SyncRunRow.id)))


def test_a_live_session_is_a_successful_run(monkeypatch) -> None:
    assert run_check(monkeypatch, Library(), record=True) == 0
    recorded = runs()
    assert len(recorded) == 1
    assert recorded[0].kind == "brandlibrary_session"
    assert recorded[0].status == STATUS_SUCCESS


def test_a_dead_session_is_a_failed_run_not_a_quiet_one(monkeypatch) -> None:
    """The whole point. A `return` here would leave `sync_run` normally and
    file the run as a success that read nothing — which is also the watermark
    the staleness alert asks about, so a keeper failing every four hours would
    look healthy forever."""
    rc = run_check(monkeypatch, Library(AuthFailed("the cookie has expired")), record=True)
    assert rc == 2
    recorded = runs()
    assert len(recorded) == 1
    assert recorded[0].status == STATUS_FAILED


def test_the_exit_code_contract_with_the_keeper_survives_recording(monkeypatch) -> None:
    """2 still means "the stored credential does not work". The shell branches
    on it, so recording must not change what it returns."""
    assert run_check(monkeypatch, Library(AuthFailed("expired")), record=True) == 2
    assert run_check(monkeypatch, Library(), record=True) == 0


def test_the_unrecorded_probe_files_nothing(monkeypatch) -> None:
    """The keeper's first check. A cookie expiring is the normal case it exists
    to handle; filing that would put a red row on the page every time the
    keeper worked."""
    assert run_check(monkeypatch, Library(AuthFailed("expired")), record=False) == 2
    assert runs() == []


def test_the_client_is_closed_either_way(monkeypatch) -> None:
    live = Library()
    run_check(monkeypatch, live, record=True)
    dead = Library(AuthFailed("expired"))
    run_check(monkeypatch, dead, record=True)
    assert live.closed and dead.closed


def test_the_session_is_judged_on_its_own_clock() -> None:
    """Four-hourly, so the daily default would let a keeper that died at
    breakfast look healthy until the next morning — by which time the weekly
    publish may already have failed for want of the cookie."""
    assert stale_after_hours("brandlibrary_session") == 9
    assert stale_after_hours("brandlibrary_session") < stale_after_hours("values")


