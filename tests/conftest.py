"""Shared fixtures.

Until now every test here was a pure function over plain values, so none of
them needed a database. The sync stages write, so they do — and the one thing
this fixture must guarantee is that a test run cannot touch `pim_studio.db`,
the real development database sitting in the project root.

`db.py` builds its engine and `SessionLocal` at import time from
`settings.database_url`. Rather than reach into the settings, the fixture swaps
`SessionLocal` on the module itself: `session_scope()` looks it up as a
module-global on each call, so the substitution reaches every caller — including
code that already did `from ..db import session_scope`.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pim_studio import db as db_module
from pim_studio.models import Base


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Point `session_scope()` at a throwaway SQLite file for one test.

    A file rather than `:memory:` — `session_scope` opens and closes a session
    per call, and an in-memory SQLite database is discarded when its last
    connection closes, so state would not survive between the two transactions
    `sync_run` deliberately uses.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )
    try:
        yield
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn argon2's cost down for the duration of a test run.

    argon2 is deliberately slow, which is the entire point of it in production
    and a 90-second test file everywhere else — the admin tests sign in for
    real rather than stubbing the dependency, so every one of them pays twice.

    Only the cost parameters move. The scheme, the verification path and the
    hash format are the production ones, so a test that proves a password now
    works, or no longer does, is proving it about the real code.
    """
    from passlib.context import CryptContext

    from pim_studio.admin import auth

    monkeypatch.setattr(
        auth,
        "_pwd_context",
        CryptContext(
            schemes=["argon2"],
            deprecated="auto",
            argon2__rounds=1,
            argon2__memory_cost=8,
            argon2__parallelism=1,
        ),
    )
