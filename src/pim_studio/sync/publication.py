"""Has the shop caught up? Asked of the shop, not of Omnitron.

The push already answers a different question well. It writes a value, reads
it straight back under the market's locale, checks that nothing else moved,
and promotes the row to `live`. That promotion is correct in this codebase's
vocabulary — `live` means "Omnitron's current value" everywhere, and stock,
price and image rows are stored that way too. What it does not mean is that a
customer can see it.

Measured on the first real push (2026-09-08, vault
`Findings/Two title surfaces`): Omnitron held the new Polish name at 13:47:03,
and example.com served it at 15:52. For two hours the panel said `live` about
text nobody could read, which is the same shape of error as reporting a
description as filled because our mirror holds one.

## Why the shop is asked, and not the channel

Akinon publishes through per-channel `IntegrationAction` rows, and their
`version_date` is a precise, one-request answer. It was the obvious source and
it is the wrong one, for a reason worth recording: **a market is not served by
the channel you would expect.** Measured on product 357545:

    channel  68 (SHOP PL)       no action at all
    channel 101 (SHOP CZE)      no action at all
    channel 102 (SHOP HUN)      no action at all
    channel 167 (Northwind SK)    exported 27 seconds after the write
    channel 596 (Northwind Global) exported 2h05m after the write

Poland's own channel holds nothing; Poland is served by Northwind Global, whose
catalogue 601 `scope.py` already records as the shared one. So attributing a
market to a channel means a mapping that is not derivable from the channel
names, differs from `scope.py`'s `channel_pk`, and would silently rot the day
Akinon moves a market again — exactly the failure `scope.py`'s own comments
describe for catalogue 4.

The storefront has none of that ambiguity. `pz-locale` selects the market, the
response is what the customer is served, and being wrong about it is
impossible in a way that being wrong about a channel is not. It costs a crawl
instead of a request, which is why this is a background stage and not a check
inside the push.

## A global key belongs to every shop, not to none of them

`PL_sezon` is written under market `*` — it is one value the whole tenant
reads, not a per-market copy — and the first version of this stage routed an
item by `STOREFRONTS[item.market]`, which for `*` is nothing. So the entire
global bucket was skipped on every pass with "no storefront", and on
2026-09-23 it stood at 0 confirmed out of 117 written, lifetime. The cell read
the same as a failed push.

The key is served: `PL_sezon` is in the listing attributes of all four shops,
identically, because there is only one copy of it. So a global item is asked
of whichever shops are being crawled anyway, and confirmed on the *first*
positive match — one shop serving it is the value being served. Markets with
nothing of their own are crawled only while global items are still
unmatched, and not at all once they are, which keeps the common case (a push
of per-market text) costing exactly what it did before.

## What it will not do

It never marks an item unpublished. Absence is "not seen yet" and the shop is
asked again next pass, because a value can be absent for reasons that have
nothing to do with our write: the option can be out of the listing entirely, a
size can be sold out, the crawl can be short. Only a positive match sets
`published_at`, and once set it stays.

That is right for the stamp and wrong as the only thing anybody is told. An
option out of the listing is not late, it is unreachable — `5H1299.64I` left
the HU listing on 2026-09-10 and sixteen values written to it on 2026-09-23
can never match, however many passes run. The stage cannot tell the two apart
from a single pass and does not try; `admin/imports_api.py` reads
`storefront_listings` beside these rows so the panel can say which of the two
a zero is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import integrations
from ..logging import log
from ..models import MARKET_AGNOSTIC, PushItem, utcnow
from .storefront import (
    _LISTING_FLOOR,
    _MAX_PAGES,
    _PAGE_SIZE,
    STOREFRONTS,
    Storefront,
    StorefrontReadFailed,
    _option_key,
    listing_floor,
)
from .targets import write_targets

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _same(a: str | None, b: str | None) -> bool:
    """Whitespace-insensitive equality, and nothing cleverer.

    The shop re-wraps and collapses whitespace, so an exact match would report
    a correctly published value as missing. Case is *not* folded: `urun_adi_pl`
    is rendered verbatim as the page title, and a value that differs only in
    case is a value the shop is not serving as we sent it — the very thing
    somebody would want to notice.
    """
    return " ".join((a or "").split()) == " ".join((b or "").split())


def read_served(
    shop: Storefront, keys: set[str], client: httpx.Client | None = None
) -> dict[str, dict[str, str]]:
    """`{model_color: {omnitron_key: value}}` for one market, from its shop.

    Same endpoint, paging and guards as `storefront.read_listing`, and for the
    same reasons: the row count is checked against the `total_count` the first
    response committed to, because a crawl that stops early is indistinguishable
    from the range having shrunk. Here a short read would silently mean "the
    shop is not serving your value", so it raises rather than returns.
    """
    owned = client is None
    http = client or integrations.http_client(timeout=_TIMEOUT, follow_redirects=True)
    try:
        served: dict[str, dict[str, str]] = {}
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
                    "User-Agent": "pim-studio/publication",
                    "Cookie": shop.cookie,
                },
            )
            if response.status_code != httpx.codes.OK:
                raise StorefrontReadFailed(
                    f"{shop.market}: page {page} returned HTTP {response.status_code}"
                )
            body = response.json()
            if expected is None:
                pagination = body.get("pagination") or {}
                expected = int(pagination.get("num_pages") or 0)
                promised = int(pagination.get("total_count") or 0)
                if expected > _MAX_PAGES:
                    raise StorefrontReadFailed(
                        f"{shop.market}: claims {expected} pages, over the "
                        f"ceiling of {_MAX_PAGES}"
                    )
            for row in body.get("products") or []:
                rows += 1
                mc = _option_key(row)
                if not mc:
                    continue
                attributes: dict[str, Any] = row.get("attributes") or {}
                present = {
                    k: str(attributes[k])
                    for k in keys
                    if attributes.get(k) not in (None, "")
                }
                if present:
                    # Sizes of one option repeat the option's values; last wins
                    # and they agree, because these are option-grain fields.
                    served.setdefault(mc, {}).update(present)
            pages += 1
            if page >= (expected or 0):
                break
            page += 1

        promised = promised or 0
        if promised and rows < promised * 0.98:
            raise StorefrontReadFailed(
                f"{shop.market}: returned {rows:,} of the {promised:,} rows it "
                f"said it had, over {pages} pages — too short to read absence from"
            )
        floor = listing_floor(_LISTING_FLOOR)
        if len(served) < floor:
            raise StorefrontReadFailed(
                f"{shop.market}: {len(served)} options carry any of these keys, "
                f"below the floor of {floor:,} — a wrong or missing "
                "pz-locale cookie returns another channel's much smaller range"
            )
        return served
    finally:
        if owned:
            http.close()


def confirm_published(
    db: Session, *, client: httpx.Client | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Ask each market's shop which of our written values it is serving.

    Only markets with something outstanding are crawled, so this costs nothing
    on a day nobody pushed. A market whose read fails is skipped and reported;
    its items stay unpublished and are asked again next pass, which is the
    behaviour that makes a failed crawl harmless.
    """
    stamp = now or utcnow()
    targets = write_targets(db)
    keys_by_market: dict[str, set[str]] = {}
    for (_attribute_id, market), target in targets.items():
        keys_by_market.setdefault(market, set()).add(target.key)

    outstanding = db.scalars(
        select(PushItem).where(
            PushItem.published_at.is_(None),
            PushItem.status.in_(("written", "confirmed")),
        )
    ).all()
    by_market: dict[str, list[PushItem]] = {}
    #: Written under `*`: one copy the whole tenant reads, so no one shop owns
    #: it and every shop serves it. Carried separately and offered to each
    #: market's read until one of them matches.
    pending_global: list[PushItem] = []
    for item in outstanding:
        if item.market == MARKET_AGNOSTIC:
            pending_global.append(item)
        else:
            by_market.setdefault(item.market, []).append(item)

    global_keys = keys_by_market.get(MARKET_AGNOSTIC, set())
    if not global_keys:
        pending_global = []

    stats: dict[str, Any] = {
        "outstanding": len(outstanding),
        "published": 0,
        "markets_read": 0,
        "global_outstanding": len(pending_global),
        "skipped_markets": {},
    }
    if not outstanding:
        return stats

    # Markets with something of their own first, so a global value is usually
    # settled on a crawl that was happening anyway; the rest only while global
    # items are still unmatched, and the loop drops out once they are not.
    order = sorted(by_market)
    order += [m for m in sorted(STOREFRONTS) if m not in by_market]

    for market in order:
        items = by_market.get(market, [])
        if not items and not pending_global:
            continue
        shop = STOREFRONTS.get(market)
        keys = keys_by_market.get(market, set())
        if pending_global:
            keys = keys | global_keys
        if shop is None or not keys:
            stats["skipped_markets"][market] = "no storefront or no write targets"
            continue
        try:
            served = read_served(shop, keys, client=client)
        except (StorefrontReadFailed, httpx.HTTPError) as exc:
            # Not an error for the run: the items simply stay unpublished and
            # are asked again. Saying so is what stops a week of silent
            # non-confirmation looking like a shop that never updates.
            stats["skipped_markets"][market] = str(exc)[:200]
            log.info("publication_market_skipped", market=market, reason=str(exc)[:200])
            continue

        stats["markets_read"] += 1
        hit = 0
        for item in items:
            if _same(served.get(item.model_color, {}).get(item.key), item.new_value):
                item.published_at = stamp
                hit += 1
        still_pending: list[PushItem] = []
        for item in pending_global:
            if _same(served.get(item.model_color, {}).get(item.key), item.new_value):
                item.published_at = stamp
                hit += 1
            else:
                still_pending.append(item)
        global_hit = len(pending_global) - len(still_pending)
        pending_global = still_pending
        stats["published"] += hit
        log.info(
            "publication_market_checked",
            market=market, outstanding=len(items), published=hit,
            global_published=global_hit, global_left=len(pending_global),
            options_served=len(served),
        )

    db.commit()
    log.info("publication_finished", **{k: v for k, v in stats.items() if k != "skipped_markets"})
    return stats
