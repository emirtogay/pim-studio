"""Asking the shop what it is selling, instead of working it out.

Liveness has been computed from Omnitron since this project started, and the
computation has been wrong in ways nothing could see. On 2026-09-07 the panel
said 3,931 options were live in Poland and the Looker control table said 3,995;
example.com said 4,592. The two derived answers were close to each other and
both ~600 short, because both read catalogue 4, whose channel `SHOP PL` is
`is_active: false` — Poland having been moved to catalogue 601. Agreement
between two sources that share their plumbing is not corroboration.

So this stage reads the storefronts. It is the only source here that is not a
report about the shop; it is the shop.

## What it costs, and therefore how often it can run

    /list?format=json&page_size=100&page=N

returns the whole live population with attributes — 46 requests for Poland's
4,592 options, about three minutes for all four markets. Two pulls of PL two
hours apart on 2026-09-07 differed by 7 options leaving and 1 joining, 0.17% of
the set. So the useful cadence is hours: every two hours holds the number
inside ~0.2% all day, and anything finer measures noise.

## Two things that will break this, stated so they break loudly

**The market is selected by a cookie.** `pz-locale` and `pz-currency`, nothing
else. Without them the same URL answers 200 with some other channel's 1,433
products, which would read as Poland losing two thirds of its range overnight.
`_LISTING_FLOOR` is the guard: a market answering with implausibly few options
is treated as a broken read, not as news.

**A short read must never be able to delist a catalogue.** The sweep works by
subtraction, so a pull that stops early looks exactly like every missing option
having gone. `read_listing` therefore checks the rows it collected against the
`total_count` the first response committed to, and raises rather than returning
a partial set; the market is then skipped entirely rather than swept. Counting
*pages* would not do — a server that has stopped returning products still
answers the remaining pages with an empty list and a 200, satisfying the page
count while the set is half missing. This is the same hazard
`catalogue._write_memberships` guards with `require_full`, but worse: there is
no incremental mode here to hide behind, because 46 pages *is* the whole set
and every pull is a full one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select

from .. import integrations
from ..config import settings
from ..db import session_scope
from ..logging import log
from ..models import StorefrontListing, utcnow
from .runs import SyncRun
from .scope import MARKET_CODES


class StorefrontReadFailed(RuntimeError):
    """A market's pull did not complete, so its result must not be believed.

    Raised rather than returned because a partial listing is not a smaller
    listing — it is indistinguishable from every option it lacks having been
    withdrawn, and the sweep downstream would act on that.
    """


@dataclass(frozen=True)
class Storefront:
    """One market's public shop, and the cookie that selects it."""

    market: str
    origin: str
    #: `pz-locale` / `pz-currency`. The entire market selector: these sites
    #: share an origin-shaped API and differ only by this pair.
    cookie: str


STOREFRONTS: dict[str, Storefront] = {
    "PL": Storefront("PL", "https://northwind-pl.example.test", "pz-locale=pl; pz-currency=pln"),
    "CZ": Storefront("CZ", "https://northwind-cz.example.test", "pz-locale=cs; pz-currency=czk"),
    "SK": Storefront("SK", "https://northwind-sk.example.test", "pz-locale=sk; pz-currency=eur"),
    "HU": Storefront("HU", "https://northwind-hu.example.test", "pz-locale=hu; pz-currency=huf"),
}

#: The storefront's own ModelColor, and our join key. Carried by 4,577 of PL's
#: 4,592 rows; `base_code` + `listing_code` covers the rest and agrees with it
#: wherever both are present.
_MC_ATTR = "integration_model_renk"

#: Rows per request. 100 is honoured; `limit`, `size` and `per_page` are
#: silently ignored and leave you on 24, which is only visible as the pull
#: taking four times as long.
_PAGE_SIZE = 100

#: Below this, a market's pull is treated as broken rather than as a collapse
#: in the range. The four markets sat between 4,292 and 4,592 on 2026-09-07 and
#: the wrong-cookie failure mode returns 1,433, so this separates the two with
#: room for a genuinely bad season either side.
_LISTING_FLOOR = 2_000


def listing_floor(default: int) -> int:
    """The floor in force: `STOREFRONT_LISTING_FLOOR` when set, else `default`.

    Unset everywhere but demo mode, whose shops sell a few hundred options.
    `default` is the caller's own constant, read at call time, so a test that
    lowers `_LISTING_FLOOR` in its module still lowers the floor applied.
    """
    configured = settings.storefront_listing_floor
    return default if configured is None else configured

#: A ceiling on the crawl, so a wrong `num_pages` cannot spin. 46 pages covers
#: Poland's 4,592; this is four times that and still an order of magnitude
#: below anything a runaway would reach.
_MAX_PAGES = 200

