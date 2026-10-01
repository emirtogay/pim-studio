"""The takedown endpoints — and mostly, what they tell the person deciding.

The dialog these feed is the only place somebody learns that taking a product
off Poland also takes it off Czechia. If the payload is wrong, the sentence
explaining the constraint is wrong, and it is wrong in the confident voice of
a system that knows. So what is pinned here is the content of the answer, not
just its shape.

Every fixture overrides `current_user` and nothing below it. `require_role`
delegates to `current_user`, so the real role check still runs — stubbing the
role dependency itself would make the permission tests assert their own
stub rather than the app.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import app as app_module
from pim_studio.admin.auth import (
    ROLE_DEVELOPER,
    ROLE_MANAGER,
    ROLE_PLANNING,
    current_user,
)
from pim_studio.models import User

pytestmark = pytest.mark.usefixtures("temp_db")


def signed_in_as(role: str) -> TestClient:
    app_module.app.dependency_overrides[current_user] = lambda: User(
        id=1, email=f"{role}@example.com", role=role, is_active=True
    )
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def no_omnitron(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs as if Omnitron were not configured.

    CI has no `.env.local`, and a developer machine usually does. A test that
    passes only when credentials happen to be present is testing the machine,
    not the code: the PL-alone test passed locally for a day and failed on CI,
    because the endpoint built its Omnitron client before it looked at the
    group. Cleared, both environments agree — and an endpoint that needs
    Omnitron in order to reject a bad request shows up here as a 503 on every
    run, not only on CI.
    """
    from pim_studio.config import settings

    for field in ("omnitron_base_url", "omnitron_username", "omnitron_password"):
        monkeypatch.setattr(settings, field, "")


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield signed_in_as(ROLE_DEVELOPER)
    app_module.app.dependency_overrides.clear()


@pytest.fixture
def _cleanup() -> Iterator[None]:
    yield
    app_module.app.dependency_overrides.clear()


def groups(client: TestClient) -> dict[str, dict]:
    body = client.get("/api/takedown/groups")
    assert body.status_code == 200, body.text
    return {g["key"]: g for g in body.json()["groups"]}


# ── what the dialog is told ────────────────────────────────────────────────


def test_the_groups_are_the_one_the_tenant_actually_allows(client) -> None:
    """One since 2026-09-16, when HU and SK joined PL and CZ on catalogue 601."""
    assert sorted(groups(client)) == ["PL+CZ+HU+SK"]


def test_the_shared_catalogue_is_the_shared_one_and_not_all_of_them(client) -> None:
    """The dialog says the markets are "all served by catalogue
    {sharedCatalogs}". It said "4, 40, 601" once, which is false: 4 is PL's own
    and 40 is CZ's own. Only 601 is shared, and only 601 is the reason the
    markets cannot be separated.
    """
    g = groups(client)["PL+CZ+HU+SK"]
    assert g["sharedCatalogs"] == [601]
    assert sorted(g["catalogs"]) == [4, 40, 41, 172, 601], (
        "every one is touched by the delete — a different list, deliberately"
    )


def test_the_group_carries_the_markets_it_will_take_offline(client) -> None:
    g = groups(client)["PL+CZ+HU+SK"]
    assert g["markets"] == ["PL", "CZ", "HU", "SK"]
    assert g["shared"] is True
    assert g["label"] == "PL + CZ + HU + SK"
    assert g["names"] == [
        "Poland Northwind", "Czech Northwind", "Hungary Northwind", "Northwind SK"]


def test_both_switches_are_reported_so_the_button_can_explain(client, monkeypatch) -> None:
    """Rather than letting somebody fill the form and fail at the end."""
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_takedown_enabled", False)
    assert client.get("/api/takedown/groups").json()["enabled"] is False

    monkeypatch.setattr(settings, "omnitron_takedown_enabled", True)
    assert client.get("/api/takedown/groups").json()["enabled"] is True


# ── refusals ───────────────────────────────────────────────────────────────


def test_the_confirm_phrase_is_checked_before_omnitron_is_touched(client) -> None:
    """A wrong phrase must fail on our side. Reaching Omnitron first would mean
    a typo costs a catalogue read, and worse, that the ordering is an accident
    rather than a rule.
    """
    r = client.post(
        "/api/takedown/L3539.132/offline",
        json={"group": "PL+CZ+HU+SK", "confirm": "yes"},
    )
    assert r.status_code == 400
    assert "take offline" in r.json()["detail"]


def test_planning_a_market_that_cannot_stand_alone_says_why(client) -> None:
    r = client.get("/api/takedown/L3539.132/plan", params={"group": "PL"})
    assert r.status_code == 400
    assert "CZ" in r.json()["detail"]


def test_taking_poland_offline_alone_is_refused_before_omnitron(client) -> None:
    """The same order on the write: a group that cannot exist is a 400 on its
    face, whether or not there is an Omnitron to ask."""
    r = client.post(
        "/api/takedown/L3539.132/offline",
        json={"group": "PL", "confirm": "take offline"},
    )
    assert r.status_code == 400
    assert "CZ" in r.json()["detail"]


def test_planning_group_is_required(client) -> None:
    """There is no default here that would be safe to pick."""
    assert client.get("/api/takedown/L3539.132/plan").status_code == 422


# ── who may ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", [ROLE_PLANNING, ROLE_MANAGER])
@pytest.mark.usefixtures("_cleanup")
def test_only_a_developer_reaches_any_of_it(role: str) -> None:
    """Planning may set values; neither role may take a product off sale.

    The real `require_role` runs here — only `current_user` is overridden — so
    this asserts the app's permission check rather than a stub of it.
    """
    c = signed_in_as(role)
    assert c.get("/api/takedown/groups").status_code == 403
    assert (
        c.post(
            "/api/takedown/L3539.132/offline",
            json={"group": "PL+CZ+HU+SK", "confirm": "take offline"},
        ).status_code
        == 403
    )
    assert c.post("/api/takedown/restore/1").status_code == 403


@pytest.mark.usefixtures("_cleanup")
def test_a_developer_does_reach_it() -> None:
    """The counterpart the parametrised refusal needs: without this, a route
    that 403s everybody would pass the test above and look correct."""
    assert signed_in_as(ROLE_DEVELOPER).get("/api/takedown/groups").status_code == 200
