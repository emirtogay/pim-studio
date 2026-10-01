"""Batched reads of our own SKU set.

The two behaviours worth pinning are both silent failures: a batch that
degrades to one result because the filter was passed as a list, and SKUs that
came back missing and were never counted.
"""

from __future__ import annotations

import httpx
import pytest

from pim_studio.omnitron import OmnitronClient
from pim_studio.sync.batching import (
    SKU_BATCH_SIZE,
    chunked,
    fetch_by_sku,
    fetch_by_sku_locales,
)

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}


def api(handler) -> OmnitronClient:
    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    return c


def products_handler(known: set[str], record_calls: list[httpx.URL] | None = None):
    """Serves only the SKUs it knows about, like the real endpoint does."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record_calls is not None:
            record_calls.append(request.url)
        raw = request.url.params.get("sku__in", "")
        asked = [s for s in raw.split(",") if s]
        results = [{"sku": s, "name": f"product {s}"} for s in asked if s in known]
        return httpx.Response(200, json={"count": len(results), "next": None, "results": results})

    return handler


# ── chunking ───────────────────────────────────────────────────────────────


def test_chunked_splits_evenly_and_keeps_the_remainder() -> None:
    assert list(chunked(["a", "b", "c", "d", "e"], 2)) == [["a", "b"], ["c", "d"], ["e"]]


def test_chunked_on_empty_yields_nothing() -> None:
    assert list(chunked([], 10)) == []


def test_chunked_rejects_a_zero_size() -> None:
    """Silently looping forever is the alternative."""
    with pytest.raises(ValueError):
        list(chunked(["a"], 0))


def test_default_batch_matches_akinons_own_ceiling() -> None:
    assert SKU_BATCH_SIZE == 100


# ── the list-vs-string trap ────────────────────────────────────────────────


def test_sku_in_is_sent_as_one_comma_joined_parameter() -> None:
    """The trap this guards: passed a Python list, httpx emits repeated
    `sku__in=` parameters, Omnitron honours one, and a batch of 100 silently
    becomes a batch of 1 while returning HTTP 200."""
    calls: list[httpx.URL] = []
    skus = [str(n) for n in range(10)]
    list(fetch_by_sku(api(products_handler(set(skus), calls)), skus, batch_size=10))

    assert len(calls) == 1
    values = calls[0].params.get_list("sku__in")
    assert len(values) == 1, "sku__in must be a single comma-joined parameter"
    assert values[0] == ",".join(skus)


# ── missing SKUs are reported, not lost ────────────────────────────────────


def test_missing_skus_are_reported_separately() -> None:
    skus = ["1", "2", "3", "4"]
    batches = list(fetch_by_sku(api(products_handler({"1", "3"})), skus, batch_size=4))

    assert len(batches) == 1
    got = batches[0]
    assert sorted(got.found) == ["1", "3"]
    assert got.missing == ["2", "4"]
    assert got.requested == 4


def test_every_sku_missing_is_not_an_error() -> None:
    """A batch where nothing resolves is a finding for the drop tally, not an
    exception — the run should carry on and report it."""
    batches = list(fetch_by_sku(api(products_handler(set())), ["1", "2"], batch_size=2))
    assert batches[0].found == {}
    assert batches[0].missing == ["1", "2"]


def test_batches_are_yielded_one_at_a_time() -> None:
    """So a 26,813-SKU run writes as it goes and a crash keeps what it had."""
    skus = [str(n) for n in range(250)]
    batches = list(fetch_by_sku(api(products_handler(set(skus))), skus, batch_size=100))
    assert [b.requested for b in batches] == [100, 100, 50]
    assert sum(len(b.found) for b in batches) == 250


def test_extra_params_cannot_override_the_batching() -> None:
    calls: list[httpx.URL] = []
    list(
        fetch_by_sku(
            api(products_handler({"1"}, calls)),
            ["1"],
            batch_size=50,
            params={"sku__in": "999", "limit": 1, "is_active": "true"},
        )
    )
    assert calls[0].params.get("sku__in") == "1"
    assert calls[0].params.get("limit") == "50"
    assert calls[0].params.get("is_active") == "true"


# ── locale: a header on every request, or on none ──────────────────────────


def header_recording_handler(known: set[str], seen: list[tuple[str | None, str]]):
    """Like `products_handler`, but records (Accept-Language, sku__in) pairs —
    the two facts the locale-aware fetch lives or dies by."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.headers.get("accept-language"), request.url.params.get("sku__in", ""))
        )
        asked = [s for s in request.url.params.get("sku__in", "").split(",") if s]
        results = [{"sku": s} for s in asked if s in known]
        return httpx.Response(200, json={"count": len(results), "next": None, "results": results})

    return handler


def test_fetch_by_sku_passes_the_locale_on_every_request() -> None:
    seen: list[tuple[str | None, str]] = []
    skus = [str(n) for n in range(4)]
    list(
        fetch_by_sku(
            api(header_recording_handler(set(skus), seen)), skus,
            batch_size=2, locale="pl",
        )
    )
    assert [loc for loc, _ in seen] == ["pl", "pl"]


def test_fetch_by_sku_sends_no_locale_header_by_default() -> None:
    """No header means the tenant default answers — today's behaviour, and it
    must stay the default so `products.py`'s identity pass is untouched."""
    seen: list[tuple[str | None, str]] = []
    list(fetch_by_sku(api(header_recording_handler({"1"}, seen)), ["1"]))
    assert seen == [(None, "1")]


def test_fetch_by_sku_locales_fetches_each_chunk_under_each_view() -> None:
    seen: list[tuple[str | None, str]] = []
    skus = [str(n) for n in range(4)]
    views_per_chunk = list(
        fetch_by_sku_locales(
            api(header_recording_handler(set(skus), seen)), skus,
            locales=(None, "pl", "cs"), batch_size=2,
        )
    )
    # 2 chunks × 3 views, each chunk finished before the next begins.
    assert seen == [
        (None, "0,1"), ("pl", "0,1"), ("cs", "0,1"),
        (None, "2,3"), ("pl", "2,3"), ("cs", "2,3"),
    ]
    assert all(set(views) == {None, "pl", "cs"} for views in views_per_chunk)
    assert sorted(views_per_chunk[0]["pl"].found) == ["0", "1"]


def test_fetch_by_sku_locales_requires_the_baseline_first() -> None:
    """`missing` is only authoritative on the locale-less view, and resolution
    compares against it — a caller who forgot it should hear so immediately."""
    with pytest.raises(ValueError, match="baseline"):
        list(fetch_by_sku_locales(api(products_handler(set())), ["1"], locales=("pl",)))


def test_fetch_by_sku_locales_missing_is_judged_from_the_baseline_view() -> None:
    views = next(
        iter(
            fetch_by_sku_locales(
                api(products_handler({"1"})), ["1", "2"], locales=(None, "pl")
            )
        )
    )
    assert views[None].missing == ["2"]
    assert views["pl"].missing == ["2"]
