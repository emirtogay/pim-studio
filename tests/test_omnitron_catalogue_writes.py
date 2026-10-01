"""The catalogue door — deleting a product from a catalogue, and putting it back.

This is the write that can stop a product selling, so the terms are stricter
than the attribute door's: two switches rather than one, and an allowlist
checked against what Omnitron says the row is, not against what the caller
claimed alongside it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pim_studio.omnitron import OmnitronClient, ReadOnlyViolation
from pim_studio.omnitron.client import WriteScopeViolation

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}


def logged_in(handler) -> OmnitronClient:
    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    return c


def _both_switches_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_takedown_enabled", True)


def item_server(catalog: int = 601, product: int = 357545, *, seen: list | None = None):
    """A tenant holding one catalog_item, pk 900."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200, json={"pk": 900, "product": product, "catalog": catalog}
            )
        return httpx.Response(204)

    return handler


# ── the switches ───────────────────────────────────────────────────────────


def test_catalogue_writes_need_their_own_switch(monkeypatch) -> None:
    """Turning on attribute pushes must not grant catalogue deletes.

    They are different decisions — one about text, one about whether the panel
    may stop a product selling — and this is the test that keeps them apart.
    """
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_takedown_enabled", False)
    seen: list = []
    c = logged_in(item_server(seen=seen))
    with pytest.raises(ReadOnlyViolation, match="OMNITRON_TAKEDOWN_ENABLED"):
        c.delete_catalog_item(900, allowed_catalogs=[601])
    assert seen == []


def test_the_takedown_switch_alone_is_not_enough(monkeypatch) -> None:
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", False)
    monkeypatch.setattr(settings, "omnitron_takedown_enabled", True)
    seen: list = []
    c = logged_in(item_server(seen=seen))
    with pytest.raises(ReadOnlyViolation, match="OMNITRON_WRITES_ENABLED"):
        c.delete_catalog_item(900, allowed_catalogs=[601])
    assert seen == []


# ── the allowlist ──────────────────────────────────────────────────────────


def test_a_row_outside_the_allowlist_is_not_deleted(monkeypatch) -> None:
    """The failure this guard exists for: a PL takedown reaching HU.

    The item is read first, and the catalogue checked is the one Omnitron
    reports — so a caller working from a muddled pk is stopped by the tenant's
    own answer rather than by its own arithmetic.
    """
    _both_switches_on(monkeypatch)
    seen: list = []
    c = logged_in(item_server(catalog=41, seen=seen))
    with pytest.raises(WriteScopeViolation, match="41"):
        c.delete_catalog_item(900, allowed_catalogs=[4, 40, 601])
    assert [m for m, _ in seen] == ["GET"], "it read, and then it stopped"


def test_an_empty_allowlist_permits_nothing(monkeypatch) -> None:
    """Rather than everything, which is the more common default and the wrong
    one for a delete."""
    _both_switches_on(monkeypatch)
    seen: list = []
    c = logged_in(item_server(seen=seen))
    with pytest.raises(WriteScopeViolation, match="allowlist permits nothing"):
        c.delete_catalog_item(900, allowed_catalogs=[])
    assert seen == []


def test_a_constructed_item_id_is_refused(monkeypatch) -> None:
    _both_switches_on(monkeypatch)
    for bad in (0, -1, True, "900", None):
        c = logged_in(item_server())
        with pytest.raises(WriteScopeViolation):
            c.delete_catalog_item(bad, allowed_catalogs=[601])


# ── the delete ─────────────────────────────────────────────────────────────


def test_a_permitted_delete_reads_then_deletes_by_item_id(monkeypatch) -> None:
    _both_switches_on(monkeypatch)
    seen: list = []
    c = logged_in(item_server(seen=seen))
    out = c.delete_catalog_item(900, allowed_catalogs=[4, 40, 601])
    assert seen == [
        ("GET", "/api/v1/catalog_items/900/"),
        ("DELETE", "/api/v1/catalog_items/900/"),
    ]
    assert out == {"pk": 900, "product": 357545, "catalog": 601}


def test_the_delete_returns_what_a_restore_needs(monkeypatch) -> None:
    """After the delete this is the only place the pair exists upstream or
    down, so returning it is not a convenience."""
    _both_switches_on(monkeypatch)
    c = logged_in(item_server(catalog=41, product=12345))
    out = c.delete_catalog_item(900, allowed_catalogs=[41])
    assert out["product"] == 12345
    assert out["catalog"] == 41


def test_an_item_with_no_catalog_is_refused_rather_than_guessed(monkeypatch) -> None:
    _both_switches_on(monkeypatch)
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"pk": 900})
        return httpx.Response(204)

    c = logged_in(handler)
    with pytest.raises(WriteScopeViolation, match="cannot describe"):
        c.delete_catalog_item(900, allowed_catalogs=[601])
    assert seen == ["GET"]


# ── the restore ────────────────────────────────────────────────────────────


def test_a_restore_posts_the_pair_back(monkeypatch) -> None:
    _both_switches_on(monkeypatch)
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["method"] = request.method
        sent["path"] = request.url.path
        sent["body"] = json.loads(request.content)
        return httpx.Response(201, json={"pk": 901, "product": 357545, "catalog": 601})

    c = logged_in(handler)
    c.create_catalog_item(product_pk=357545, catalog_pk=601, allowed_catalogs=[601])
    assert sent["method"] == "POST"
    assert sent["path"] == "/api/v1/catalog_items/"
    assert sent["body"] == {"product": 357545, "catalog": 601}, (
        "the pair and nothing else — a restore is not a place to set fields"
    )


def test_a_restore_into_a_catalogue_outside_the_allowlist_is_refused(monkeypatch) -> None:
    """An undo is still a write, and putting a product into a market that
    never had it is the same class of mistake as taking it out of one."""
    _both_switches_on(monkeypatch)
    seen: list = []
    c = logged_in(item_server(seen=seen))
    with pytest.raises(WriteScopeViolation, match="41"):
        c.create_catalog_item(product_pk=1, catalog_pk=41, allowed_catalogs=[601])
    assert seen == []


def test_the_restore_is_shut_by_the_same_switches(monkeypatch) -> None:
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_takedown_enabled", False)
    seen: list = []
    c = logged_in(item_server(seen=seen))
    with pytest.raises(ReadOnlyViolation):
        c.create_catalog_item(product_pk=1, catalog_pk=601, allowed_catalogs=[601])
    assert seen == []


# ── reading membership ─────────────────────────────────────────────────────


def test_membership_is_read_one_catalogue_at_a_time() -> None:
    """`catalog__in` is silently ignored upstream — it returns the unfiltered
    set — which `sync/catalogue.py` found the hard way. One pass per
    catalogue, and the filter proven by the request that goes out.
    """
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        catalog = int(request.url.params["catalog"])
        return httpx.Response(
            200,
            json={
                "results": [{"pk": catalog * 10, "product": 7, "catalog": catalog}],
                "next": None,
            },
        )

    c = logged_in(handler)
    rows = c.catalog_items_for_product(7, [601, 4])
    assert [r["catalog"] for r in rows] == [4, 601], "sorted, one call each"
    assert len(asked) == 2
    assert all("catalog__in" not in url for url in asked)


def test_a_row_for_another_product_is_dropped_not_trusted() -> None:
    """Belt and braces on a filter the tenant has ignored before."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"pk": 1, "product": 7, "catalog": 601},
                    {"pk": 2, "product": 999, "catalog": 601},
                ],
                "next": None,
            },
        )

    c = logged_in(handler)
    rows = c.catalog_items_for_product(7, [601])
    assert [r["pk"] for r in rows] == [1]