#: How far short of `total_count` a pull may come and still be believed.
#:
#: The index is live and a crawl takes about ninety seconds, so options leave
#: it while we are walking it — and each departure shifts everything after it
#: back one, which costs an item at a page boundary. The first real run saw
#: Hungary return 4,332 of a promised 4,346: 0.3%, entirely normal, and the
#: first version of this guard called it a failure.
#:
#: The tolerance is not a licence to sweep on a short read. A pull that comes
#: up short is *incomplete* — it is recorded, and its departure sweep is
#: skipped — so the cost of skew is a slightly late departure rather than a
#: false one. Above the tolerance the read is refused outright, which is the
#: server-degrading case: a shop that has stopped returning products answers
#: the rest of the pages empty and would otherwise look like a collapse in
#: range.
_SHORTFALL_TOLERANCE = 0.02

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def _option_key(row: dict[str, Any]) -> str | None:
    attributes = row.get("attributes") or {}
    key = (attributes.get(_MC_ATTR) or "").strip().upper()
    if key:
        return key
    base = (row.get("base_code") or "").strip().upper()
    colour = (row.get("listing_code") or "").strip().upper()
    return f"{base}.{colour}" if base and colour else None


@dataclass(frozen=True)
class Listing:
    """One market's pull, and whether it may be believed *as a whole*.

    `complete` is the distinction that matters downstream. Options seen are
    always safe to record — they are additive. Options *not* seen are only
    evidence of departure when the read was whole, and a live index guarantees
    it often is not.
    """

    options: set[str]
    rows: int
    promised: int
    complete: bool

    @property
    def shortfall(self) -> int:
        return max(0, self.promised - self.rows)


def read_listing(
    shop: Storefront, client: httpx.Client | None = None
) -> Listing:
    """Every option one storefront is selling, and whether the pull was whole.

    **The check that matters is rows against `total_count`, not pages against
    `num_pages`.** Paging to the promised last page proves nothing: a server
    that has stopped returning products answers those pages with an empty list
    and a 200, so the page count is satisfied while the set is half missing.
    The row count is the only figure the first response commits to that a
    degraded crawl cannot also satisfy.

    A small shortfall is normal and is reported rather than raised — see
    `_SHORTFALL_TOLERANCE`. Beyond it, `StorefrontReadFailed`, because the
    caller's sweep works by subtraction and cannot tell a broken read from a
    market withdrawing its range.
    """
    owned = client is None
    http = client or integrations.http_client(timeout=_TIMEOUT, follow_redirects=True)
    try:
        options: set[str] = set()
        rows = pages = 0
        page = 1
        expected: int | None = None
        promised: int | None = None
        while True:
            response = http.get(
                f"{shop.origin}/list",
                params={"format": "json", "page_size": _PAGE_SIZE, "page": page},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "pim-studio/storefront",
                    "Cookie": shop.cookie,
                },
            )
            if response.status_code != httpx.codes.OK:
                raise StorefrontReadFailed(
                    f"{shop.market}: page {page} returned HTTP {response.status_code}"
                )
            body = response.json()
            pagination = body.get("pagination") or {}
            if expected is None:
                expected = int(pagination.get("num_pages") or 0)
                promised = int(pagination.get("total_count") or 0)
                if expected > _MAX_PAGES:
                    raise StorefrontReadFailed(
                        f"{shop.market}: claims {expected} pages, over the "
                        f"ceiling of {_MAX_PAGES}"
                    )
            for row in body.get("products") or []:
                rows += 1
                key = _option_key(row)
                if key:
                    options.add(key)
            pages += 1
            if page >= (expected or 0):
                break
            page += 1

        promised = promised or 0
        shortfall = max(0, promised - rows)
        if promised and shortfall > promised * _SHORTFALL_TOLERANCE:
            raise StorefrontReadFailed(
                f"{shop.market}: returned {rows:,} of the {promised:,} options "
                f"it said it had, over {pages} pages — more than "
                f"{_SHORTFALL_TOLERANCE:.0%} short"
            )
        floor = listing_floor(_LISTING_FLOOR)
        if len(options) < floor:
            raise StorefrontReadFailed(
                f"{shop.market}: {len(options)} options is below the floor of "
                f"{floor:,} — a wrong or missing pz-locale cookie "
                f"returns another channel's much smaller range"
            )
        if shortfall:
            log.info(
                "storefront_read_skewed",
                market=shop.market, rows=rows, promised=promised,
                shortfall=shortfall,
                note="the index changed mid-crawl; departures not swept",
            )
        return Listing(options, rows, promised, complete=not shortfall)
    finally:
        if owned:
            http.close()


