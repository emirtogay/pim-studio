"""Refreshing products and applying deletions.

The two behaviours that must hold: absence never delists, and `deleted_items`
always does — matched on Omnitron's pk, which is what the refresh stores.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import Product
from pim_studio.omnitron import OmnitronClient
from pim_studio.sync import products, sync_run
from pim_studio.sync.runs import SyncRun

pytestmark = pytest.mark.usefixtures("temp_db")

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}


def our_product(sku: str, base_code: str = "L3539", **over) -> None:
    fields = {
        "sku": sku,
        "base_code_format": f"{base_code}.132.T5",
        "model_color": f"{base_code}.132",
        "base_code": base_code,
        "size": "T5",
    }
    fields.update(over)
    with session_scope() as db:
        db.add(Product(**fields))


def upstream(sku: str, pk: int, base_code: str = "L3539", is_active: bool = True) -> dict:
    return {
        "sku": sku,
        "pk": pk,
        "base_code": base_code,
        "is_active": is_active,
        "modified_date": "2026-08-10T11:14:09.701924Z",
    }


def api_with(product_records: list[dict], deleted: list[dict] | None = None) -> OmnitronClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "deleted_items" in request.url.path:
            rows = deleted or []
        else:
            asked = (request.url.params.get("sku__in") or "").split(",")
            rows = [r for r in product_records if r["sku"] in asked]
        return httpx.Response(200, json={"count": len(rows), "next": None, "results": rows})

    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    return c


def a_run(kind: str = "omnitron_products") -> SyncRun:
    return SyncRun(row_id=1, kind=kind)


def row(sku: str) -> Product:
    with session_scope() as db:
        return db.scalars(select(Product).where(Product.sku == sku)).one()


# ── refresh ────────────────────────────────────────────────────────────────


def test_identity_fields_land_on_our_rows() -> None:
    our_product("1057561")
    stats = products.refresh_products(api_with([upstream("1057561", 5674)]), a_run())

    assert (stats["found"], stats["missing"]) == (1, 0)
    refreshed = row("1057561")
    assert refreshed.omnitron_pk == 5674
    assert refreshed.omnitron_is_active is True
    assert refreshed.omnitron_modified_date is not None


def test_a_missing_sku_is_counted_and_never_delisted() -> None:
    """Absence from a batched read can be a partial failure. Only the deletion
    feed retires a product."""
    our_product("999999")
    run = a_run()
    stats = products.refresh_products(api_with([]), run)

    assert stats["missing"] == 1
    assert run.rows_dropped == 1
    assert row("999999").delisted_at is None, "absence must not delist"


def test_a_base_code_disagreement_is_reported_not_overwritten() -> None:
    """If their derivation and ours disagree, one is wrong — an UPDATE would
    hide the only evidence."""
    our_product("1057561", base_code="L3539")
    stats = products.refresh_products(
        api_with([upstream("1057561", 5674, base_code="PH9619")]), a_run()
    )

    assert stats["base_code_disagreements"] == 1
    assert row("1057561").base_code == "L3539", "ours must survive"


def test_delisted_rows_are_left_alone() -> None:
    """A retired product stays retired even if its SKU answers again —
    resurrection is a human decision, not a nightly side effect."""
    from pim_studio.models import utcnow

    our_product("1057561", delisted_at=utcnow())
    stats = products.refresh_products(api_with([upstream("1057561", 5674)]), a_run())

    assert stats["skus"] == 0
    assert row("1057561").omnitron_pk is None


def test_second_pass_with_no_upstream_change_updates_nothing() -> None:
    our_product("1057561")
    api = api_with([upstream("1057561", 5674)])
    products.refresh_products(api, a_run())

    stats = products.refresh_products(api, a_run())
    assert stats["updated"] == 0, "same upstream state must not count as a change"


# ── deletions ──────────────────────────────────────────────────────────────


def test_a_deletion_in_the_feed_delists_our_row_by_pk() -> None:
    our_product("1057561")
    api = api_with(
        [upstream("1057561", 5674)],
        deleted=[{"object_id": 5674, "deleted_date": "2026-08-13T10:00:00Z"}],
    )
    products.refresh_products(api, a_run())
    stats = products.delist_deleted(api, a_run())

    assert stats["ours_delisted"] == 1
    assert row("1057561").delisted_at is not None


def test_a_deletion_for_a_product_we_never_held_is_ignored() -> None:
    our_product("1057561")
    api = api_with(
        [upstream("1057561", 5674)],
        deleted=[{"object_id": 999999, "deleted_date": "2026-08-13T10:00:00Z"}],
    )
    products.refresh_products(api, a_run())
    stats = products.delist_deleted(api, a_run())

    assert stats["deletions_in_feed"] == 1
    assert stats["ours_delisted"] == 0
    assert row("1057561").delisted_at is None


def test_an_already_delisted_row_is_not_delisted_twice() -> None:
    our_product("1057561")
    api = api_with(
        [upstream("1057561", 5674)],
        deleted=[{"object_id": 5674, "deleted_date": "2026-08-13T10:00:00Z"}],
    )
    products.refresh_products(api, a_run())
    products.delist_deleted(api, a_run())
    first = row("1057561").delisted_at

    stats = products.delist_deleted(api, a_run())
    assert stats["ours_already_delisted"] == 1
    assert row("1057561").delisted_at == first, "the original date must survive"


def test_the_deletion_window_narrows_after_a_successful_run() -> None:
    """First run reads the whole feed; later runs window on the previous
    successful run of the same kind, formatted as explicit UTC."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "deleted_items" in request.url.path:
            seen.append(request.url)
        return httpx.Response(200, json={"count": 0, "next": None, "results": []})

    api = OmnitronClient(**CREDS)
    api._http = httpx.Client(transport=httpx.MockTransport(handler))
    api._token = "test-token"

    with sync_run("omnitron_products") as run:
        products.delist_deleted(api, run)
    assert seen[0].params.get("deleted_date__gte") is None, "first run must read all history"

    with sync_run("omnitron_products") as run:
        products.delist_deleted(api, run)
    window = seen[1].params.get("deleted_date__gte")
    assert window is not None, "second run must window on the previous run"
    assert window.endswith("Z"), "the window must be explicit UTC"


