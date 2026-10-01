"""The lock that keeps the panel and the host's cron job off each other.

Until the panel had a route to the brand library, nothing enforced this and
nothing needed to: the panel's `threading.Lock` covered its own process, the
host job's `flock` covered its own, and the two could not both run because one
of them could not reach the library at all. Host networking removed that
accident, so the exclusion now lives in the one place both sides look — the
database they already share.

**Contention itself is not exercised here.** These run on SQLite, where
`acquire` grants unconditionally by design: one process, and its in-process
lock is the whole story. So the behaviour under contention is tested by
standing in for the lock and asserting what each caller does when told no,
which is the part with branches in it. What cannot be checked without a real
PostgreSQL is that `pg_try_advisory_lock` refuses a second holder, and that is
PostgreSQL's own contract rather than this module's.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pim_studio import locks
from pim_studio.admin import app as app_module
from pim_studio.admin import images_api
from pim_studio.admin.auth import current_user
from pim_studio.brandlibrary import __main__ as main_module
from pim_studio.brandlibrary.targets import Target, TargetSet
from pim_studio.db import session_scope
from pim_studio.models import SyncRun as SyncRunRow
from pim_studio.models import User

pytestmark = pytest.mark.usefixtures("temp_db")


# ── the key ────────────────────────────────────────────────────────────────


def test_the_key_is_stable_across_calls() -> None:
    """Two processes derive it independently; they have to agree."""
    assert locks.key_for("brandlibrary_publish") == locks.key_for("brandlibrary_publish")


def test_different_names_do_not_collide() -> None:
    assert locks.key_for("brandlibrary_publish") != locks.key_for("omnitron_sync")


def test_every_key_fits_a_signed_bigint() -> None:
    """`pg_try_advisory_lock` takes a bigint and Postgres has no unsigned one.

    An unsigned digest overflows for roughly half of all names, which would
    surface as a database error on the day somebody added a lock whose name
    happened to hash high.
    """
    limit = 2**63
    for name in ("brandlibrary_publish", "omnitron_sync", "a", "", "z" * 200):
        assert -limit <= locks.key_for(name) < limit


# ── the fallback ───────────────────────────────────────────────────────────


def test_sqlite_grants_the_lock() -> None:
    """Refusing here would make every test look like a contended run."""
    lock = locks.Advisory(locks.PUBLISH)
    assert lock.acquire() is True
    lock.release()


def test_releasing_a_lock_that_was_never_held_is_harmless() -> None:
    """Both callers release in a `finally` that also runs on the refusal path."""
    locks.Advisory(locks.PUBLISH).release()


def test_held_yields_and_releases() -> None:
    with locks.held(locks.PUBLISH) as got:
        assert got is True


def test_held_releases_even_when_the_body_raises() -> None:
    lock_state: list[str] = []

    class Recording(locks.Advisory):
        def acquire(self) -> bool:
            lock_state.append("acquired")
            return True

        def release(self) -> None:
            lock_state.append("released")

    original = locks.Advisory
    locks.Advisory = Recording  # type: ignore[misc]
    try:
        with pytest.raises(RuntimeError), locks.held("x"):
            raise RuntimeError("boom")
    finally:
        locks.Advisory = original  # type: ignore[misc]

    assert lock_state == ["acquired", "released"]


# ── what each caller does when told no ─────────────────────────────────────


class Refuses:
    """Stands in for a lock somebody else is holding."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.released = 0

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        self.released += 1


def _targets() -> TargetSet:
    return TargetSet(
        targets=(Target("AF0276.J2G"),),
        missing=1,
        banned=0,
        published=0,
        unclassified=0,
        image_source_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


@contextmanager
def _refusing_held(name: str):  # noqa: ANN202 — a stand-in for `locks.held`
    """`held`, for a lock somebody else already has."""
    yield False


def test_the_cli_skips_without_recording_a_run(monkeypatch, capsys) -> None:
    """A deferred run is not a failed one, and must leave no row.

    Recording a failure would age the watermark the staleness alert measures
    from, for a job that did the right thing by standing aside.
    """
    monkeypatch.setattr(main_module, "find_targets", lambda _db, limit=None: _targets())
    monkeypatch.setattr(main_module, "held", _refusing_held)

    rc = main_module.cmd_publish(argparse.Namespace(dry_run=True, limit=None))

    assert rc == 0
    assert "already running on this server" in capsys.readouterr().err
    with session_scope() as db:
        assert db.scalars(select(SyncRunRow)).all() == []


def test_the_panel_refuses_with_a_conflict(monkeypatch) -> None:
    app_module.app.dependency_overrides[current_user] = lambda: User(
        id=1, email="t@example.com", role="planning", name="T", is_active=True
    )
    refused = Refuses(locks.PUBLISH)
    monkeypatch.setattr(images_api, "Advisory", lambda name: refused)
    monkeypatch.setattr(
        images_api,
        "_configured",
        lambda db: {
            "library": True,
            "photoServer": True,
            "ready": True,
            "mode": "oauth",
            "cookie": None,
        },
    )
    monkeypatch.setattr(images_api, "find_targets", lambda db: _targets())
    monkeypatch.setattr(images_api, "_preflight", lambda db: _FakeClient())
    monkeypatch.setattr(images_api, "_publish_in_background", lambda *a, **k: None)

    try:
        client = TestClient(app_module.app, raise_server_exceptions=False)
        response = client.post("/api/images/publish")
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    # It must name the host job too — "already running" with no candidate sends
    # somebody looking for a second browser tab.
    assert "scheduled job on the host" in detail


class _FakeClient:
    mode = "oauth"

    def close(self) -> None:
        pass


def test_the_lock_is_released_when_the_thread_cannot_start(monkeypatch) -> None:
    """The release lives in the thread's `finally`, so a thread that never
    starts would strand the lock until the process restarted."""
    app_module.app.dependency_overrides[current_user] = lambda: User(
        id=1, email="t@example.com", role="planning", name="T", is_active=True
    )
    taken: list[str] = []

    class Grants:
        def __init__(self, name: str) -> None:
            self.name = name

        def acquire(self) -> bool:
            taken.append("acquired")
            return True

        def release(self) -> None:
            taken.append("released")

    monkeypatch.setattr(images_api, "Advisory", Grants)
    monkeypatch.setattr(
        images_api,
        "_configured",
        lambda db: {
            "library": True,
            "photoServer": True,
            "ready": True,
            "mode": "oauth",
            "cookie": None,
        },
    )
    monkeypatch.setattr(images_api, "find_targets", lambda db: _targets())
    monkeypatch.setattr(images_api, "_preflight", lambda db: _FakeClient())

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("no threads today")

    monkeypatch.setattr(images_api.threading, "Thread", explode)

    try:
        client = TestClient(app_module.app, raise_server_exceptions=False)
        client.post("/api/images/publish")
    finally:
        app_module.app.dependency_overrides.clear()

    assert taken == ["acquired", "released"]
