"""A conflict is a comparison across ONE option's sizes, or it is nothing.

`model_color` is parsed from Omnitron's `integration_base_code_format`. For 87
styles (1,445 SKUs, 1.5% of the catalogue, measured 2026-09-23) that field
arrives as the bare base code — no colour, no size — so every SKU of every
colour parses to the same `model_color`.

A conflict detector reading that key does exactly what it is told and compares
across colours. On `TH5038` in production that is 192 SKUs spanning ten
colours, and `color_code` alone offers 28 candidates. Ten products are being
asked to agree, and the "winner" of such a vote is one colour's value written
over the other nine.

So the endpoint excludes them from the queue and counts them instead, and
`decide` refuses them outright. The refusal is the half that matters: the
listing is a convenience, the write is a door.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import accounts
from pim_studio.admin import app as app_module
from pim_studio.db import session_scope
from pim_studio.models import Attribute, Product, ValueConflict, utcnow

pytestmark = pytest.mark.usefixtures("temp_db")

PASSWORD = "a-long-enough-one"

#: A real option: one colour, several sizes.
OPTION = "AB0933.001"
#: A style: Omnitron gave the bare base code, so every colour landed here.
STYLE = "TH5038"


def _product(sku: str, model_color: str, base_code: str, size: str | None) -> Product:
    return Product(
        sku=sku,
        base_code_format=model_color if size is None else f"{model_color}.{size}",
        model_color=model_color,
        base_code=base_code,
        size=size,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )


@pytest.fixture
def seeded() -> None:
    with session_scope() as db:
        attribute = Attribute(
            key="product_name",
            label="Product name",
            write_channel="product_card",
            value_grain="model_color",
            per_market=True,
        )
        db.add(attribute)
        db.flush()

        # The option — model_color differs from base_code and its rows carry
        # sizes, which is what a colour of a style looks like.
        for i, size in enumerate(("T1", "T2", "T3")):
            db.add(_product(f"opt{i}", OPTION, "AB0933", size))
        # The style — model_color IS the base code and no row carries a size.
        # Two different colours, which is the whole problem.
        for i in range(4):
            db.add(_product(f"sty{i}", STYLE, STYLE, None))

        for key, skus in ((OPTION, 3), (STYLE, 4)):
            db.add(
                ValueConflict(
                    entity_kind="model_color",
                    entity_key=key,
                    attribute_id=attribute.id,
                    market="PL",
                    candidates=json.dumps({"Koszulka": 2, "Polo": skus - 2}),
                    sku_count=skus,
                    sources="omnitron_api",
                    detected_at=utcnow(),
                )
            )


@pytest.fixture
def client(seeded: None) -> Iterator[TestClient]:
    del seeded
    with session_scope() as db:
        accounts.create_user(
            db, email="dev@example.com", password=PASSWORD, role="developer"
        )
    c = TestClient(app_module.app, raise_server_exceptions=False)
    assert (
        c.post(
            "/api/auth/login", json={"email": "dev@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )
    yield c


def test_the_option_is_listed_and_the_style_is_not(client: TestClient) -> None:
    body = client.get("/api/conflicts").json()
    keys = [r["modelColor"] for r in body["rows"]]
    assert keys == [OPTION], keys


def test_the_style_is_counted_rather_than_dropped(client: TestClient) -> None:
    """Excluded and silent would read exactly like never detected."""
    body = client.get("/api/conflicts").json()
    assert body["notAnOption"] == {"rows": 1, "styles": 1}


def test_total_and_by_attribute_count_only_real_options(client: TestClient) -> None:
    """The headline number and the queue have to describe the same set.

    Counting the style in `total` while refusing to list it gives a page that
    says 2 and shows 1, which reads as pagination rather than as exclusion.
    """
    body = client.get("/api/conflicts").json()
    assert body["total"] == 1
    assert [a["count"] for a in body["byAttribute"]] == [1]


def test_deciding_a_style_is_refused(client: TestClient) -> None:
    """The important half: this write would stamp one colour on all of them."""
    res = client.post(
        "/api/conflicts/decide",
        json={
            "modelColor": STYLE,
            "attribute": "product_name",
            "market": "PL",
            "value": "Polo",
        },
    )
    assert res.status_code == 422
    assert "style" in res.json()["detail"].lower()


def test_deciding_a_real_option_still_works(client: TestClient) -> None:
    """The guard must not have closed the door it was narrowing."""
    res = client.post(
        "/api/conflicts/decide",
        json={
            "modelColor": OPTION,
            "attribute": "product_name",
            "market": "PL",
            "value": "Polo",
        },
    )
    assert res.status_code == 200, res.text