# ── product_type ───────────────────────────────────────────────────────────


def test_the_refresh_stamps_product_type_on_rows_minted_before_the_skip() -> None:
    """The backfill path. `sync.catalogue` skips product_meta rows now, but the
    ones it already created are in the table and only this stage reads them —
    it is what tells a grouping row apart from a size after the fact."""
    our_product("TH2312", base_code="TH2312")
    record = upstream("TH2312", 267024, base_code="TH2312")
    record["product_type"] = 1

    products.refresh_products(api_with([record]), a_run())

    with session_scope() as db:
        row = db.scalars(select(Product).where(Product.sku == "TH2312")).one()
    assert row.omnitron_product_type == 1
    assert row.delisted_at is None, "a meta is not deleted, it is not a size"


def test_a_meta_row_is_read_by_the_refresh_and_hidden_from_everything_else() -> None:
    our_product("TH2312", base_code="TH2312")
    our_product("5259109", base_code="TH2312")
    meta = upstream("TH2312", 267024, base_code="TH2312")
    meta["product_type"] = 1
    real = upstream("5259109", 265001, base_code="TH2312")
    real["product_type"] = 0

    stats = products.refresh_products(api_with([meta, real]), a_run())
    assert stats["found"] == 2, "the refresh must see both, or it cannot stamp one"

    with session_scope() as db:
        visible = list(db.scalars(select(Product.sku).where(Product.is_size())))
    assert visible == ["5259109"]


def test_an_unrefreshed_row_still_counts_as_a_size() -> None:
    """NULL means not yet read. A table that has never been refreshed must
    render rather than empty."""
    our_product("5259109")
    with session_scope() as db:
        assert list(db.scalars(select(Product.sku).where(Product.is_size()))) == [
            "5259109"
        ]