@dataclass
class MarketResult:
    market: str
    listed: int = 0
    added: int = 0
    returned: int = 0
    left: int = 0
    #: Rows the shop promised and did not deliver. Non-zero suppresses the
    #: departure sweep for this market.
    shortfall: int = 0
    error: str = ""
    options: set[str] = field(default_factory=set)


def sync_storefront(
    run: SyncRun,
    markets: tuple[str, ...] = MARKET_CODES,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Pull each storefront and record what it is showing.

    Takes no `OmnitronClient`: this reads a different system — the public shop,
    over plain HTTP, with no credentials. The stage signature in `__main__`
    still accepts one because every stage does, and it is ignored there rather
    than given a second shape for one caller.

    Each market is independent. One failing is recorded and the others still
    run, because three markets measured beats four markets stale — and the
    failure is reported rather than folded into the numbers.
    """
    now = utcnow()
    results: list[MarketResult] = []

    for code in markets:
        shop = STOREFRONTS.get(code)
        if shop is None:
            results.append(MarketResult(code, error="no storefront configured"))
            continue
        result = MarketResult(code)
        try:
            listing = read_listing(shop, client)
            result.options = listing.options
            result.listed = len(listing.options)
            result.shortfall = listing.shortfall
            run.seen(listing.rows)
        except (StorefrontReadFailed, httpx.HTTPError, ValueError) as exc:
            result.error = str(exc)
            run.dropped(reason=f"{code}: storefront read failed")
            log.warning("storefront_read_failed", market=code, error=str(exc))
            results.append(result)
            continue
        _record(result, now, sweep=listing.complete)
        results.append(result)
        log.info(
            "storefront_market",
            market=code, listed=result.listed, added=result.added,
            returned=result.returned, left=result.left,
            shortfall=result.shortfall, swept=listing.complete,
        )

    run.imported(sum(r.listed for r in results if not r.error))
    stats: dict[str, Any] = {
        "markets_read": sum(1 for r in results if not r.error),
        "markets_failed": sum(1 for r in results if r.error),
        "observed_at": now.isoformat(),
    }
    for r in results:
        stats[f"listed_{r.market}"] = r.listed
        stats[f"added_{r.market}"] = r.added
        stats[f"left_{r.market}"] = r.left
        stats[f"shortfall_{r.market}"] = r.shortfall
        if r.error:
            stats[f"error_{r.market}"] = r.error
    log.info("storefront_synced", **stats)
    return stats


def _record(result: MarketResult, now: datetime, *, sweep: bool) -> None:
    """Write one market's observation: what is new, what is still there, and —
    only if the pull was whole — what has gone.

    What was *seen* is always safe to write: it is additive, and an option
    observed on sale is on sale. What was *not* seen only means departure when
    the read was complete, which on a live index it often is not. So `sweep`
    is passed rather than assumed, and a skewed pull records arrivals while
    leaving departures for the next clean read — a late departure instead of a
    false one.
    """
    with session_scope() as db:
        held = {
            row.model_color: row
            for row in db.scalars(
                select(StorefrontListing).where(
                    StorefrontListing.market == result.market
                )
            )
        }
        for option in result.options:
            row = held.get(option)
            if row is None:
                db.add(
                    StorefrontListing(
                        model_color=option,
                        market=result.market,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                result.added += 1
                continue
            row.last_seen_at = now
            if row.left_at is not None:
                # Back on sale. Said out loud rather than silently cleared: an
                # option returning is a restock or a reinstatement, and both
                # are events somebody is waiting on.
                log.info(
                    "storefront_option_returned",
                    market=result.market, model_color=option,
                    left_at=str(row.left_at),
                )
                row.left_at = None
                result.returned += 1

        if not sweep:
            return
        for option, row in held.items():
            if option not in result.options and row.left_at is None:
                row.left_at = now
                result.left += 1


def load_listings(db: Any) -> tuple[dict[str, set[str]], datetime | None]:
    """Options currently on sale per market, and when that was last observed.

    The timestamp is half the answer. Every number derived from this table is a
    snapshot, and one presented without its `as of` is what let a figure 18
    days old read as today's.

    An empty mapping for a market means the pull has never run or never
    succeeded — not that nothing is on sale. Callers must tell those apart,
    which is why this returns the observation time rather than leaving them to
    infer freshness from the rows.
    """
    listed: dict[str, set[str]] = {code: set() for code in MARKET_CODES}
    observed: datetime | None = None
    for market, model_color, last_seen in db.execute(
        select(
            StorefrontListing.market,
            StorefrontListing.model_color,
            StorefrontListing.last_seen_at,
        ).where(StorefrontListing.left_at.is_(None))
    ):
        listed.setdefault(market, set()).add(model_color)
        if observed is None or last_seen > observed:
            observed = last_seen
    return listed, observed
