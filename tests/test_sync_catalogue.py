"""Discovering the product set from catalogue membership.

The behaviours worth pinning are the ones that make this replace a frozen
export safely: membership resolves per SKU, leaving a catalogue is not
deletion, and a product with no usable identifier is dropped rather than
guessed at.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import CatalogueMembership, Product
from pim_studio.omnitron import OmnitronClient
from pim_studio.sync import catalogue
from pim_studio.sync.runs import SyncRun

pytestmark = pytest.mark.usefixtures("temp_db")

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}

# catalog pk -> market, mirroring scope.MARKETS
PL, CZ, HU, SK = 4, 40, 41, 172


def upstream(pk: int, sku: str, mcs: str = "L3539.132.T2", is_active: bool = True) -> dict:
    return {
        "pk": pk,
        "sku": sku,
        "is_active": is_active,
        "modified_date": "2026-08-10T11:14:09.701924Z",
        "attributes": {"integration_base_code_format": mcs},
    }


def api_with(memberships: dict[int, list[int]], catalogue_products: list[dict]):
    """memberships maps catalog pk -> product pks in it."""

    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if "catalog_items" in path:
            cat = int(params.get("catalog"))
            rows = [{"pk": i, "product": p, "catalog": cat}
                    for i, p in enumerate(memberships.get(cat, []), start=1)]
        else:
            asked = {int(x) for x in (params.get("pk__in") or "").split(",") if x}
            rows = [p for p in catalogue_products if p["pk"] in asked]
        return httpx.Response(200, json={"count": len(rows), "next": None, "results": rows})

    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    return c


def a_run() -> SyncRun:
    return SyncRun(row_id=1, kind="omnitron_catalogue")


def products() -> list[Product]:
    with session_scope() as db:
        return list(db.scalars(select(Product).order_by(Product.sku)))


def memberships() -> list[CatalogueMembership]:
    with session_scope() as db:
        return list(
            db.scalars(select(CatalogueMembership).order_by(CatalogueMembership.market))
        )


# ── discovery ──────────────────────────────────────────────────────────────


def test_a_product_only_in_the_catalogue_is_created() -> None:
    """The whole point: products absent from the old export but present in a
    catalogue were previously invisible and unfindable by any re-sync."""
    stats = catalogue.discover_products(
        api_with({PL: [190541]}, [upstream(190541, "2011169", "2011169.-.-")]), a_run()
    )
    assert stats["products_created"] == 1
    row = products()[0]
    assert row.sku == "2011169"
    assert row.omnitron_pk == 190541
    assert row.model_color == "2011169.-"


def test_identifiers_derive_from_the_mcs_attribute() -> None:
    catalogue.discover_products(
        api_with({PL: [1]}, [upstream(1, "36889", "L3539.132.T2")]), a_run()
    )
    row = products()[0]
    assert (row.base_code, row.model_color, row.size) == ("L3539", "L3539.132", "T2")


def test_a_product_with_no_mcs_is_dropped_not_guessed() -> None:
    record = upstream(1, "36889")
    record["attributes"] = {}
    run = a_run()
    stats = catalogue.discover_products(api_with({PL: [1]}, [record]), run)

    assert stats["products_created"] == 0
    assert stats["unresolvable"] == 1
    assert run.rows_dropped == 1
    assert products() == []


def test_a_product_we_already_hold_is_not_duplicated() -> None:
    with session_scope() as db:
        db.add(
            Product(
                sku="36889", base_code_format="L3539.132.T2", model_color="L3539.132",
                base_code="L3539", size="T2", omnitron_pk=1,
            )
        )
    stats = catalogue.discover_products(
        api_with({PL: [1]}, [upstream(1, "36889")]), a_run()
    )
    assert stats["already_known"] == 1
    assert stats["products_created"] == 0
    assert len(products()) == 1


# ── membership is per SKU and per market ───────────────────────────────────


def test_membership_is_recorded_per_market() -> None:
    catalogue.discover_products(
        api_with(
            {PL: [1], CZ: [1], HU: [], SK: []},
            [upstream(1, "36889")],
        ),
        a_run(),
    )
    assert sorted(m.market for m in memberships()) == ["CZ", "PL"]


def test_siblings_can_differ_in_membership() -> None:
    """`sku=4677500` is absent from PL and CZ while `sku=2001212`, same base
    code, is in both. Resolving at style level would include products no
    market sells."""
    catalogue.discover_products(
        api_with(
            {PL: [190537], CZ: [190537]},
            [
                upstream(190537, "2001212", "2001212.-.-"),
                upstream(190528, "4677500", "2001212.-.-"),
            ],
        ),
        a_run(),
    )
    skus = [p.sku for p in products()]
    assert skus == ["2001212"], "only the catalogue member should be created"


def test_catalogues_are_read_one_at_a_time() -> None:
    """`catalog__in` is silently ignored upstream and returns everything, so
    each catalogue must be its own request."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "catalog_items" in request.url.path:
            seen.append(request.url.params.get("catalog"))
            assert request.url.params.get("catalog__in") is None
        return httpx.Response(200, json={"count": 0, "next": None, "results": []})

    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    catalogue.discover_products(c, a_run())
    read = [int(x) for x in seen]
    assert read == [4, 40, 41, 172, 601], (
        "every catalogue any market reads, each once, every own one first"
    )


