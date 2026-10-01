"""Reading the shops, and refusing to guess when the read goes wrong.

The stage is a loop over four public URLs, so the behaviours worth pinning are
not the happy path. They are the two ways a listing read can lie — a pull that
stops early, and a pull that answers for the wrong market — because both come
back as a plausible smaller number, and the sweep downstream acts on it.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import StorefrontListing
from pim_studio.sync import storefront
from pim_studio.sync.runs import SyncRun
from pim_studio.sync.storefront import (
    STOREFRONTS,
    StorefrontReadFailed,
    load_listings,
    read_listing,
    sync_storefront,
)

pytestmark = pytest.mark.usefixtures("temp_db")

MARKETS = ("PL", "CZ", "SK", "HU")


def option(mc: str) -> dict:
    """One listing row, shaped as the storefront sends it."""
    base, colour = mc.split(".", 1)
    return {
        "pk": abs(hash(mc)) % 10**6,
        "sku": str(abs(hash(mc)) % 10**7),
        "base_code": base,
        "listing_code": colour,
        "attributes": {"integration_model_renk": mc},
    }


def shop(per_market: dict[str, list[str]], *, page_size: int = 100,
         claim_pages: int | None = None, claim_total: int | None = None,
         stop_after: int | None = None, seen: dict | None = None):
    """A client answering `/list` for each market, keyed off the cookie.

    The cookie *is* the market selector on these sites, so the fake keys off it
    too — a stage that stopped sending it would otherwise still pass here while
    returning another channel's range in production.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("Cookie", "")
        market = next(
            (m for m, s in STOREFRONTS.items() if s.cookie == cookie), None
        )
        if seen is not None:
            seen.setdefault(market, []).append(str(request.url))
        if market is None:
            return httpx.Response(200, json={
                "pagination": {"num_pages": 1, "total_count": 0}, "products": []
            })
        options = per_market.get(market, [])
        pages = max(1, -(-len(options) // page_size))
        page = int(request.url.params.get("page", 1))
        chunk = options[(page - 1) * page_size : page * page_size]
        # `stop_after` is the degradation that matters: the server keeps
        # answering 200 with the same pagination and simply stops returning
        # products. Every page-based check passes; the set is half missing.
        if stop_after is not None and page > stop_after:
            chunk = []
        return httpx.Response(200, json={
            "pagination": {
                "current_page": page,
                "num_pages": claim_pages or pages,
                "page_size": page_size,
                "total_count": claim_total or len(options),
            },
            "products": [option(mc) for mc in chunk],
        })

    return httpx.Client(transport=httpx.MockTransport(handler))


def a_run() -> SyncRun:
    return SyncRun(row_id=1, kind="storefront")


def listings(market: str, *, live_only: bool = True) -> list[StorefrontListing]:
    with session_scope() as db:
        rows = db.scalars(
            select(StorefrontListing)
            .where(StorefrontListing.market == market)
            .order_by(StorefrontListing.model_color)
        ).all()
    return [r for r in rows if r.left_at is None] if live_only else list(rows)


def a_range(n: int, prefix: str = "L") -> list[str]:
    return [f"{prefix}{i:04d}.166" for i in range(n)]


# ── reading ────────────────────────────────────────────────────────────────


def test_every_market_is_read_and_recorded() -> None:
    ranges = {m: a_range(2_100, m) for m in MARKETS}
    stats = sync_storefront(a_run(), client=shop(ranges))

    assert stats["markets_read"] == 4
    assert stats["markets_failed"] == 0
    for m in MARKETS:
        assert stats[f"listed_{m}"] == 2_100
        assert len(listings(m)) == 2_100


def test_the_cookie_is_what_selects_the_market() -> None:
    """Without `pz-locale` these sites answer 200 with another channel's much
    smaller range, so a stage that dropped the header would read as three
    quarters of the catalogue disappearing rather than as a bug."""
    seen: dict = {}
    sync_storefront(a_run(), client=shop({m: a_range(2_100, m) for m in MARKETS},
                                         seen=seen))
    assert set(seen) == set(MARKETS), "each market must be reached under its own cookie"


def test_an_option_we_do_not_hold_is_still_recorded() -> None:
    """57 options were selling in PL that our product table did not contain.
    There is no foreign key precisely so those appear."""
    sync_storefront(a_run(), markets=("PL",),
                    client=shop({"PL": a_range(2_100) + ["NOTOURS.001"]}))
    assert "NOTOURS.001" in {r.model_color for r in listings("PL")}


# ── movement ───────────────────────────────────────────────────────────────


def test_an_option_that_stops_being_listed_is_marked_gone_not_deleted() -> None:
    full = a_range(2_100)
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full}))
    stats = sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full[:-3]}))

    assert stats["left_PL"] == 3
    assert len(listings("PL")) == 2_097
    assert len(listings("PL", live_only=False)) == 2_100, (
        "the row is kept so the day it went can be read off it"
    )


