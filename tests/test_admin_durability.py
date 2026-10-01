"""Did the write actually land, and did it let go of the lock?

Two invariants that every mutating endpoint has to satisfy, asserted here as
properties over all of them rather than remembered at each one. Both were
violated by sign-in, in the same single missing commit, and neither violation
was visible from inside the endpoint that caused it.

**1. The change survives the request.** `get_session` hands out a session and
does not commit it, so an endpoint that writes and forgets has its work rolled
back on the way out — and returns 200 while doing it. `authenticate` flushed
`last_login_at` and left the commit to its caller, so the column had never
held a value in its life. The test is to read back through a *fresh* session:
the endpoint's own session would report the write from its identity map
whether or not it was ever committed, which is exactly why nobody noticed.

**2. The request is not still holding a write lock when the audit row is
written.** `audit.record` opens its own session on purpose — it must not be
rolled back with the work it describes. The cost is that an endpoint which
still has an open write transaction is a first connection the audit write
waits on: five seconds on SQLite, then the row is silently dropped.

So "every mutating endpoint produces an audit row" is not a test about
logging. It is the general form of "no endpoint leaves a transaction open",
and it is the one assertion that catches the whole class from outside.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pim_studio import db as db_module
from pim_studio.admin import accounts
from pim_studio.admin import app as app_module
from pim_studio.admin.auth import authenticate, verify_password
from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeValue,
    AuditLog,
    ModuleUserState,
    User,
    ValueConflict,
)

pytestmark = pytest.mark.usefixtures("temp_db")

PASSWORD = "a-long-enough-one"


@pytest.fixture
def developer() -> int:
    with session_scope() as db:
        return accounts.create_user(
            db, email="dev@example.com", password=PASSWORD, role="developer"
        ).id


@pytest.fixture
def client(developer: int) -> Iterator[TestClient]:
    """Signed in with a real cookie — no stubbed dependency anywhere in this
    file. Every claim here is about what the deployed request path does."""
    del developer
    client = TestClient(app_module.app, raise_server_exceptions=False)
    assert (
        client.post(
            "/api/auth/login", json={"email": "dev@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )
    with session_scope() as db:
        db.query(AuditLog).delete()
    yield client


# ── 1. the change survives the request ─────────────────────────────────────


def test_signing_in_stamps_last_login_durably() -> None:
    """Through the endpoint, read back cold."""
    with session_scope() as db:
        accounts.create_user(
            db, email="who@example.com", password=PASSWORD, role="manager"
        )

    client = TestClient(app_module.app, raise_server_exceptions=False)
    client.post(
        "/api/auth/login", json={"email": "who@example.com", "password": PASSWORD}
    )

    with session_scope() as db:
        stamped = db.scalar(select(User).where(User.email == "who@example.com"))
        assert stamped.last_login_at is not None


def test_authenticate_stamps_durably_on_its_own() -> None:
    """The guard against the next caller.

    The endpoint is not the invariant — `authenticate` is, because it is the
    only writer of this column. When the commit lived in the endpoint, a
    second caller anywhere (an SSO exchange, a CLI `check-password`, a script)
    silently lost the stamp again and there was nothing to catch it.

    The session here is opened the way `get_session` opens one — yield, then
    close, and **never commit** — rather than through `session_scope`, which
    commits on the way out. That distinction is the whole test: written against
    `session_scope` this passes with the bug fully present, because the fixture
    supplies the commit the function is missing and the assertion ends up being
    about the test's own plumbing.
    """
    with session_scope() as db:
        accounts.create_user(
            db, email="direct@example.com", password=PASSWORD, role="manager"
        )

    # Resolved through the module: `temp_db` swaps `SessionLocal` there, and
    # importing the name directly would bind the real development database.
    uncommitted = db_module.SessionLocal()
    try:
        assert authenticate(uncommitted, "direct@example.com", PASSWORD) is not None
    finally:
        uncommitted.close()

    with session_scope() as fresh:
        user = fresh.scalar(select(User).where(User.email == "direct@example.com"))
        assert user.last_login_at is not None


def test_creating_an_account_survives_the_request(client: TestClient) -> None:
    client.post(
        "/api/users", json={"email": "made@example.com", "password": PASSWORD}
    )

    with session_scope() as db:
        assert db.scalar(select(User).where(User.email == "made@example.com")) is not None


def test_changing_an_account_survives_the_request(client: TestClient) -> None:
    with session_scope() as db:
        target = accounts.create_user(
            db, email="target@example.com", password=PASSWORD, role="manager"
        ).id

    client.patch(f"/api/users/{target}", json={"role": "planning", "is_active": False})

    with session_scope() as db:
        after = db.get(User, target)
        assert (after.role, after.is_active) == ("planning", False)


def test_a_password_reset_survives_the_request(client: TestClient) -> None:
    """The one write whose loss would be invisible until somebody tried to
    sign in with a password they were told had been set."""
    with session_scope() as db:
        target = accounts.create_user(
            db, email="reset@example.com", password=PASSWORD, role="manager"
        ).id

    client.post(f"/api/users/{target}/password", json={"password": "a-different-one"})

    with session_scope() as db:
        assert verify_password("a-different-one", db.get(User, target).password_hash)


def test_acknowledging_the_changelog_survives_the_request(
    client: TestClient, developer: int
) -> None:
    client.post("/api/auth/acknowledge_changelog")

    with session_scope() as db:
        row = db.scalar(
            select(ModuleUserState).where(ModuleUserState.user_id == developer)
        )
        assert row is not None and row.last_seen_changelog_version


def test_deciding_a_conflict_survives_the_request(client: TestClient) -> None:
    with session_scope() as db:
        attribute = Attribute(
            key="sleeve_type",
            label="Sleeve type",
            write_channel="product_card",
            value_grain="model_color",
            per_market=True,
        )
        db.add(attribute)
        db.flush()
        db.add(
            ValueConflict(
                entity_kind="model_color",
                entity_key="L3539.132",
                attribute_id=attribute.id,
                market="PL",
                candidates=json.dumps({"short": 3, "long": 2}),
                sku_count=5,
            )
        )

    res = client.post(
        "/api/conflicts/decide",
        json={
            "modelColor": "L3539.132",
            "attribute": "sleeve_type",
            "market": "PL",
            "value": "short",
        },
    )
    assert res.status_code == 200

    with session_scope() as db:
        written = db.scalar(
            select(AttributeValue).where(AttributeValue.entity_key == "L3539.132")
        )
        assert written is not None
        assert (written.value, written.state) == ("short", "approved")
        # The queue row goes with the decision, in the same transaction.
        assert db.scalar(select(func.count()).select_from(ValueConflict)) == 0


# ── 2. no endpoint holds a write lock across its own audit row ─────────────
#
# Parameterised over every mutating endpoint rather than asserted once, because
# the failure is per-endpoint: each one manages its own transaction, and each
# one can forget on its own.


def _create(client: TestClient) -> None:
    client.post("/api/users", json={"email": "audit1@example.com", "password": PASSWORD})


def _patch(client: TestClient) -> None:
    with session_scope() as db:
        target = accounts.create_user(
            db, email="audit2@example.com", password=PASSWORD, role="manager"
        ).id
    client.patch(f"/api/users/{target}", json={"role": "planning"})


def _password(client: TestClient) -> None:
    with session_scope() as db:
        target = accounts.create_user(
            db, email="audit3@example.com", password=PASSWORD, role="manager"
        ).id
    client.post(f"/api/users/{target}/password", json={"password": "a-different-one"})


def _acknowledge(client: TestClient) -> None:
    client.post("/api/auth/acknowledge_changelog")


def _sign_in(client: TestClient) -> None:
    """The one that was broken. A fresh client so it is a real sign-in and not
    a re-post on an existing session."""
    with session_scope() as db:
        accounts.create_user(
            db, email="audit4@example.com", password=PASSWORD, role="manager"
        )
    TestClient(app_module.app, raise_server_exceptions=False).post(
        "/api/auth/login", json={"email": "audit4@example.com", "password": PASSWORD}
    )


@pytest.mark.parametrize(
    ("name", "act"),
    [
        ("sign in", _sign_in),
        ("create an account", _create),
        ("change an account", _patch),
        ("reset a password", _password),
        ("acknowledge the changelog", _acknowledge),
    ],
)
def test_the_audit_row_lands(
    client: TestClient, name: str, act: Callable[[TestClient], None]
) -> None:
    """`audit.record` writes on its own connection, so a row that does not
    appear means the request was still holding a write lock when it fired —
    the symptom is a five-second stall and then silence, in production as much
    as here. Nothing else in this assertion is about logging."""
    act(client)

    with session_scope() as db:
        rows = db.scalars(select(AuditLog)).all()
    assert rows, f"{name} left no audit row — its transaction was still open"