def test_a_market_reads_its_shared_catalogue_too() -> None:
    """The bug this fixes: 283 options the PL storefront sells were
    unaccounted for, 57 of them not held at all, because this stage only ever
    read `catalog_pk`. 44 of 45 sampled were in 601."""
    only_in_601 = upstream(9001, "5000001", "NEW1.001.T2")
    stats = catalogue.discover_products(
        api_with({601: [9001]}, [only_in_601]), a_run()
    )

    assert stats["products_created"] == 1
    assert [(m.market, m.catalog_pk) for m in memberships()] == [
        ("CZ", 601), ("HU", 601), ("PL", 601), ("SK", 601)
    ], "a shared catalogue grants membership to every market that reads it"


def test_a_membership_records_which_catalogue_it_came_from() -> None:
    """Own catalogue wins over shared, because it is the more specific
    provenance and it is read first. Which catalogue is holding a product up
    stops being derivable from the market once a market reads two."""
    both = upstream(9002, "5000002", "NEW2.001.T2")
    catalogue.discover_products(api_with({PL: [9002], 601: [9002]}, [both]), a_run())

    by_market = {m.market: m.catalog_pk for m in memberships()}
    assert by_market["PL"] == 4, "PL's own catalogue is the provenance"
    assert by_market["CZ"] == 601, "CZ only sees it through the shared one"


# ── leaving is not deletion ────────────────────────────────────────────────


def test_leaving_a_catalogue_sets_left_at_and_not_delisted_at() -> None:
    """A product dropping out of PL is a commercial event. `delisted_at` means
    Omnitron deleted the record — a different fact, with a different source."""
    api = api_with({PL: [1], CZ: [1]}, [upstream(1, "36889")])
    catalogue.discover_products(api, a_run())
    assert len(memberships()) == 2

    gone_from_pl = api_with({PL: [], CZ: [1]}, [upstream(1, "36889")])
    stats = catalogue.discover_products(gone_from_pl, a_run())

    assert stats["departed"] == 1
    by_market = {m.market: m for m in memberships()}
    assert by_market["PL"].left_at is not None
    assert by_market["CZ"].left_at is None
    assert products()[0].delisted_at is None, "leaving a catalogue is not deletion"


def test_rejoining_clears_left_at() -> None:
    api_gone = api_with({PL: []}, [upstream(1, "36889")])
    with session_scope() as db:
        db.add(
            Product(
                sku="36889", base_code_format="L3539.132.T2", model_color="L3539.132",
                base_code="L3539", size="T2", omnitron_pk=1,
            )
        )
    catalogue.discover_products(api_gone, a_run())

    back = api_with({PL: [1]}, [upstream(1, "36889")])
    catalogue.discover_products(back, a_run())
    assert memberships()[0].left_at is None


def test_a_second_identical_run_adds_nothing() -> None:
    api = api_with({PL: [1], CZ: [1]}, [upstream(1, "36889")])
    catalogue.discover_products(api, a_run())
    stats = catalogue.discover_products(api, a_run())

    assert stats["products_created"] == 0
    assert stats["added"] == 0
    assert stats["departed"] == 0
    assert stats["refreshed"] == 2


