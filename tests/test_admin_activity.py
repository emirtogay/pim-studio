"""The activity log — and the middleware that fills it.

Two things are being tested here, and the second one matters more.

**The endpoint**: filters, facets, pagination, and the edge of the record.

**That anything gets recorded at all.** `audit.py` described a middleware that
records every mutating request from the day it was ported, and no such
middleware existed. The page would have opened, rendered, and shown a tidy
list of sign-ins — a complete-looking record of one kind of event. The tests
below that drive real endpoints and then read the log are the ones that would
have caught that, so they go through the app rather than calling `record`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import accounts, audit
from pim_studio.admin import app as app_module
from pim_studio.db import session_scope
from pim_studio.models import AuditLog

pytestmark = pytest.mark.usefixtures("temp_db")


PASSWORD = "a-long-enough-one"


@pytest.fixture
def developer_id() -> int:
    with session_scope() as db:
        user = accounts.create_user(
            db, email="dev@example.com", password=PASSWORD, role="developer", name="Dev"
        )
        return user.id


@pytest.fixture
def client(developer_id: int) -> TestClient:
    """Signed in for real, with a session cookie, then the log wiped.

    Deliberately not `dependency_overrides[current_user]`, which the rest of
    the admin tests use. Attribution runs inside `current_user` — it is the one
    place a request becomes a person — so a stubbed dependency would leave
    every row anonymous and the tests would pass while agreeing with each other
    about nothing. Half of this file is about what the middleware records
    unprompted, and that only means something against the real path.

    The sign-in writes its own row, so the table is cleared afterwards: each
    test starts with a live session and an empty log.
    """
    del developer_id
    client = TestClient(app_module.app, raise_server_exceptions=False)
    assert client.post(
        "/api/auth/login", json={"email": "dev@example.com", "password": PASSWORD}
    ).status_code == 200
    with session_scope() as db:
        db.query(AuditLog).delete()
    return client


def an_entry(
    action: str = "signed in",
    *,
    email: str = "someone@example.com",
    days_ago: float = 0,
    status_code: int = 200,
) -> None:
    with session_scope() as db:
        db.add(
            AuditLog(
                created_at=datetime.now(UTC) - timedelta(days=days_ago),
                action=action,
                user_email=email,
                method="POST",
                path="/api/auth/login",
                status_code=status_code,
                ip="10.0.0.1",
            )
        )


# ── the middleware ─────────────────────────────────────────────────────────


def test_a_mutation_is_recorded_without_the_endpoint_asking(
    client: TestClient, developer_id: int
) -> None:
    """The gap this release closes. `acknowledge_changelog` writes nothing to
    the log itself, and it changes state, so it must appear anyway."""
    assert client.post("/api/auth/acknowledge_changelog").status_code == 200

    rows = client.get("/api/audit").json()["items"]
    (row,) = rows
    assert row["action"] == "read the release notes"
    assert row["method"] == "POST"
    assert row["status_code"] == 200
    assert row["user_email"] == "dev@example.com"


def test_a_read_is_not_recorded(client: TestClient) -> None:
    client.get("/api/version")
    client.get("/api/users")

    assert client.get("/api/audit").json()["items"] == []


def test_a_read_shaped_post_is_not_recorded(client: TestClient) -> None:
    """`/api/coverage/resolve` mutates nothing and fires while somebody edits a
    box. Recorded, it would bury every row worth reading."""
    client.post("/api/coverage/resolve/text", json={"text": "L3539.132"})

    assert client.get("/api/audit").json()["items"] == []


def test_one_request_records_one_row(client: TestClient) -> None:
    """The endpoint writes a detailed row and claims the request; the
    middleware must then stand down rather than adding a vaguer duplicate."""
    client.post(
        "/api/users", json={"email": "new@example.com", "password": "a-long-enough-one"}
    )

    rows = client.get("/api/audit").json()["items"]
    assert [r["action"] for r in rows] == ["created an account"]
    assert rows[0]["detail"] == "new@example.com as planning"


def test_a_refused_mutation_is_still_recorded(client: TestClient) -> None:
    """A rejected write is often the row somebody came here to find."""
    client.post("/api/users", json={"email": "nope", "password": "short"})

    (row,) = client.get("/api/audit").json()["items"]
    assert row["action"] == "created an account"
    assert row["status_code"] == 422


def test_a_failed_sign_in_records_the_address_as_typed(client: TestClient) -> None:
    """Normalised, `emir@example.com` and `emir@examp1e.com` are one row — and
    the difference between them is why somebody is reading this page."""
    client.post(
        "/api/auth/login", json={"email": "Emir@examp1e.com", "password": "wrong-one"}
    )

    (row,) = client.get("/api/audit").json()["items"]
    assert row["action"] == "sign-in failed"
    assert row["user_email"] == "Emir@examp1e.com"
    assert row["status_code"] == 401


def test_a_sign_out_is_attributed_to_whoever_signed_out(client: TestClient) -> None:
    """The session is cleared during the request, so the id has to be read
    before that — otherwise every sign-out is anonymous."""
    client.post("/api/auth/logout")

    with session_scope() as db:
        (row,) = db.query(AuditLog).all()
    assert row.action == "signed out"
    assert row.user_email == "dev@example.com"


def test_the_static_spa_is_not_recorded(client: TestClient) -> None:
    """Only `/api/` paths. The catch-all serves index.html for every unknown
    route, and a log of page loads is not an audit log."""
    assert audit.should_record("POST", "/products/L3539.132") is False
    assert audit.should_record("GET", "/api/users") is False
    assert audit.should_record("PATCH", "/api/users/3") is True


def test_an_unlabelled_endpoint_still_lands_in_the_log() -> None:
    """Recording is the default and skipping is the exception, so a route
    added later without a friendly label shows up untidy rather than not at
    all — and the untidiness is the prompt to give it one."""
    assert audit.should_record("DELETE", "/api/something/new") is True
    assert audit.friendly_action("DELETE", "/api/something/new") == (
        "DELETE /api/something/new"
    )


# ── filters and facets ─────────────────────────────────────────────────────


def test_rows_come_back_newest_first(client: TestClient) -> None:
    an_entry("signed in", days_ago=3)
    an_entry("sign-in failed", days_ago=1)

    items = client.get("/api/audit").json()["items"]
    assert [r["action"] for r in items] == ["sign-in failed", "signed in"]


def test_filtering_by_action_narrows_the_rows(client: TestClient) -> None:
    an_entry("signed in")
    an_entry("signed in")
    an_entry("sign-in failed")

    body = client.get("/api/audit?action=sign-in failed").json()

    assert body["total"] == 1
    assert [r["action"] for r in body["items"]] == ["sign-in failed"]


def test_filtering_by_user_is_case_insensitive_on_the_query(
    client: TestClient,
) -> None:
    an_entry(email="someone@example.com")
    an_entry(email="other@example.com")

    body = client.get("/api/audit?user_email=Someone@Example.COM").json()
    assert body["total"] == 1


def test_the_facets_survive_the_filter_they_offer(client: TestClient) -> None:
    """Count facets over the filtered rows and picking one leaves a single
    chip standing with no way back. The chips describe the window; the rows
    describe the window plus the filters."""
    an_entry("signed in")
    an_entry("signed in")
    an_entry("sign-in failed")

    body = client.get("/api/audit?action=sign-in failed").json()

    assert body["total"] == 1
    assert {f["action"]: f["count"] for f in body["actions"]} == {
        "signed in": 2,
        "sign-in failed": 1,
    }


def test_the_window_narrows_both_rows_and_facets(client: TestClient) -> None:
    an_entry("signed in", days_ago=40)
    an_entry("sign-in failed", days_ago=1)

    body = client.get("/api/audit?days=7").json()

    assert body["total"] == 1
    assert [f["action"] for f in body["actions"]] == ["sign-in failed"]
    assert body["windowDays"] == 7


def test_the_default_window_is_everything(client: TestClient) -> None:
    """This log is young. A default window would hide most of it, on a page
    whose whole job is showing what happened."""
    an_entry("signed in", days_ago=400)

    body = client.get("/api/audit").json()
    assert body["total"] == 1
    assert body["windowDays"] == 0


def test_the_page_is_told_where_the_record_starts(client: TestClient) -> None:
    """Empty because the filter matched nothing, and empty because the log
    began on Tuesday, look identical. The second is not evidence."""
    an_entry("signed in", days_ago=5)

    body = client.get("/api/audit").json()
    assert body["firstAt"] is not None
    # The oldest row in the table, not in the window — a window is a question
    # about the record, not a fact about it.
    assert client.get("/api/audit?days=1").json()["firstAt"] == body["firstAt"]


def test_an_empty_log_says_so_without_pretending(client: TestClient) -> None:
    body = client.get("/api/audit").json()
    assert body == {
        "total": 0,
        "items": [],
        "actions": [],
        "users": [],
        "windowDays": 0,
        "firstAt": None,
        "accessActions": sorted(audit.ACCESS_ACTIONS),
    }


def test_total_counts_the_match_not_the_page(client: TestClient) -> None:
    for _ in range(5):
        an_entry("signed in")

    body = client.get("/api/audit?limit=2").json()

    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert len(client.get("/api/audit?limit=2&offset=4").json()["items"]) == 1


def test_the_limit_is_capped(client: TestClient) -> None:
    assert client.get("/api/audit?limit=99999").status_code == 422
