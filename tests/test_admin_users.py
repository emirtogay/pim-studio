"""Account management — the rules, and the two callers that share them.

The endpoints are thin; the tests are mostly about `accounts.py`, because that
is where the things worth getting wrong are:

- an email stored in a casing `authenticate()` will never look up,
- a developer demoting themselves halfway through something else,
- the last developer being demoted by somebody who did not check,
- a password policy that the CLI enforces and the API does not.

The CLI shares all four and is tested against the same rules, because a rule
one caller skips is not a rule.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pim_studio.admin import accounts
from pim_studio.admin import app as app_module
from pim_studio.admin import users as users_cli
from pim_studio.admin.auth import authenticate, current_user
from pim_studio.db import session_scope
from pim_studio.models import AuditLog, User

pytestmark = pytest.mark.usefixtures("temp_db")

GOOD_PASSWORD = "a-long-enough-one"


def a_user(
    email: str = "dev@example.com",
    *,
    role: str = "developer",
    active: bool = True,
    name: str | None = None,
) -> int:
    with session_scope() as db:
        user = accounts.create_user(
            db, email=email, password=GOOD_PASSWORD, role=role, name=name
        )
        user.is_active = active
        db.flush()
        return user.id


@pytest.fixture
def actor_id() -> int:
    """The signed-in developer every request in this file is made as."""
    return a_user()


@pytest.fixture
def client(actor_id: int) -> TestClient:
    """The app signed in as a developer.

    Only `current_user` is overridden — `require_role` builds on it, so the
    real role check still runs on every request here. That is deliberate:
    stubbing the gate as well would mean these tests never exercise the line
    that keeps `planning` out of this router.
    """

    def as_developer() -> User:
        with session_scope() as db:
            return db.get(User, actor_id)

    app_module.app.dependency_overrides[current_user] = as_developer
    try:
        yield TestClient(app_module.app, raise_server_exceptions=False)
    finally:
        app_module.app.dependency_overrides.clear()


def audit_rows() -> list[AuditLog]:
    with session_scope() as db:
        return list(db.query(AuditLog).order_by(AuditLog.id))


# ── email normalisation ────────────────────────────────────────────────────


def test_a_mixed_case_email_is_stored_lowercase(client: TestClient) -> None:
    """The bug this whole module exists to make unrepresentable.

    `authenticate()` looks the account up as `email.strip().lower()`. Stored
    any other way, the account cannot sign in — and the error it gets is
    "wrong email or password", which reads as a typo forever.
    """
    body = client.post(
        "/api/users",
        json={"email": "  Nowy.User@Example.COM ", "password": GOOD_PASSWORD},
    ).json()

    assert body["email"] == "nowy.user@example.com"

    with session_scope() as db:
        assert authenticate(db, "Nowy.User@Example.COM", GOOD_PASSWORD) is not None


def test_a_duplicate_in_another_casing_is_refused(client: TestClient) -> None:
    client.post("/api/users", json={"email": "a@example.com", "password": GOOD_PASSWORD})
    res = client.post(
        "/api/users", json={"email": "A@EXAMPLE.COM", "password": GOOD_PASSWORD}
    )

    assert res.status_code == 409
    assert "already has an account" in res.json()["detail"]


def test_something_that_is_not_an_address_is_refused(client: TestClient) -> None:
    res = client.post("/api/users", json={"email": "nobody", "password": GOOD_PASSWORD})
    assert res.status_code == 422


# ── password policy ────────────────────────────────────────────────────────


def test_the_api_enforces_the_length_the_cli_does(client: TestClient) -> None:
    """The weakest path decides how strong the policy is. The CLI has always
    refused under ten characters; an API that took six would end that."""
    res = client.post("/api/users", json={"email": "b@example.com", "password": "short"})

    assert res.status_code == 422
    assert str(accounts.MIN_PASSWORD_LENGTH) in res.json()["detail"]


def test_a_reset_changes_what_sign_in_accepts(client: TestClient) -> None:
    user_id = a_user("c@example.com", role="planning")

    res = client.post(
        f"/api/users/{user_id}/password", json={"password": "brand-new-secret"}
    )
    assert res.status_code == 200

    with session_scope() as db:
        assert authenticate(db, "c@example.com", "brand-new-secret") is not None
        assert authenticate(db, "c@example.com", GOOD_PASSWORD) is None


def test_a_password_never_reaches_the_audit_log(client: TestClient) -> None:
    user_id = a_user("d@example.com", role="planning")
    client.post(f"/api/users/{user_id}/password", json={"password": "hunter2-hunter2"})

    written = " ".join(f"{r.action} {r.detail or ''}" for r in audit_rows())
    assert "hunter2" not in written
    assert "d@example.com" in written


# ── the lockout guards ─────────────────────────────────────────────────────


def test_you_cannot_demote_yourself(client: TestClient, actor_id: int) -> None:
    a_user("second@example.com")  # so the last-developer rule isn't what fires

    res = client.patch(f"/api/users/{actor_id}", json={"role": "manager"})

    assert res.status_code == 409
    assert "your own account" in res.json()["detail"]


def test_you_cannot_deactivate_yourself(client: TestClient, actor_id: int) -> None:
    a_user("second@example.com")
    res = client.patch(f"/api/users/{actor_id}", json={"is_active": False})
    assert res.status_code == 409


def test_a_developer_may_be_demoted_while_another_remains(client: TestClient) -> None:
    other = a_user("other@example.com")

    res = client.patch(f"/api/users/{other}", json={"role": "planning"})

    assert res.status_code == 200
    assert res.json()["role"] == "planning"


# The last-developer guard is tested against `accounts.py` rather than through
# the API, because through the API it cannot currently fire: reaching the
# router requires being an active developer, and the self-guard stops that
# developer being their own target, so one is always left. That is a fact about
# a route gate and a separate rule, in two other files, either of which could
# reasonably change — which is exactly why the backstop exists and why it is
# checked where it lives.


def test_the_last_developer_cannot_be_demoted() -> None:
    a_user("only@example.com")

    with session_scope() as db:
        only = db.query(User).one()
        with pytest.raises(accounts.AccountError) as caught:
            accounts.update_user(db, only, actor_id=999, role="planning")

    assert caught.value.status == 409
    assert "last active developer" in str(caught.value)


def test_the_last_developer_cannot_be_deactivated() -> None:
    a_user("only@example.com")

    with session_scope() as db:
        only = db.query(User).one()
        with pytest.raises(accounts.AccountError):
            accounts.update_user(db, only, actor_id=999, is_active=False)


def test_a_disabled_developer_does_not_count_as_cover() -> None:
    """Two developers on the page, one of them disabled, is one developer."""
    a_user("dormant@example.com", active=False)
    a_user("only@example.com")

    with session_scope() as db:
        only = db.scalar(select(User).where(User.email == "only@example.com"))
        with pytest.raises(accounts.AccountError):
            accounts.update_user(db, only, actor_id=999, role="manager")


def test_promoting_the_last_developer_further_is_not_blocked() -> None:
    """The guard is about losing the tier, not about touching the person in
    it. A no-op role write on the only developer must not read as a lockout."""
    a_user("only@example.com")

    with session_scope() as db:
        only = db.query(User).one()
        assert accounts.update_user(db, only, actor_id=999, name="Renamed")


# ── what a change records ──────────────────────────────────────────────────


def test_the_audit_row_says_what_moved(client: TestClient) -> None:
    user_id = a_user("e@example.com", role="manager", name="Old")

    client.patch(f"/api/users/{user_id}", json={"role": "planning", "name": "New"})

    (row,) = [r for r in audit_rows() if r.action == "changed an account"]
    assert "role manager → planning" in row.detail
    assert "name Old → New" in row.detail
    assert row.user_email == "dev@example.com"


def test_a_patch_that_changes_nothing_writes_nothing(client: TestClient) -> None:
    """Otherwise half the log is people opening a form and closing it."""
    user_id = a_user("f@example.com", role="planning")

    res = client.patch(f"/api/users/{user_id}", json={"role": "planning"})

    assert res.status_code == 200
    assert [r for r in audit_rows() if r.action == "changed an account"] == []


def test_an_explicit_null_clears_the_name(client: TestClient) -> None:
    """`{"name": null}` clears it; `{}` leaves it alone. The two have to stay
    distinguishable or a page that patches one field wipes the others."""
    user_id = a_user("g@example.com", role="planning", name="Somebody")

    assert client.patch(f"/api/users/{user_id}", json={}).json()["name"] == "Somebody"
    assert client.patch(f"/api/users/{user_id}", json={"name": None}).json()["name"] is None


# ── the shape on the wire ──────────────────────────────────────────────────


def test_the_hash_is_never_serialised(client: TestClient) -> None:
    a_user("h@example.com", role="planning")
    body = client.get("/api/users").json()

    assert body
    for row in body:
        assert "password_hash" not in row
        assert set(row) == {
            "id", "email", "name", "role", "is_active", "created_at", "last_login_at",
        }


def test_last_seen_is_actually_persisted() -> None:
    """`authenticate` stamps `last_login_at` and flushes, and `get_session`
    does not commit — so until the login endpoint was made to, the stamp was
    rolled back at the end of every request and the column never held a value.

    The Users page reads it. An empty column there would have read as "this
    person has never signed in", which is a statement, not a blank.
    """
    a_user("seen@example.com", role="planning")
    client = TestClient(app_module.app, raise_server_exceptions=False)

    res = client.post(
        "/api/auth/login", json={"email": "seen@example.com", "password": GOOD_PASSWORD}
    )
    assert res.status_code == 200

    with session_scope() as db:
        seen = db.scalar(select(User).where(User.email == "seen@example.com"))
        assert seen.last_login_at is not None


def test_the_sign_in_row_survives_the_request_that_wrote_it() -> None:
    """The other half of the same bug. `audit.record` opens its own session, so
    recording before the request commits is a second connection waiting on the
    first — which on SQLite times out and drops the row silently, exactly the
    way an audit log must never fail."""
    a_user("seen@example.com", role="planning")
    client = TestClient(app_module.app, raise_server_exceptions=False)
    client.post(
        "/api/auth/login", json={"email": "seen@example.com", "password": GOOD_PASSWORD}
    )

    assert [r.action for r in audit_rows()] == ["signed in"]


def test_disabled_accounts_are_listed_not_hidden(client: TestClient) -> None:
    """There is no delete, so deactivation is the only off switch — and an
    account nobody can see is an account nobody reviews."""
    a_user("gone@example.com", role="planning", active=False)

    listed = client.get("/api/users").json()
    emails = [r["email"] for r in listed]

    assert "gone@example.com" in emails
    # Active first, so the disabled rows collect at the bottom.
    assert emails[-1] == "gone@example.com"


def test_there_is_no_delete_endpoint(client: TestClient) -> None:
    """`users` is shared with the price app. Removing a row there takes that
    account with it and orphans every audit row pointing at the id."""
    user_id = a_user("i@example.com", role="planning")

    assert client.delete(f"/api/users/{user_id}").status_code == 405


def test_planning_cannot_reach_the_router() -> None:
    """The sidebar hides these controls from planning. A page that hides what
    the API would have honoured teaches people the wrong thing about their
    own access, so the gate is tested rather than assumed."""
    planning_id = a_user("plan@example.com", role="planning")

    def as_planning() -> User:
        with session_scope() as db:
            return db.get(User, planning_id)

    app_module.app.dependency_overrides[current_user] = as_planning
    try:
        client = TestClient(app_module.app, raise_server_exceptions=False)
        assert client.get("/api/users").status_code == 403
        assert client.get("/api/audit").status_code == 403
    finally:
        app_module.app.dependency_overrides.clear()


# ── the CLI, on the same rules ─────────────────────────────────────────────


def test_the_cli_stores_the_same_lowercase_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(users_cli, "_read_password", lambda confirm=True: GOOD_PASSWORD)

    assert users_cli.main(["add", "CLI.User@Example.COM", "--role", "planning"]) == 0

    with session_scope() as db:
        assert authenticate(db, "cli.user@example.com", GOOD_PASSWORD) is not None


def test_the_cli_refuses_a_short_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(users_cli, "_read_password", lambda confirm=True: "short")

    assert users_cli.main(["add", "j@example.com"]) == 1
    with session_scope() as db:
        assert db.query(User).count() == 0


def test_the_cli_writes_the_same_audit_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accounts are created this way. A page that showed them appearing with
    nobody creating them would be worse than no page."""
    monkeypatch.setattr(users_cli, "_read_password", lambda confirm=True: GOOD_PASSWORD)
    monkeypatch.setenv("USER", "emir")

    users_cli.main(["add", "k@example.com", "--role", "manager"])

    (row,) = audit_rows()
    assert row.action == "created an account"
    assert row.method == "CLI"
    assert row.user_email == "emir (cli)"
    assert "k@example.com as manager" in row.detail


def test_the_cli_walks_past_the_last_developer_guard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The escape hatch, and the reason the guards can be strict: they have to
    be defeatable from somewhere, or a lockout is permanent rather than merely
    inconvenient. Requiring a shell on the server is the right friction."""
    a_user("only@example.com")

    assert users_cli.main(["role", "only@example.com", "planning"]) == 0

    with session_scope() as db:
        assert db.query(User).one().role == "planning"
    assert "no active developer" in capsys.readouterr().err.lower()


def test_the_cli_finds_an_account_stored_in_the_old_casing() -> None:
    """Rows created before normalisation exist. The fix for one of them is
    this CLI, and you cannot fix what you cannot look up."""
    with session_scope() as db:
        db.add(
            User(
                email="Legacy@Example.COM",
                password_hash="x",
                role="planning",
                is_active=True,
            )
        )

    assert users_cli.main(["disable", "legacy@example.com"]) == 0

    with session_scope() as db:
        assert db.query(User).one().is_active is False
