"""The keep-alive `check` command — the keeper's go/no-go signal.

The exit code is a contract with `pim-studio-keeper.sh`: 0 means the stored
cookie still works, 2 means log in for a fresh one. Getting that wrong in either
direction is the failure with consequences — a false 0 lets a dead session sail
into Tuesday's publish, a false 2 logs in every few hours for no reason — so the
codes are what these tests pin, along with the one write `check` is allowed to
make: recording that a live read happened.
"""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pim_studio import db as db_module
from pim_studio.brandlibrary import __main__ as main_module
from pim_studio.brandlibrary import session as session_store
from pim_studio.brandlibrary.client import AuthFailed
from pim_studio.models import Base


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'check.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class FakeClient:
    """Stands in for the API client so `check` never touches the network."""

    def __init__(self, ok: bool, message: str, mode: str = "cookie") -> None:
        self._ok = ok
        self._message = message
        self.mode = mode
        self.closed = False

    def test(self) -> tuple[bool, str]:
        return self._ok, self._message

    def close(self) -> None:
        self.closed = True


def test_check_alive_returns_0_and_records_success(db, monkeypatch):
    session_store.rotate(db, cookie="bynder=abc", user_agent="UA/1", by="tester")
    assert session_store.current(db).last_ok_at is None

    client = FakeClient(True, "48 210 photos visible · using a pasted cookie")
    monkeypatch.setattr(main_module, "_client", lambda _db: client)

    rc = main_module.cmd_check(argparse.Namespace())

    assert rc == 0
    assert client.closed
    db.expire_all()
    assert session_store.current(db).last_ok_at is not None


def test_check_dead_returns_2_and_records_nothing(db, monkeypatch):
    session_store.rotate(db, cookie="bynder=stale", user_agent=None, by="tester")

    client = FakeClient(False, "the cookie has expired or been revoked")
    monkeypatch.setattr(main_module, "_client", lambda _db: client)

    rc = main_module.cmd_check(argparse.Namespace())

    assert rc == 2
    assert client.closed
    db.expire_all()
    # A failed read must not look like a success — last_ok_at stays unset.
    assert session_store.current(db).last_ok_at is None


def test_check_no_credential_returns_2(db, monkeypatch):
    # `build` raises AuthFailed when there is nothing to use at all; for the
    # keeper that is simply "log in", not a crash.
    def boom(_db):
        raise AuthFailed("no way into the brand library")

    monkeypatch.setattr(main_module, "_client", boom)

    assert main_module.cmd_check(argparse.Namespace()) == 2


def test_check_oauth_does_not_touch_the_cookie_row(db, monkeypatch):
    # In OAuth mode there is no stored cookie to bless — note_success would be
    # meaningless, so `check` skips it.
    client = FakeClient(True, "48 210 photos visible · signs itself in", mode="oauth")
    monkeypatch.setattr(main_module, "_client", lambda _db: client)

    rc = main_module.cmd_check(argparse.Namespace())

    assert rc == 0
    assert session_store.current(db) is None