# ── product_meta ───────────────────────────────────────────────────────────
#
# Omnitron puts its grouping rows in catalogues alongside real products, and
# they look enough like one to have been minted as `Product` rows for as long
# as this stage existed. The cost is in `models.PRODUCT_META`; these pin the
# behaviour that stops it.
#
# **Every one of them runs against both spellings of `product_type`, and that
# is not defensiveness.** `/api/v1/products/` — which is what `_resolve` reads —
# sends the string `"1"`. These tests only ever passed `1`, the integer, so
# they exercised the one value the old `== PRODUCT_META` comparison handled
# while the guard was inert against every real record: across every recorded
# `omnitron_catalogue` run the product_meta drop count is zero, in a tenant
# holding ~18,431 of them. The suite was green throughout.
#
# So the parameter is the regression test. A guard whose fixture disagrees with
# the wire is not pinned by the fixture — it is hidden by it.

#: What `product_type` looks like coming off the wire, and what it looks like
#: in every fixture written before 2026-09-09.
META_SPELLINGS = pytest.mark.parametrize(
    "product_type",
    ["1", 1],
    ids=["string-as-the-api-sends-it", "int-as-the-old-fixtures-had-it"],
)


@META_SPELLINGS
def test_a_product_meta_row_never_becomes_a_product(product_type: object) -> None:
    record = upstream(267024, "TH2312", "TH2312.166.T5")
    record["product_type"] = product_type
    stats = catalogue.discover_products(api_with({PL: [267024]}, [record]), a_run())

    assert stats["products_created"] == 0
    assert stats["product_metas_skipped"] == 1
    assert products() == []


@META_SPELLINGS
def test_a_product_meta_brings_no_catalogue_membership_either(product_type: object) -> None:
    """It is a catalogue item, so the membership pass sees it. Nothing may
    hang off a product row that was deliberately not created."""
    meta = upstream(267024, "TH2312", "TH2312.166.T5")
    meta["product_type"] = product_type
    real = upstream(265001, "5259109", "TH2312.001.T2")

    catalogue.discover_products(api_with({PL: [267024, 265001]}, [meta, real]), a_run())

    assert [p.sku for p in products()] == ["5259109"]
    assert [(m.market, m.product_id) for m in memberships()] == [("PL", products()[0].id)]


@META_SPELLINGS
def test_the_meta_would_otherwise_land_inside_a_real_model_color(product_type: object) -> None:
    """Why the skip is at the top of the loop rather than a later filter.

    The meta's `integration_base_code_format` names one arbitrary colour and
    size, so every identifier derived from it is indistinguishable from a real
    size of a real option — here a second `TH2312.166.T5` sitting beside the
    genuine one. Anything only the meta carried then becomes that option's.
    """
    meta = upstream(267024, "TH2312", "TH2312.166.T5")
    meta["product_type"] = product_type
    twin = upstream(265002, "5259200", "TH2312.166.T5")

    catalogue.discover_products(api_with({PL: [267024, 265002]}, [meta, twin]), a_run())

    rows = products()
    assert len(rows) == 1
    assert (rows[0].sku, rows[0].model_color, rows[0].size) == (
        "5259200", "TH2312.166", "T5",
    )


def test_a_string_zero_is_a_real_product_not_a_grouping_row() -> None:
    """The other half of reading the field as text.

    Widening the comparison to strings must not widen what counts as a meta.
    `"0"` is a size, and a guard that answered "grouping row" to anything
    string-shaped would empty the product table rather than merely fail to
    protect it — the louder failure, but not one to find in production.
    """
    record = upstream(1, "36889", "L3539.132.T2")
    record["product_type"] = "0"
    stats = catalogue.discover_products(api_with({PL: [1]}, [record]), a_run())

    assert stats["products_created"] == 1
    assert stats["product_metas_skipped"] == 0
    assert products()[0].sku == "36889"


def test_a_record_with_no_product_type_is_kept() -> None:
    """Absent is not meta. The field is missing from some list responses, and
    treating that as a grouping row would drop real products on an endpoint
    that simply declines to say."""
    record = upstream(1, "36889", "L3539.132.T2")
    record.pop("product_type", None)
    stats = catalogue.discover_products(api_with({PL: [1]}, [record]), a_run())

    assert stats["products_created"] == 1
    assert stats["product_metas_skipped"] == 0


def test_a_real_product_records_its_type() -> None:
    record = upstream(1, "36889", "L3539.132.T2")
    record["product_type"] = 0
    catalogue.discover_products(api_with({PL: [1]}, [record]), a_run())
    assert products()[0].omnitron_product_type == 0
