"""The runs endpoint — the panel's answer to "is this data actually current?".

The point of this endpoint is that a stale panel and a fresh one look
identical once rendered. Every number PIM Studio shows is as old as the last
sync, and until now there was no way to find out how old that was without a
database client. So the tests here are less about serialisation than about the
two verdicts the page depends on:

- `stale` — nothing has succeeded recently enough.
- `reconciliationOverdue` — things have been succeeding, but only incrementally,
  so nothing has been removed in a long time and the counts can only drift up.

The second is the subtle one. A run of green nights is exactly the state in
which nobody asks the question.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import app as app_module
from pim_studio.admin.auth import current_user
from pim_studio.db import session_scope
from pim_studio.models import Attribute, Product, SyncRun, User, ValueRetirement

pytestmark = pytest.mark.usefixtures("temp_db")


@pytest.fixture
def client() -> TestClient:
    """The app with authentication stubbed out.

    `get_session` is left alone deliberately — it resolves `SessionLocal` at
    call time, which the `temp_db` fixture has already redirected, so the
    endpoint runs against the throwaway database like everything else.
    """
    app_module.app.dependency_overrides[current_user] = lambda: User(
        id=1, email="t@example.com", role="developer", name="T", is_active=True
    )
    try:
        yield TestClient(app_module.app, raise_server_exceptions=False)
    finally:
        app_module.app.dependency_overrides.clear()


def a_run(
    kind: str = "omnitron_catalogue",
    *,
    status: str = "success",
    mode: str = "full",
    hours_ago: float = 1,
    retired: int = 0,
) -> None:
    started = datetime.now(UTC) - timedelta(hours=hours_ago)
    with session_scope() as db:
        db.add(
            SyncRun(
                kind=kind,
                status=status,
                mode=mode,
                started_at=started,
                finished_at=started + timedelta(minutes=5),
                rows_seen=10,
                rows_imported=9,
                rows_dropped=1,
                rows_retired=retired,
            )
        )


def test_a_fresh_full_run_reads_as_current(client: TestClient) -> None:
    a_run(hours_ago=2)
    body = client.get("/api/sync/runs").json()

    (kind,) = body["kinds"]
    assert kind["kind"] == "omnitron_catalogue"
    assert kind["stale"] is False
    assert kind["lastMode"] == "full"

    # The catalogue is reconciled. Liveness is still listed as overdue with
    # `days: None`, and that is correct rather than noise — a kind that has
    # never had a full pass is the strongest version of overdue, not an
    # absence of information. It is also the true state of the real database.
    overdue = {o["kind"]: o["days"] for o in body["reconciliationOverdue"]}
    assert "omnitron_catalogue" not in overdue
    assert overdue == {"omnitron_liveness": None}


def test_a_sync_that_stopped_days_ago_is_flagged_stale(client: TestClient) -> None:
    a_run(hours_ago=72)
    body = client.get("/api/sync/runs").json()

    (kind,) = body["kinds"]
    assert kind["stale"] is True
    assert kind["ageHours"] > body["staleAfterHours"]


def test_green_incremental_nights_still_report_reconciliation_overdue(
    client: TestClient,
) -> None:
    """The failure this endpoint exists to catch.

    Nothing here is stale and nothing has failed — yet no full pass has run in
    a fortnight, so departures and retirements have been accumulating unseen
    and the live counts can only have drifted upward.
    """
    a_run(mode="full", hours_ago=24 * 20)
    for night in range(1, 6):
        a_run(mode="incremental", hours_ago=24 * night)

    body = client.get("/api/sync/runs").json()
    (kind,) = body["kinds"]

    assert kind["stale"] is False, "recent runs, so not stale"
    assert kind["lastMode"] == "incremental"
    overdue = {o["kind"]: o["days"] for o in body["reconciliationOverdue"]}
    assert overdue["omnitron_catalogue"] >= 9
    # liveness has never run at all, which is worse than overdue.
    assert overdue["omnitron_liveness"] is None


def test_a_failed_run_is_visible_with_its_error(client: TestClient) -> None:
    a_run(status="failed", hours_ago=1)
    with session_scope() as db:
        row = db.query(SyncRun).one()
        row.error_summary = "WinError 10054"

    body = client.get("/api/sync/runs").json()
    (run,) = body["runs"]
    assert run["status"] == "failed"
    assert run["errorSummary"] == "WinError 10054"
    assert body["kinds"][0]["stale"] is True, "a failure is not a success"


def test_retirements_are_reported_separately_from_drops(client: TestClient) -> None:
    a_run(retired=40_000)
    (run,) = client.get("/api/sync/runs").json()["runs"]
    assert run["rowsDropped"] == 1
    assert run["rowsRetired"] == 40_000


def test_a_run_that_never_finished_is_surfaced(client: TestClient) -> None:
    """A `running` row long after the fact is a process that died silently."""
    a_run(status="running", hours_ago=30)
    body = client.get("/api/sync/runs").json()
    assert [u["kind"] for u in body["unfinished"]] == ["omnitron_catalogue"]


def test_the_endpoint_needs_a_session() -> None:
    """Same gate as every other endpoint here — no anonymous ops data."""
    anon = TestClient(app_module.app, raise_server_exceptions=False)
    assert anon.get("/api/sync/runs").status_code == 401


def test_a_push_nobody_has_started_for_days_is_not_stale(client: TestClient) -> None:
    """Its age is how long since somebody last pushed, not a sync that stopped.

    `staleAfterHours` is null rather than the default, so the page has nothing
    to print an "over 36h old" against.
    """
    a_run(kind="omnitron_push", hours_ago=72)
    (kind,) = client.get("/api/sync/runs").json()["kinds"]

    assert kind["stale"] is False
    assert kind["staleAfterHours"] is None


def test_a_push_that_only_ever_failed_is_reported_by_its_status_not_as_stale(
    client: TestClient,
) -> None:
    """For a scheduled kind, never having succeeded is stale at any age. For
    the push it is a failed push: the card's status says so, and the Imports
    page holds the batch. It is not a sign that the panel's data is old."""
    a_run(kind="omnitron_push", status="failed", hours_ago=1)
    (kind,) = client.get("/api/sync/runs").json()["kinds"]

    assert kind["lastStatus"] == "failed"
    assert kind["lastSuccessAt"] is None
    assert kind["stale"] is False


