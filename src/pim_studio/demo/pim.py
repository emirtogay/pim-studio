"""The PIM API, served from the demo world.

Promoted from the test fakes rather than invented: `FakeOmnitron` in
`tests/test_sync_push.py` already answered the product read and the PATCH with
per-locale merge semantics, and `FakeApi` stood in for the client at the method
level for the scope checks. This answers at the HTTP level, so `MockPim` is the
real `OmnitronClient` — its pagination, its write doors, its scope and locale
guards all run unchanged — with only the transport swapped. A demo that
bypassed those would be demonstrating a different program.

Endpoints served, with the filters the sync relies on honoured and every other
parameter ignored, exactly as the real API ignores what it does not know:

    POST   /api/i1/auth/login/
    GET    /api/v1/attributes/          key
    GET    /api/v1/attribute_value/     attribute; label follows Accept-Language
    GET    /api/v1/attribute_set/
    GET    /api/v1/attribute_config/
    GET    /api/v1/channels/
    GET    /api/v1/catalogs/{pk}/
    GET    /api/v1/catalog_items/       catalog, product, modified_date__gte
    GET    /api/v1/catalog_items/{pk}/
    DELETE /api/v1/catalog_items/{pk}/
    POST   /api/v1/catalog_items/
    GET    /api/v1/products/            sku__in, pk__in; attributes follow Accept-Language
    PATCH  /api/v1/products/{pk}/
    GET    /api/v1/product_price/       price_list, price__gt, modified_date__gte
    GET    /api/v1/product_stock/       stock_list, stock__gt, modified_date__gte
    GET    /api/v1/deleted_items/       content_type_model, deleted_date__gte
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx

from ..omnitron.client import OmnitronClient
from . import catalog as cat
from .world import World, current, registry, vocabulary_records

#: Where the mock PIM answers. The client is handed this explicitly, so the
#: demo never depends on what the environment says the PIM's address is.
BASE_URL = "https://pim.example.test"

_ITEM = re.compile(r"^/api/v1/catalog_items/(\d+)/?$")
_PRODUCT = re.compile(r"^/api/v1/products/(\d+)/?$")
_CATALOG = re.compile(r"^/api/v1/catalogs/(\d+)/?$")


def _catalog_record(pk: int) -> dict[str, Any] | None:
    """A catalogue, shaped the way `scope.verify_against_live` reads it."""
    for market, own in cat.OWN_CATALOG.items():
        if own == pk:
            return {
                "pk": pk,
                "name": f"Northwind {market} catalogue",
                "price_list": cat.PRICE_LIST[market],
                "stock_list": cat.STOCK_LIST[market],
                "category_tree": {"PL": 35, "CZ": 101, "HU": 134, "SK": 266}[market],
                "extra_price_lists": [200] if market == "SK" else [],
            }
    if pk == cat.SHARED_CATALOG:
        return {
            "pk": pk,
            "name": "Northwind shared catalogue",
            "price_list": 1,
            "stock_list": 68,
            "category_tree": 629,
            "extra_price_lists": [134, 35, 2, 200, 135, 167],
        }
    return None


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _page(request: httpx.Request, records: list[dict[str, Any]]) -> httpx.Response:
    params = request.url.params
    limit = max(1, int(params.get("limit") or 100))
    page = max(1, int(params.get("page") or 1))
    chunk = records[(page - 1) * limit : page * limit]
    more = page * limit < len(records)
    following = str(request.url.copy_set_param("page", page + 1)) if more else None
    return httpx.Response(
        200,
        json={"count": len(records), "next": following, "previous": None, "results": chunk},
    )


def _ids(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _not_found(what: str) -> httpx.Response:
    return httpx.Response(404, json={"detail": f"{what} not found"})


def handle(world: World, request: httpx.Request) -> httpx.Response:
    """Answer one request against the PIM as `world` holds it."""
    path = request.url.path
    params = request.url.params
    method = request.method
    locale = request.headers.get("accept-language") or None

    if path.rstrip("/") == "/api/i1/auth/login":
        return httpx.Response(200, json={"key": "demo-token"})

    if method == "GET" and path.rstrip("/") == "/api/v1/attributes":
        records = registry()["attributes"]
        if params.get("key"):
            records = [r for r in records if r["key"] == params["key"]]
        return _page(request, records)
    if method == "GET" and path.rstrip("/") == "/api/v1/attribute_value":
        records = vocabulary_records(world, locale)
        if params.get("attribute"):
            records = [r for r in records if str(r["attribute"]) == params["attribute"]]
        return _page(request, records)
    if method == "GET" and path.rstrip("/") == "/api/v1/attribute_set":
        return _page(request, registry()["sets"])
    if method == "GET" and path.rstrip("/") == "/api/v1/attribute_config":
        return _page(request, registry()["configs"])
    if method == "GET" and path.rstrip("/") == "/api/v1/channels":
        return _page(request, cat.channels())
    if method == "GET" and (match := _CATALOG.match(path)):
        record = _catalog_record(int(match.group(1)))
        return httpx.Response(200, json=record) if record else _not_found("catalog")

    if path.rstrip("/") == "/api/v1/catalog_items":
        if method == "POST":
            body = json.loads(request.content or b"{}")
            product, catalog = int(body["product"]), int(body["catalog"])
            if product not in world.products:
                return httpx.Response(400, json={"product": ["Invalid pk."]})
            if any(p == product and c == catalog for (p, c, _m) in world.items.values()):
                return httpx.Response(
                    400,
                    json={"non_field_errors": [
                        "The fields product, catalog must make a unique set."
                    ]},
                )
            return httpx.Response(201, json=world.create_item(product, catalog))
        since = _when(params.get("modified_date__gte"))
        records = [
            {"pk": pk, "product": product, "catalog": catalog,
             "modified_date": modified.isoformat()}
            for pk, (product, catalog, modified) in sorted(world.items.items())
            if (not params.get("catalog") or str(catalog) == params["catalog"])
            and (not params.get("product") or str(product) == params["product"])
            and (since is None or modified >= since)
        ]
        return _page(request, records)
    if match := _ITEM.match(path):
        pk = int(match.group(1))
        held = world.items.get(pk)
        if held is None:
            return _not_found("catalog item")
        if method == "DELETE":
            world.delete_item(pk)
            return httpx.Response(204)
        product, catalog, modified = held
        return httpx.Response(
            200,
            json={"pk": pk, "product": product, "catalog": catalog,
                  "modified_date": modified.isoformat()},
        )

    if path.rstrip("/") == "/api/v1/products" and method == "GET":
        if params.get("sku__in") is not None:
            wanted = [world.by_sku[s] for s in _ids(params.get("sku__in")) if s in world.by_sku]
        elif params.get("pk__in") is not None:
            wanted = [int(p) for p in _ids(params.get("pk__in")) if int(p) in world.products]
        else:
            wanted = sorted(world.products)
        records = [world.products[pk].record(locale) for pk in sorted(set(wanted))]
        return _page(request, records)
    if match := _PRODUCT.match(path):
        pk = int(match.group(1))
        if pk not in world.products:
            return _not_found("product")
        if method == "PATCH":
            body = json.loads(request.content or b"{}")
            world.patch(pk, dict(body.get("attributes") or {}), locale)
            return httpx.Response(200, json=world.products[pk].record(locale))
        return httpx.Response(200, json=world.products[pk].record(locale))

    if method == "GET" and path.rstrip("/") == "/api/v1/product_price":
        since = _when(params.get("modified_date__gte"))
        floor = float(params["price__gt"]) if params.get("price__gt") else None
        records = [
            r for r in world.prices
            if (not params.get("price_list") or str(r["price_list"]) == params["price_list"])
            and (floor is None or float(r["price"]) > floor)
            and (since is None or (_when(r["modified_date"]) or since) >= since)
        ]
        return _page(request, records)
    if method == "GET" and path.rstrip("/") == "/api/v1/product_stock":
        since = _when(params.get("modified_date__gte"))
        floor = float(params["stock__gt"]) if params.get("stock__gt") else None
        records = [
            r for r in world.stocks
            if (not params.get("stock_list") or str(r["stock_list"]) == params["stock_list"])
            and (floor is None or r["stock"] > floor)
            and (since is None or (_when(r["modified_date"]) or since) >= since)
        ]
        return _page(request, records)
    if method == "GET" and path.rstrip("/") == "/api/v1/deleted_items":
        since = _when(params.get("deleted_date__gte"))
        records = [
            r for r in world.deleted
            if (not params.get("content_type_model")
                or r["content_type_model"] == params["content_type_model"])
            and (since is None or (_when(r["deleted_date"]) or since) >= since)
        ]
        return _page(request, records)

    return _not_found(f"{method} {path}")


class MockPim(OmnitronClient):
    """`OmnitronClient`, unchanged, talking to the demo world instead of a tenant.

    Construction never reads the PIM settings: the demo must not depend on
    whatever `OMNITRON_BASE_URL` happens to be, and must never be able to
    reach a real tenant by inheriting one.
    """

    def __init__(self, world: World | None = None, timeout: float = 30.0) -> None:
        super().__init__(
            base_url=BASE_URL, username="demo", password="demo", timeout=timeout
        )
        self.world = world or current()
        self._http.close()
        # Imported here: `web` routes other hosts and imports this module.
        from .web import transport

        self._http = httpx.Client(
            transport=transport(self.world), timeout=timeout, follow_redirects=False
        )