def test_an_option_that_comes_back_is_live_again() -> None:
    full = a_range(2_100)
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full}))
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full[:-1]}))
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full}))

    assert len(listings("PL")) == 2_100
    with session_scope() as db:
        row = db.scalars(
            select(StorefrontListing).where(
                StorefrontListing.model_color == full[-1],
                StorefrontListing.market == "PL",
            )
        ).one()
    assert row.left_at is None
    assert row.first_seen_at < row.last_seen_at, "it is the same row, not a new one"


# ── the two ways a read lies ───────────────────────────────────────────────


def test_a_badly_short_pull_raises() -> None:
    """The server here behaves exactly as a degraded one does: 200 on every
    page, the same pagination throughout, and no products after page 10. Paging
    to the last page succeeds; the row count is what catches it."""
    client = shop({"PL": a_range(2_100)}, stop_after=10)
    with pytest.raises(StorefrontReadFailed, match="1,000 of the 2,100"):
        read_listing(STOREFRONTS["PL"], client)


def test_a_slightly_short_pull_is_normal_and_is_not_a_failure() -> None:
    """The index is live and a crawl takes about ninety seconds, so options
    leave while it is being walked and each departure costs an item at a page
    boundary. The first real run saw Hungary return 4,332 of 4,346 — 0.3%, and
    the first version of this guard called it broken."""
    listing = read_listing(
        STOREFRONTS["PL"], shop({"PL": a_range(2_100)}, claim_total=2_114)
    )
    assert listing.shortfall == 14
    assert listing.complete is False
    assert len(listing.options) == 2_100, "what was seen is still recorded"


def test_a_skewed_pull_records_arrivals_but_holds_departures_over() -> None:
    """A late departure instead of a false one. What was seen is additive and
    safe; what was not seen is only evidence of departure on a whole read."""
    full = a_range(2_100)
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full}))

    stats = sync_storefront(
        a_run(), markets=("PL",),
        client=shop({"PL": full[:-6] + ["NEW.001"]}, claim_total=2_101),
    )

    assert stats["shortfall_PL"] == 6
    assert stats["left_PL"] == 0, "a skewed read must not delist"
    assert stats["added_PL"] == 1, "but it still records what turned up"
    assert len(listings("PL")) == 2_101

    # The next clean read is what settles it.
    stats = sync_storefront(
        a_run(), markets=("PL",), client=shop({"PL": full[:-6] + ["NEW.001"]})
    )
    assert stats["left_PL"] == 6
    assert len(listings("PL")) == 2_095


def test_a_wildly_wrong_page_count_is_refused_before_the_crawl() -> None:
    """A ceiling, so a broken `num_pages` costs one request rather than a
    morning of them."""
    with pytest.raises(StorefrontReadFailed, match="over the ceiling"):
        read_listing(STOREFRONTS["PL"], shop({"PL": a_range(2_100)}, claim_pages=5_000))


def test_a_short_market_is_skipped_and_its_listings_left_alone() -> None:
    full = a_range(2_100)
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full}))

    stats = sync_storefront(
        a_run(), markets=("PL",), client=shop({"PL": full}, stop_after=10)
    )

    assert stats["markets_failed"] == 1
    assert stats["left_PL"] == 0
    assert len(listings("PL")) == 2_100, "a failed read must not delist anything"


def test_a_range_below_the_floor_is_treated_as_a_broken_read() -> None:
    """The wrong-cookie failure returns another channel's 1,433 products with a
    200. The four markets sat between 4,292 and 4,592, so the floor separates
    them with room for a bad season either side."""
    with pytest.raises(StorefrontReadFailed, match="below the floor"):
        read_listing(STOREFRONTS["PL"], shop({"PL": a_range(1_433)}))


def test_one_market_failing_does_not_stop_the_others() -> None:
    """Three markets measured beats four markets stale — as long as the failure
    is reported rather than folded into the numbers."""
    ranges = {m: a_range(2_100, m) for m in MARKETS}
    ranges["CZ"] = a_range(10, "CZ")

    stats = sync_storefront(a_run(), client=shop(ranges))

    assert stats["markets_read"] == 3
    assert stats["markets_failed"] == 1
    assert "error_CZ" in stats
    assert stats["listed_PL"] == 2_100
    assert listings("CZ") == []


# ── reading it back ────────────────────────────────────────────────────────


def test_load_listings_excludes_departures_and_carries_the_observation_time() -> None:
    full = a_range(2_100)
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full}))
    sync_storefront(a_run(), markets=("PL",), client=shop({"PL": full[:-5]}))

    with session_scope() as db:
        listed, observed_at = load_listings(db)
    assert len(listed["PL"]) == 2_095
    assert observed_at is not None, "a live count with no as-of is unreadable"


def test_a_market_never_read_comes_back_empty_rather_than_absent() -> None:
    """Empty must be distinguishable from "nothing is live" by the caller, so
    the key is always present and `FunnelData.is_live` treats it as unknown."""
    with session_scope() as db:
        listed, observed_at = load_listings(db)
    assert set(listed) == set(MARKETS)
    assert all(not v for v in listed.values())
    assert observed_at is None


def test_the_floor_is_below_every_market_and_above_the_wrong_cookie_answer() -> None:
    """A regression guard on the constant itself: the gap it sits in is
    narrow, and both edges were measured on 2026-09-07."""
    assert 1_433 < storefront._LISTING_FLOOR < 4_292