# ── what went dark ─────────────────────────────────────────────────────────


def _retirement(sku: str, attr_id: int, market: str, value: str, run_id: int = 1) -> None:
    with session_scope() as db:
        db.add(
            ValueRetirement(
                entity_kind="sku", entity_key=sku, attribute_id=attr_id,
                market=market, last_value=value, retired_at=datetime.now(UTC),
                sync_run_id=run_id, reason=f"{market}: no longer priced or stocked",
            )
        )


def _seed_product_and_attrs() -> dict[str, int]:
    with session_scope() as db:
        ids = {}
        for key in ("price", "stock"):
            a = Attribute(
                key=key, label=key, data_type="number", write_channel="read_only",
                value_grain="sku", per_market=True, long_text=False,
                gates_go_live=False, display_group="live", display_order=1,
            )
            db.add(a)
            db.flush()
            ids[key] = a.id
        db.add(Product(sku="s11", base_code_format="L3539.132.T2",
                       model_color="L3539.132", base_code="L3539"))
        return ids


def test_retirements_group_by_product_not_by_row(client: TestClient) -> None:
    """One product losing stock in four markets is one thing to look at."""
    ids = _seed_product_and_attrs()
    for market in ("PL", "CZ", "SK", "HU"):
        _retirement("s11", ids["stock"], market, "1")

    body = client.get("/api/sync/retirements").json()

    assert body["total"] == 4
    (product,) = body["products"]
    assert product["modelColor"] == "L3539.132"
    assert product["markets"] == ["CZ", "HU", "PL", "SK"]
    assert product["fields"] == ["stock"]


def test_a_retirement_carries_the_value_that_was_lost(client: TestClient) -> None:
    """'Lost its PL price' and 'lost its PL price, which was 349.00' are
    different amounts of help to whoever picks this up."""
    ids = _seed_product_and_attrs()
    _retirement("s11", ids["price"], "PL", "349.00")

    (product,) = client.get("/api/sync/retirements").json()["products"]
    assert product["lastValues"]["PL price"] == "349.00"
    assert product["syncRunId"] == 1


def test_a_retirement_survives_a_product_we_no_longer_hold(client: TestClient) -> None:
    """It still happened. Keying on the sku keeps it visible rather than
    dropping it for want of a join."""
    ids = _seed_product_and_attrs()
    _retirement("gone-sku", ids["stock"], "PL", "3")

    products = client.get("/api/sync/retirements").json()["products"]
    orphan = [p for p in products if p["sku"] == "gone-sku"]
    assert len(orphan) == 1
    assert orphan[0]["modelColor"] is None


def test_no_retirements_is_an_empty_list_not_an_error(client: TestClient) -> None:
    body = client.get("/api/sync/retirements").json()
    assert body == {"total": 0, "shown": 0, "products": []}
