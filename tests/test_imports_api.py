"""Setting a value from the panel, and reading the option's live cells back.

Two properties are defended. A value set here is `approved` and carries the
person's name — in the row, in `value_changes`, and in the audit log — so the
sync cannot overwrite it and the page can say who did it. And the registry, not
the endpoint, decides what may be set: a partner-owned field, a tenant-wide
one, and a market with no write target are all refused with the reason.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pim_studio.admin import app as app_module
from pim_studio.admin.auth import current_user
from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeColumn,
    AttributeValue,
    OmnitronAttribute,
    Product,
    User,
    ValueChange,
)

pytestmark = pytest.mark.usefixtures("temp_db")


def _as(role: str) -> None:
    app_module.app.dependency_overrides[current_user] = lambda: User(
        id=7, email="planner@example.com", role=role, name="P", is_active=True
    )


@pytest.fixture
def client() -> TestClient:
    _as("planning")
    try:
        yield TestClient(app_module.app, raise_server_exceptions=False)
    finally:
        app_module.app.dependency_overrides.clear()


def attribute(key: str, channel: str = "omnitron_import", grain: str = "model_color") -> int:
    with session_scope() as db:
        row = Attribute(
            key=key, label=key.replace("_", " ").capitalize(), write_channel=channel,
            value_grain=grain, per_market=True,
        )
        db.add(row)
        db.flush()
        return row.id


def column(attr_id: int, name: str, market: str | None, *, writable: bool) -> None:
    with session_scope() as db:
        db.add(
            AttributeColumn(
                attribute_id=attr_id, source="omnitron", column_name=name,
                market=market, api_writable=writable,
            )
        )
        key = name.split("__")[-1]
        db.add(
            OmnitronAttribute(
                pk=abs(hash(name)) % 90_000 + 1, key=key, name=key, data_type="text",
                is_localizable=name.startswith("attributes__urun_adi"),
            )
        )


@pytest.fixture
def seeded() -> dict[str, int]:
    name = attribute("product_name")
    column(name, "attributes__urun_adi_pl", "PL", writable=True)
    column(name, "attributes__urun_adi_cz", "CZ", writable=True)
    sleeve = attribute("sleeve_type", channel="product_card")
    column(sleeve, "attributes_kwargs__integration_kol_boyu__label", None, writable=False)
    with session_scope() as db:
        for size in ("T2", "T3"):
            db.add(
                Product(
                    sku=f"L3558.132.{size}", base_code_format=f"L3558.132.{size}",
                    model_color="L3558.132", base_code="L3558", size=size,
                )
            )
    return {"name": name, "sleeve": sleeve}


def set_pl_name(client: TestClient, value: str, market: str = "PL"):
    return client.post(
        "/api/imports/values",
        json={"modelColor": "L3558.132", "attribute": "product_name",
              "market": market, "value": value},
    )


def test_a_set_value_is_approved_and_carries_the_persons_name(client, seeded) -> None:
    res = set_pl_name(client, "  Koszulka polo N.10.10  ")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["written"] == 1
    assert body["row"]["state"] == "approved"
    assert body["row"]["value"] == "Koszulka polo N.10.10"
    assert body["row"]["updatedBy"] == "planner@example.com"

    with session_scope() as db:
        row = db.scalar(select(AttributeValue))
        assert row is not None
        assert (row.state, row.source_kind, row.entry_grain) == (
            "approved", "human", "model_color",
        )
        assert row.market == "PL" and row.entity_kind == "model_color"


def test_changing_an_approved_value_again_leaves_history_with_a_name(client, seeded) -> None:
    assert set_pl_name(client, "first").status_code == 200
    res = set_pl_name(client, "second")
    assert res.status_code == 200
    assert res.json()["written"] == 1

    with session_scope() as db:
        # The first write had no "before", so exactly one change is recorded.
        changes = db.scalars(select(ValueChange)).all()
        assert len(changes) == 1
        assert (changes[0].old_value, changes[0].new_value) == ("first", "second")
        assert changes[0].changed_by == "planner@example.com"
        assert db.scalar(select(func.count()).select_from(AttributeValue)) == 1


def test_setting_the_same_value_twice_is_unchanged_not_rewritten(client, seeded) -> None:
    set_pl_name(client, "same")
    res = set_pl_name(client, "same")
    assert res.status_code == 200
    assert (res.json()["written"], res.json()["unchanged"]) == (0, 1)


def test_a_partner_owned_attribute_is_refused_with_the_reason(client, seeded) -> None:
    res = client.post(
        "/api/imports/values",
        json={"modelColor": "L3558.132", "attribute": "sleeve_type",
              "market": "*", "value": "LONG SLEEVES"},
    )
    assert res.status_code == 422
    assert "Turkey team" in res.json()["detail"]
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AttributeValue)) == 0


def test_a_market_with_no_write_target_is_refused(client, seeded) -> None:
    res = set_pl_name(client, "Meno", market="SK")
    assert res.status_code == 422
    assert "market SK" in res.json()["detail"]


def test_an_empty_value_is_refused(client, seeded) -> None:
    res = set_pl_name(client, "   ")
    assert res.status_code == 422
    assert "Clearing" in res.json()["detail"]


def test_an_unknown_product_is_refused(client, seeded) -> None:
    res = client.post(
        "/api/imports/values",
        json={"modelColor": "ZZ0960.000", "attribute": "product_name",
              "market": "PL", "value": "x"},
    )
    assert res.status_code == 404


def test_a_manager_may_look_but_not_set(seeded) -> None:
    _as("manager")
    try:
        client = TestClient(app_module.app, raise_server_exceptions=False)
        assert set_pl_name(client, "x").status_code == 403
        assert client.get("/api/products/L3558.132/values").status_code == 200
    finally:
        app_module.app.dependency_overrides.clear()


def test_the_overlay_reports_the_cells_and_which_may_be_edited(client, seeded) -> None:
    set_pl_name(client, "Koszulka")
    with session_scope() as db:
        db.add(
            AttributeValue(
                entity_kind="model_color", entity_key="L3558.132",
                attribute_id=seeded["name"], market="CZ", state="live",
                entry_grain="model_color", source_kind="omnitron_api", value="Polokosile",
            )
        )
    res = client.get("/api/products/L3558.132/values")
    assert res.status_code == 200
    body = res.json()
    rows = {(r["attribute"], r["market"]): r for r in body["rows"]}
    assert rows[("product_name", "PL")]["state"] == "approved"
    assert rows[("product_name", "PL")]["updatedBy"] == "planner@example.com"
    assert rows[("product_name", "CZ")]["state"] == "live"
    assert sorted((w["attribute"], w["market"]) for w in body["writable"]) == [
        ("product_name", "CZ"), ("product_name", "PL"),
    ]
    assert body["writesEnabled"] is False


def test_a_conflict_decision_now_leaves_history_too(client, seeded) -> None:
    with session_scope() as db:
        db.add(
            AttributeValue(
                entity_kind="model_color", entity_key="L3558.132",
                attribute_id=seeded["name"], market="PL", state="live",
                entry_grain="model_color", source_kind="omnitron_api", value="Generic",
            )
        )
    res = client.post(
        "/api/conflicts/decide",
        json={"modelColor": "L3558.132", "attribute": "product_name",
              "market": "PL", "value": "Real copy"},
    )
    assert res.status_code == 200, res.text
    with session_scope() as db:
        change = db.scalar(select(ValueChange))
        assert change is not None
        assert (change.old_value, change.new_value, change.changed_by) == (
            "Generic", "Real copy", "planner@example.com",
        )


# ── pushing from the panel ─────────────────────────────────────────────────


def _writes_on(monkeypatch) -> None:
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_write_mode", "merge")
    monkeypatch.setattr(settings, "omnitron_base_url", "https://pim.example.test")
    monkeypatch.setattr(settings, "omnitron_username", "u")
    monkeypatch.setattr(settings, "omnitron_password", "p")


class _QuietClient:
    """Answers the preflight read and nothing else."""

    def __init__(self, *a, **kw) -> None:
        self.closed = False

    def get(self, path, params=None, locale=None):
        return {"count": 0, "next": None, "results": []}

    def close(self) -> None:
        self.closed = True


def test_pending_lists_the_queue_and_says_why_a_push_cannot_start(client, seeded) -> None:
    set_pl_name(client, "Koszulka")
    res = client.get("/api/imports/pending")
    assert res.status_code == 200
    body = res.json()
    assert body["approvedCount"] == 1
    assert body["itemCount"] == 2  # two sizes
    assert body["approved"][0]["attribute"] == "product_name"
    assert body["approved"][0]["sizes"] == 2
    assert body["canPush"] is False
    assert "switched off" in body["reason"]
    assert body["exported"] == []


def test_a_push_is_refused_with_the_reason_while_writes_are_off(client, seeded) -> None:
    from pim_studio.models import PushBatch

    set_pl_name(client, "Koszulka")
    res = client.post("/api/imports/push", json={})
    assert res.status_code == 409
    assert "switched off" in res.json()["detail"]
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(PushBatch)) == 0


def test_a_push_builds_a_batch_and_hands_it_to_the_thread(client, seeded, monkeypatch) -> None:
    from pim_studio.admin import imports_api
    from pim_studio.models import PushBatch

    _writes_on(monkeypatch)
    handed: list[tuple] = []

    def fake_thread(target, args, name, daemon):
        class T:
            def start(self_inner):
                handed.append(args)
                # Release what the request handed over, as the real thread would.
                args[3].release()
                args[2].close()

        return T()

    monkeypatch.setattr(imports_api, "OmnitronClient", _QuietClient)
    monkeypatch.setattr(imports_api.threading, "Thread", fake_thread)
    set_pl_name(client, "Koszulka")

    res = client.post("/api/imports/push", json={"note": "first"})
    assert res.status_code == 202, res.text
    body = res.json()
    assert (body["items"], body["skus"], body["products"]) == (2, 2, 1)
    assert len(handed) == 1 and handed[0][1] == "planner@example.com"

    listed = client.get("/api/imports/batches").json()["rows"]
    assert listed[0]["id"] == body["batchId"]
    assert listed[0]["status"] == "pending" and listed[0]["note"] == "first"
    detail = client.get(f"/api/imports/batches/{body['batchId']}").json()
    assert {i["sku"] for i in detail["items"]} == {"L3558.132.T2", "L3558.132.T3"}
    assert detail["items"][0]["key"] == "urun_adi_pl" and detail["items"][0]["locale"] == "pl"

    # A second push while the first is pending is refused, not stacked.
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(PushBatch)) == 1
    again = client.post("/api/imports/push", json={})
    assert again.status_code == 409 and "pending" in again.json()["detail"]


def test_a_push_that_cannot_reach_omnitron_fails_its_batch_and_says_so(
    client, seeded, monkeypatch
) -> None:
    from pim_studio.admin import imports_api
    from pim_studio.models import PushBatch
    from pim_studio.omnitron.client import OmnitronError

    _writes_on(monkeypatch)

    class Dead(_QuietClient):
        def get(self, path, params=None, locale=None):
            raise OmnitronError("connection refused")

    monkeypatch.setattr(imports_api, "OmnitronClient", Dead)
    set_pl_name(client, "Koszulka")
    res = client.post("/api/imports/push", json={})
    assert res.status_code == 502
    assert "did not answer" in res.json()["detail"]
    with session_scope() as db:
        batch = db.scalar(select(PushBatch))
        assert batch is not None and batch.status == "failed"
        assert "preflight" in (batch.error_summary or "")
    # And the failed batch does not block the next attempt.
    assert client.get("/api/imports/pending").json()["openBatch"] is None


# ── many values: preview, apply, upload ────────────────────────────────────


def _edit(mc: str, attribute: str, market: str, value: str) -> dict:
    return {"modelColor": mc, "attribute": attribute, "market": market, "value": value}


def test_a_preview_flags_each_row_and_writes_nothing(client, seeded) -> None:
    set_pl_name(client, "Koszulka")
    res = client.post(
        "/api/imports/preview",
        json={"edits": [
            _edit("L3558.132", "product_name", "PL", "Koszulka"),        # unchanged
            _edit("L3558.132", "product_name", "CZ", "Polokosile"),       # fine
            _edit("L3558.132", "product_name", "SK", "Polokosela"),       # no target
            _edit("L3558.132", "sleeve_type", "*", "LONG SLEEVES"),       # partner's
            _edit("ZZ0960.000", "product_name", "PL", "x"),               # unknown product
            _edit("L3558.132", "product_name", "CZ", "   "),              # empty
            _edit("L3558.132", "product_name", "HU", "Classic Fit Long Sleeved Polo Shirt"),
        ]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    flags = [set(r["flags"]) for r in body["rows"]]
    assert flags[0] == {"unchanged"}
    assert flags[1] == set() and body["rows"][1]["applicable"] is True
    assert flags[2] == {"not_writable"}
    assert flags[3] == {"not_writable"}
    assert flags[4] == {"unknown_product"}
    assert flags[5] == {"empty"}
    assert "language_suspect" in flags[6] and "not_writable" in flags[6]
    assert body["counts"]["applicable"] == 1
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AttributeValue)) == 1


def test_apply_writes_only_the_applicable_rows_as_approved(client, seeded) -> None:
    res = client.post(
        "/api/imports/apply",
        json={"edits": [
            _edit("L3558.132", "product_name", "PL", "Koszulka"),
            _edit("L3558.132", "product_name", "CZ", "Polokosile"),
            _edit("L3558.132", "sleeve_type", "*", "LONG SLEEVES"),
            _edit("ZZ0960.000", "product_name", "PL", "x"),
        ]},
    )
    assert res.status_code == 200, res.text
    assert (res.json()["applied"], res.json()["skipped"]) == (2, 2)
    with session_scope() as db:
        rows = db.scalars(select(AttributeValue).order_by(AttributeValue.market)).all()
        assert [(r.market, r.value, r.state, r.updated_by) for r in rows] == [
            ("CZ", "Polokosile", "approved", "planner@example.com"),
            ("PL", "Koszulka", "approved", "planner@example.com"),
        ]


def test_an_uploaded_sheet_becomes_a_preview_with_its_provenance(client, seeded) -> None:
    import io

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Values"
    sheet.append(["Values"])
    sheet.append(["read at …"])
    sheet.append(["ModelColor", "Name", "Product name · PL", "Product name · CZ", "Nazwa · PL"])
    sheet.append(["L3558.132", "Polo", "Koszulka", "Polokosile", "ignored"])
    sheet.append(["ZZ0960.000", "Polo", "x", "", ""])
    out = io.BytesIO()
    book.save(out)

    res = client.post(
        "/api/imports/upload",
        files={"file": ("values.xlsx", out.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["file"]["sheet"] == "Values" and body["file"]["headerRow"] == 3
    assert body["file"]["unknownColumns"] == ["Nazwa · PL"]
    assert body["file"]["columns"] == ["product_name · PL", "product_name · CZ"]
    by = {(r["modelColor"], r["market"]): r for r in body["rows"]}
    assert by[("L3558.132", "PL")]["applicable"] is True
    assert by[("L3558.132", "CZ")]["applicable"] is True
    assert by[("ZZ0960.000", "PL")]["flags"] == ["unknown_product"]
    # Nothing was written by the upload itself.
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AttributeValue)) == 0


def test_an_unreadable_upload_says_why(client, seeded) -> None:
    res = client.post("/api/imports/upload", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert res.status_code == 400
    assert ".xlsx or .csv" in res.json()["detail"]


def test_targets_lists_what_the_grid_may_offer(client, seeded) -> None:
    body = client.get("/api/imports/targets").json()
    assert body["attributes"] == [
        {"key": "product_name", "label": "Product name", "perMarket": True,
         "longText": False, "markets": ["CZ", "PL"]},
    ]


# ── choosing what goes out, and taking something out of the queue ─────────


def test_a_push_can_be_scoped_to_the_cells_that_were_ticked(
    client, seeded, monkeypatch
) -> None:
    from pim_studio.admin import imports_api
    from pim_studio.models import PushItem

    _writes_on(monkeypatch)
    monkeypatch.setattr(imports_api, "OmnitronClient", _QuietClient)
    monkeypatch.setattr(
        imports_api.threading, "Thread",
        lambda target, args, name, daemon: type(
            "T", (), {"start": lambda self_inner: (args[3].release(), args[2].close())}
        )(),
    )
    set_pl_name(client, "Koszulka")
    set_pl_name(client, "Polokosile", market="CZ")

    res = client.post(
        "/api/imports/push",
        json={"values": [
            {"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"}
        ]},
    )
    assert res.status_code == 202, res.text
    # Two sizes of the one ticked cell, not the four the whole queue would give.
    assert res.json()["items"] == 2
    with session_scope() as db:
        markets = {i.market for i in db.scalars(select(PushItem))}
    assert markets == {"PL"}


def test_an_empty_selection_pushes_nothing_rather_than_everything(
    client, seeded, monkeypatch
) -> None:
    """`[]` and absent must not mean the same thing, or a page that sends its
    selection would push the whole queue the moment somebody unticked it all."""
    _writes_on(monkeypatch)
    set_pl_name(client, "Koszulka")
    res = client.post("/api/imports/push", json={"values": []})
    assert res.status_code == 422
    assert "selected" in res.json()["detail"]


def test_discarding_removes_the_value_and_records_who_did_it(client, seeded) -> None:
    set_pl_name(client, "Koszulka")
    res = client.post(
        "/api/imports/discard",
        json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["discarded"] == "Koszulka"

    with session_scope() as db:
        # Deleted, not flagged — an absent row is the absence.
        assert db.scalar(select(func.count()).select_from(AttributeValue)) == 0
        change = db.scalars(select(ValueChange)).all()[-1]
        assert (change.old_value, change.new_value) == ("Koszulka", None)
        assert change.changed_by == "planner@example.com"
    assert client.get("/api/imports/pending").json()["approvedCount"] == 0


def test_only_an_approved_value_can_be_discarded(client, seeded) -> None:
    """A live value belongs to the sync; an exported one has already been
    written to Omnitron and dropping it would lose the only trace of that."""
    for state in ("live", "exported"):
        with session_scope() as db:
            db.query(AttributeValue).delete()
            db.add(AttributeValue(
                entity_kind="model_color", entity_key="L3558.132",
                attribute_id=seeded["name"], market="PL", state=state,
                entry_grain="model_color", source_kind="omnitron_api", value="x",
            ))
        res = client.post(
            "/api/imports/discard",
            json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"},
        )
        assert res.status_code == 422, state
        assert state in res.json()["detail"]
        with session_scope() as db:
            assert db.scalar(select(func.count()).select_from(AttributeValue)) == 1


def test_discarding_something_that_is_not_there_is_a_404(client, seeded) -> None:
    res = client.post(
        "/api/imports/discard",
        json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"},
    )
    assert res.status_code == 404


# ── proposed values ──────────────────────────────────────────────────
#
# A proposal is a suggestion beside the live value, never over it. Accepting is
# the same act as typing a value: it becomes `approved` and joins the push
# queue with the person's name on it, not the source's.


def a_proposal(attr_id: int, value: str, market: str = "PL", why: str = "reads as English"):
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="model_color", entity_key="L3558.132", attribute_id=attr_id,
            market=market, state="proposed", state_lane="proposal",
            entry_grain="model_color", source_kind="proposal",
            source_ref="sibling:english", value=value,
            suspect_flag="EN?", suspect_reason=why,
        ))


def test_a_waiting_proposal_shows_up_beside_the_value(client, seeded) -> None:
    a_proposal(seeded["name"], "Koszulka polo")
    body = client.get("/api/products/L3558.132/values").json()
    (row,) = body["proposals"]
    assert row["attribute"] == "product_name"
    assert row["market"] == "PL"
    assert row["value"] == "Koszulka polo"
    assert row["source"] == "sibling"
    assert row["flag"] == "EN?"


def test_accepting_a_proposal_makes_it_the_persons_approved_value(client, seeded) -> None:
    a_proposal(seeded["name"], "Koszulka polo")
    res = client.post(
        "/api/imports/proposals/accept",
        json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "value": "Koszulka polo", "edited": False}

    with session_scope() as db:
        rows = db.scalars(select(AttributeValue)).all()
    # The proposal is gone and one approved value stands in its place.
    assert [(r.state_lane, r.state) for r in rows] == [("current", "approved")]
    assert rows[0].value == "Koszulka polo"
    assert rows[0].updated_by == "planner@example.com"
    assert client.get("/api/imports/pending").json()["approvedCount"] == 1


def test_a_proposal_can_be_edited_before_it_is_accepted(client, seeded) -> None:
    a_proposal(seeded["name"], "Koszulka polo")
    res = client.post(
        "/api/imports/proposals/accept",
        json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL",
              "value": "  Koszulka polo klasyczna  "},
    )
    assert res.status_code == 200, res.text
    assert res.json()["value"] == "Koszulka polo klasyczna"
    assert res.json()["edited"] is True
    with session_scope() as db:
        (row,) = db.scalars(select(AttributeValue)).all()
    assert row.value == "Koszulka polo klasyczna"


def test_rejecting_a_proposal_leaves_the_live_value_alone(client, seeded) -> None:
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="model_color", entity_key="L3558.132",
            attribute_id=seeded["name"], market="PL", state="live",
            state_lane="current", entry_grain="model_color",
            source_kind="omnitron_api", value="An English name nobody translated",
        ))
    a_proposal(seeded["name"], "Koszulka polo")

    res = client.post(
        "/api/imports/proposals/reject",
        json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["rejected"] == "Koszulka polo"

    with session_scope() as db:
        rows = db.scalars(select(AttributeValue)).all()
    assert [(r.state_lane, r.value) for r in rows] == [
        ("current", "An English name nobody translated")
    ]
    # Nothing changed about the product, so nothing is written to its history.
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(ValueChange)) == 0


def test_acting_on_a_proposal_that_is_not_there_is_a_404(client, seeded) -> None:
    for action in ("accept", "reject"):
        res = client.post(
            f"/api/imports/proposals/{action}",
            json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"},
        )
        assert res.status_code == 404, res.text
        assert "No proposal waiting" in res.text


def test_a_proposal_for_an_unwritable_field_cannot_be_accepted(client, seeded) -> None:
    """The garment dropdowns are tenant-wide. A proposal should never exist for
    one, and accepting it must be refused even if it does."""
    a_proposal(seeded["sleeve"], "Długi rękaw")
    res = client.post(
        "/api/imports/proposals/accept",
        json={"modelColor": "L3558.132", "attribute": "sleeve_type", "market": "PL"},
    )
    assert res.status_code == 422, res.text


# ── the list that makes the proposals findable ─────────────────────────────
#
# Shipped without one, and therefore unusable: a suggestion was only visible
# if you already had the right product open, and there are thousands of them
# across a hundred thousand options.


def test_the_list_pairs_each_proposal_with_the_value_it_would_replace(client, seeded) -> None:
    """A suggestion without the text it replaces is not reviewable — that
    comparison is the whole decision."""
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="model_color", entity_key="L3558.132",
            attribute_id=seeded["name"], market="PL", state="live",
            state_lane="current", entry_grain="model_color",
            source_kind="omnitron_api", value="An English name nobody translated",
        ))
    a_proposal(seeded["name"], "Koszulka polo")

    body = client.get("/api/imports/proposals").json()

    assert body["total"] == 1
    (row,) = body["rows"]
    assert row["current"] == "An English name nobody translated"
    assert row["proposed"] == "Koszulka polo"
    assert row["market"] == "PL"
    assert row["label"] == "Product name"
    assert row["flag"] == "EN?"
    assert row["source"] == "sibling"


def test_a_proposal_for_a_cell_with_no_live_value_still_lists(client, seeded) -> None:
    """A field that was empty rather than wrong is a normal case; the row
    reports no current text instead of being dropped."""
    a_proposal(seeded["name"], "Koszulka polo")
    (row,) = client.get("/api/imports/proposals").json()["rows"]
    assert row["current"] is None
    assert row["proposed"] == "Koszulka polo"


def test_the_counts_describe_everything_waiting_not_the_page(client, seeded) -> None:
    """The totals must not move when a filter or a limit does, or they cannot
    be used to size the backlog."""
    a_proposal(seeded["name"], "Koszulka polo", market="PL")
    a_proposal(seeded["name"], "Polokosile", market="CZ")

    body = client.get("/api/imports/proposals?limit=1").json()

    assert body["shown"] == 1
    assert body["total"] == 2
    assert body["byMarket"] == {"PL": 1, "CZ": 1}
    assert body["byAttribute"] == {"product_name": 2}
    assert body["byFlag"] == {"EN?": 2}


def test_the_list_can_be_narrowed_to_one_market(client, seeded) -> None:
    a_proposal(seeded["name"], "Koszulka polo", market="PL")
    a_proposal(seeded["name"], "Polokosile", market="CZ")

    body = client.get("/api/imports/proposals?market=CZ").json()

    assert [r["market"] for r in body["rows"]] == ["CZ"]
    assert body["total"] == 1                      # what you are looking at
    # …while the breakdown still describes everything waiting, so picking a
    # market does not collapse the counts you would use to pick the next one.
    assert body["byMarket"] == {"PL": 1, "CZ": 1}


def test_an_unknown_attribute_filter_matches_nothing_rather_than_everything(client, seeded) -> None:
    a_proposal(seeded["name"], "Koszulka polo")
    body = client.get("/api/imports/proposals?attribute=not_a_real_attribute").json()
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["byAttribute"] == {"product_name": 1}


def test_accepting_from_the_list_takes_it_out_of_the_list(client, seeded) -> None:
    a_proposal(seeded["name"], "Koszulka polo")
    assert client.get("/api/imports/proposals").json()["total"] == 1

    client.post(
        "/api/imports/proposals/accept",
        json={"modelColor": "L3558.132", "attribute": "product_name", "market": "PL"},
    )

    assert client.get("/api/imports/proposals").json()["total"] == 0
    assert client.get("/api/imports/pending").json()["approvedCount"] == 1


# ── what the shop check can reach, and what it never will ──────────────────
#
# The publication stage confirms by crawling each market's listing feed, and
# only ever sets a stamp on a positive match. So an option that has left that
# feed produces the same cell as one written five minutes ago, for ever. On
# 2026-09-23 a batch of 22 read `0 / 22` with a tooltip promising it would be
# asked again every sync, while sixteen of those values were on an option the
# HU listing had dropped thirteen days earlier and six were simply younger
# than the once-a-day check. The endpoint separates the two so the panel can.


def _batch_with(rows: list[tuple[str, str, bool]]) -> int:
    """rows: (model_color, market, published). All written and confirmed."""
    from pim_studio.models import PushBatch, PushItem, utcnow

    attr = attribute("product_name")
    with session_scope() as db:
        batch = PushBatch(
            source="ui", status="done", created_by="p@example.com",
            item_count=len(rows), written_count=len(rows), confirmed_count=len(rows),
        )
        db.add(batch)
        db.flush()
        for model_color, market, published in rows:
            db.add(PushItem(
                batch_id=batch.id, model_color=model_color, sku=f"{model_color}.T2",
                attribute_id=attr, market=market, key="urun_adi_pl",
                new_value="Koszulka", status="confirmed",
                published_at=utcnow() if published else None,
            ))
        return batch.id


def _listed(model_color: str, market: str, *, gone: bool = False) -> None:
    from pim_studio.models import StorefrontListing, utcnow

    with session_scope() as db:
        db.add(StorefrontListing(
            model_color=model_color, market=market,
            left_at=utcnow() if gone else None,
        ))


def test_a_value_waiting_on_a_listed_option_is_not_called_unlisted(client) -> None:
    batch = _batch_with([("L3558.132", "PL", False)])
    _listed("L3558.132", "PL")

    row = client.get("/api/imports/batches").json()["rows"][0]
    assert (row["publishedCount"], row["unlistedCount"]) == (0, 0)
    detail = client.get(f"/api/imports/batches/{batch}").json()
    assert detail["items"][0]["unlisted"] is False


def test_an_option_that_left_the_listing_is_counted_apart_from_the_wait(client) -> None:
    """The batch that started this: some live, the rest unreachable."""
    batch = _batch_with(
        [("NF3982HX.T98", "PL", True)] * 2 + [("5H1299.64I", "HU", False)] * 3
    )
    _listed("NF3982HX.T98", "PL")
    _listed("5H1299.64I", "HU", gone=True)

    row = client.get("/api/imports/batches").json()["rows"][0]
    assert (row["confirmedCount"], row["publishedCount"], row["unlistedCount"]) == (5, 2, 3)
    detail = client.get(f"/api/imports/batches/{batch}").json()
    by_mc = {(i["modelColor"], i["unlisted"]) for i in detail["items"]}
    assert by_mc == {("NF3982HX.T98", False), ("5H1299.64I", True)}


def test_an_option_no_sweep_has_ever_seen_is_unlisted_too(client) -> None:
    """`2001343.-` has never been in any market's feed. Silence from the
    sweep is not "not yet" — there is no row to wait for."""
    _batch_with([("2001343.-", "PL", False)])

    assert client.get("/api/imports/batches").json()["rows"][0]["unlistedCount"] == 1


def test_a_global_value_is_reachable_while_any_market_still_lists_it(client) -> None:
    """A `*` item is one copy every shop serves, and `confirm_published` asks
    each of them, so one market still listing the option is enough."""
    _batch_with([("L3558.132", "*", False)])
    _listed("L3558.132", "PL", gone=True)
    _listed("L3558.132", "SK")

    assert client.get("/api/imports/batches").json()["rows"][0]["unlistedCount"] == 0


def test_a_failed_item_is_not_owed_a_publication(client) -> None:
    """It was never written, so it counts against neither number."""
    from pim_studio.models import PushItem

    batch = _batch_with([("2001343.-", "PL", False)])
    with session_scope() as db:
        db.scalars(select(PushItem).where(PushItem.batch_id == batch)).one().status = "failed"

    row = client.get("/api/imports/batches").json()["rows"][0]
    assert (row["publishedCount"], row["unlistedCount"]) == (0, 0)
    detail = client.get(f"/api/imports/batches/{batch}").json()
    assert detail["items"][0]["unlisted"] is False
