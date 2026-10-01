"""The product's own change timeline.

What is worth pinning here is the interleaving. Values live at two grains — a
name on the option, a price on each size — and a person looking at a product
wants one list, not two. So the endpoint has to gather both and keep them in
time order, and it has to say which grain each row came from, because "the
price changed" and "the price changed on one size" are different events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import app as app_module
from pim_studio.admin.auth import current_user
from pim_studio.db import session_scope
from pim_studio.models import Attribute, Product, User, ValueChange

pytestmark = pytest.mark.usefixtures("temp_db")

WHEN = datetime(2026, 9, 5, 2, 14, tzinfo=UTC)


@pytest.fixture
def client() -> TestClient:
    app_module.app.dependency_overrides[current_user] = lambda: User(
        id=1, email="dev@example.com", role="developer", is_active=True
    )
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def attribute(key: str, label: str) -> int:
    with session_scope() as db:
        row = Attribute(
            key=key, label=label, write_channel="omnitron_import", value_grain="sku"
        )
        db.add(row)
        db.flush()
        return row.id


def product(sku: str, model_color: str) -> None:
    with session_scope() as db:
        db.add(
            Product(
                sku=sku,
                base_code_format=sku,
                model_color=model_color,
                base_code=model_color.split(".")[0],
            )
        )


def change(attr_id: int, entity_kind: str, entity_key: str, **over) -> None:
    fields = {
        "entity_kind": entity_kind,
        "entity_key": entity_key,
        "attribute_id": attr_id,
        "market": "PL",
        "old_value": "OUTLET",
        "new_value": "26AW",
        "changed_at": WHEN,
    }
    fields.update(over)
    with session_scope() as db:
        db.add(ValueChange(**fields))


@pytest.fixture
def seeded() -> dict[str, int]:
    season = attribute("pl_sezon", "Season (PL team)")
    price = attribute("price", "Price")
    product("AH4463.132.T03", "AH4463.132")
    product("AH4463.132.T04", "AH4463.132")
    product("ZZ0960.000.T01", "ZZ0960.000")
    return {"season": season, "price": price}


def history(client: TestClient, mc: str = "AH4463.132") -> dict:
    response = client.get(f"/api/products/{mc}/history")
    assert response.status_code == 200
    return response.json()


def test_an_option_grain_change_appears(client, seeded):
    change(seeded["season"], "model_color", "AH4463.132")
    rows = history(client)["changes"]
    assert len(rows) == 1
    assert rows[0]["label"] == "Season (PL team)"
    assert (rows[0]["oldValue"], rows[0]["newValue"]) == ("OUTLET", "26AW")
    assert rows[0]["sku"] is None


def test_a_size_grain_change_appears_and_names_its_size(client, seeded):
    """The reason `sku` is on the row. A price that moved on one size is not
    the same event as one that moved on the option."""
    change(seeded["price"], "sku", "AH4463.132.T03", old_value="349", new_value="299")
    rows = history(client)["changes"]
    assert len(rows) == 1
    assert rows[0]["sku"] == "AH4463.132.T03"
    assert rows[0]["entityKind"] == "sku"


def test_both_grains_land_in_one_timeline_in_time_order(client, seeded):
    change(seeded["season"], "model_color", "AH4463.132")
    change(
        seeded["price"],
        "sku",
        "AH4463.132.T03",
        changed_at=WHEN + timedelta(days=1),
        old_value="349",
        new_value="299",
    )
    rows = history(client)["changes"]
    assert [r["attribute"] for r in rows] == ["price", "pl_sezon"]


def test_another_products_changes_do_not_leak_in(client, seeded):
    change(seeded["season"], "model_color", "ZZ0960.000")
    change(seeded["price"], "sku", "ZZ0960.000.T01")
    assert history(client)["changes"] == []


def test_no_history_is_not_the_same_as_never_changed(client, seeded):
    """The table starts the day it was added. A product can be years old and
    have nothing here, and the page must not present that as 'nothing ever
    changed'."""
    body = history(client)
    assert body["changes"] == []
    assert body["recordingSince"] is None

    change(seeded["season"], "model_color", "AH4463.132")
    assert history(client)["recordingSince"] is not None


def test_a_long_value_is_cut_and_says_so(client, seeded):
    """Descriptions run to 1,716 characters. A timeline is a list of what
    happened, not a place to read one."""
    change(seeded["season"], "model_color", "AH4463.132", new_value="x" * 5000)
    row = history(client)["changes"][0]
    assert row["truncated"] is True
    assert len(row["newValue"]) == 300


def test_the_page_is_bounded_and_says_there_is_more(client, seeded):
    for n in range(5):
        change(
            seeded["season"],
            "model_color",
            "AH4463.132",
            changed_at=WHEN + timedelta(days=n),
        )
    response = client.get("/api/products/AH4463.132/history?limit=2")
    body = response.json()
    assert len(body["changes"]) == 2
    assert body["more"] is True


def test_sizes_covered_is_reported(client, seeded):
    """So the page can say the timeline spans two sizes rather than leaving a
    reader to wonder whether a size was missed."""
    assert history(client)["skusCovered"] == 2


def test_history_requires_a_session():
    app_module.app.dependency_overrides.clear()
    with TestClient(app_module.app) as anon:
        assert anon.get("/api/products/AH4463.132/history").status_code == 401
