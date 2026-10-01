"""Market scope — we pull from four channels and ignore the rest.

A hard line, decided 2026-08-14, and enforced here rather than remembered.
The tenant is Northwind's global Omnitron: 26 channels, 27 catalogues, 288,990
products. Ours are four. Everything else — TR, UA, Global, KZ, the marketplace
integrations — is out of scope, and a read that would cross that boundary
raises `MarketScopeViolation` instead of quietly returning someone else's data.

This is the same shape as the read-only rule in `omnitron.client`: the point is
not that a careful person would avoid the mistake, it is that the code will not
let them make it. A sync that accidentally widened its scope would not fail —
it would succeed, slowly, and fill our tables with products we do not sell.

## Where the line falls, precisely

The boundary is **market-scoped data**, and it is worth being exact because a
broadly-worded rule that gets quietly violated is worse than a narrow one.

Restricted — these resources carry a channel, catalogue, price list, stock list
or category tree, and must name one of ours: products, catalogue membership,
prices, stock, category assignment, images.

Not restricted — the attribute registry (`attributes`, `attribute_value`,
`attribute_set`, `attribute_config`) is tenant-global. An attribute definition
has no channel dimension; there is no PL copy of `integration_yaka` to prefer
over a TR one. Syncing the registry is not a scope breach, it is the thing the
module was asked for. What the registry *does* need is honesty about coverage:
some of the 149 attributes are used only by channels we ignore, and the
reconciliation report should say so rather than present them as gaps in ours.

## The numbers, read from the live instance on 2026-08-14

Resolved from each catalogue object, never matched on name. The pk space
overlaps across resource types — `SK Price List` is pk 167 and `hu_stock_list`
is also pk 167 — so name matching would have produced a plausible, wrong map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..logging import log

if TYPE_CHECKING:  # pragma: no cover
    from ..omnitron.client import OmnitronClient


class MarketScopeViolation(RuntimeError):
    """Raised when a read would reach outside our four markets.

    If you are reading this in a traceback, the fix is not to widen the
    allowlist. It is to scope the query — or, if the data genuinely is needed,
    to add an entry to the vault's `05 - Decisions Log` first, because this
    boundary was set deliberately.
    """


@dataclass(frozen=True)
class Market:
    """One of our four markets and every Omnitron id that belongs to it."""

    code: str
    name: str
    channel_pk: int
    catalog_pk: int
    price_list_pk: int
    stock_list_pk: int
    category_tree_pk: int
    #: Extra price lists the catalogue declares. SK carries KZ_Price_List (200),
    #: which we do NOT follow — see `EXTRA_PRICE_LISTS_IGNORED` below.
    extra_price_list_pks: tuple[int, ...] = ()
    #: Catalogues this market shares with others. Membership found in one of
    #: these counts for this market as much as membership in `catalog_pk`.
    #:
    #: All four carry 601 (`Northwind Global`, channel 596, created 2026-01-13)
    #: because their own catalogues have stopped being the whole of their range.
    #: Measured 2026-09-07 against the PL storefront: of 45 sampled options that
    #: example.com sells and the panel could not account for, **44 are in 601 and
    #: 1 is in catalogue 4** — and 26 of them sit in the CZ, HU and SK
    #: catalogues while being absent from PL's. Catalogue 4 is not stale in the
    #: sense of holding nothing; it still identifies PL-exclusive range
    #: correctly. It is being left behind.
    #:
    #: HU and SK followed on 2026-09-16 (confirmed by Emir, and measured the same
    #: day: 40 of 40 sampled options on each shop are in 601, and 4 HU and 3 SK
    #: of them are in 601 *only*). Their channels still read `is_active: true`,
    #: which is recorded below and, like PL's flag, decides nothing.
    #:
    #: This is deliberately additive rather than a repoint. A market keeps its
    #: own catalogue as the one whose price list, stock list and category tree
    #: define it — `verify_against_live` still checks those against
    #: `catalog_pk` — and gains a second place to find members.
    shared_catalog_pks: tuple[int, ...] = ()
    #: `SHOP PL` is `is_active: false` on the channel object. That is recorded,
    #: not acted on: the control table shows 12,867 live PL SKUs, so the flag is
    #: not evidence PL is switched off. It does mean a query scoped to channel
    #: 68 may return nothing, and that would look like our bug rather than a
    #: fact about the tenant.
    channel_is_active: bool = True


#: The catalogue all four markets were migrated onto — PL and CZ first, HU and
#: SK by 2026-09-16. One catalogue serving several markets, which is why it is
#: `shared_` rather than a new `catalog_pk`: it carries every market's price and
#: stock list as extras (`extra_price_lists [200, 2, 134, 135, 167, 35]`), so
#: membership in it says "somebody sells this", not "Poland sells this". What
#: narrows it to a market is the price list, which liveness rule 1 already
#: applies.
#:
#: Until 2026-09-16 HU and SK were deliberately left off it: their channels were
#: active and their own catalogues matched their storefronts to 99.7%. The
#: channels are still active; the catalogues no longer hold everything the shops
#: sell.
NORTHWIND_GLOBAL_CATALOG = 601

MARKETS: dict[str, Market] = {
    "PL": Market("PL", "Poland Northwind", 68, 4, 2, 2, 35, channel_is_active=False,
                 shared_catalog_pks=(NORTHWIND_GLOBAL_CATALOG,)),
    "CZ": Market("CZ", "Czech Northwind", 101, 40, 134, 168, 101,
                 shared_catalog_pks=(NORTHWIND_GLOBAL_CATALOG,)),
    "HU": Market("HU", "Hungary Northwind", 102, 41, 135, 167, 134,
                 shared_catalog_pks=(NORTHWIND_GLOBAL_CATALOG,)),
    "SK": Market("SK", "Northwind SK", 167, 172, 167, 169, 266, extra_price_list_pks=(200,),
                 shared_catalog_pks=(NORTHWIND_GLOBAL_CATALOG,)),
}

#: Channels that exist, are active, and are deliberately none of our business:
#: marketplaces and the Turkish home market, each with its own catalogue.
#:
#: An allowlist rather than a blocklist, because the failure it guards is a
#: channel *appearing*. `Northwind Global` (596 → catalogue 601) was created in
#: January 2026 and carried Poland and Czechia away from catalogues 4 and 40
#: (and later Hungary and Slovakia from 41 and 172) without anything here
#: noticing for eight months — `verify_against_live` only
#: ever re-read the four catalogues we already named, so a tenant growing a
#: fifth was invisible to it by construction. Anything active and not listed
#: here is now reported as drift and has to be classified by a person.
KNOWN_FOREIGN_CHANNELS: dict[int, str] = {
    # Read from the live tenant 2026-09-07 — **channel** pks, not catalogue
    # pks. The first draft of this map used the catalogue id of each channel
    # and every entry was therefore wrong; the guard reported fourteen
    # unmodelled channels instead of one, which is how it was caught. Trendyol
    # is channel 3 pointing at catalogue 5, and the two numbers are never
    # interchangeable on this tenant.
    1: "SHOP TR — the Turkish home market",
    3: "Trendyol",
    134: "Amazon",
    332: "Hepsiburada",
    365: "Sanal Çadır",
    398: "Fashion Federation",
    431: "Northwind Mağazalar — Turkish retail stores",
    464: "Superstep",
    498: "Beymen",
    530: "Intersport",
    563: "United Brands",
    597: "Amazon (second integration)",
    598: "Fashfed",
    599: "Trendyol (second integration)",
    # Inactive today and so never reported anyway, but classified here so a
    # reactivation is a quiet non-event rather than an alarm: Ukraine is out of
    # scope by decision, and Allegro is a marketplace rather than our shop.
    103: "SHOP UKR — Ukraine, out of scope by decision",
    299: "Allegro PL — marketplace, not our storefront",
}

#: SK's catalogue declares KZ_Price_List (200) as an extra. We do not read it.
#: Kazakhstan is not one of our markets, and the fact that a Slovak catalogue
#: points at a Kazakh price list is a tenant configuration oddity to raise with
#: whoever owns it, not a reason to widen our scope. Recorded so nobody
#: rediscovers it and assumes it was an oversight.
EXTRA_PRICE_LISTS_IGNORED = {200: "KZ_Price_List, declared by SK catalogue 172"}

ALLOWED_CHANNELS = frozenset(m.channel_pk for m in MARKETS.values())
ALLOWED_CATALOGS = frozenset(
    pk for m in MARKETS.values() for pk in (m.catalog_pk, *m.shared_catalog_pks)
)
#: Catalogues more than one market reads. `market_for_catalog` cannot answer
#: for these and says so rather than picking the first match.
SHARED_CATALOGS = frozenset(
    pk for m in MARKETS.values() for pk in m.shared_catalog_pks
)
ALLOWED_PRICE_LISTS = frozenset(m.price_list_pk for m in MARKETS.values())
ALLOWED_STOCK_LISTS = frozenset(m.stock_list_pk for m in MARKETS.values())
ALLOWED_CATEGORY_TREES = frozenset(m.category_tree_pk for m in MARKETS.values())

#: Every market code, in the order reports should present them.
MARKET_CODES = tuple(MARKETS)


# ── lookups ────────────────────────────────────────────────────────────────


def market_for_catalog(pk: int) -> Market:
    """The market a catalogue belongs to — only where that question has one
    answer.

    A shared catalogue has several, and returning the first would be a silent
    wrong answer of exactly the kind this module exists to prevent. Callers
    that only need "is this ours" should use `require_catalog`, which does not
    ask a question the data cannot answer.
    """
    for m in MARKETS.values():
        if m.catalog_pk == pk:
            return m
    if pk in SHARED_CATALOGS:
        raise MarketScopeViolation(
            f"catalog {pk} is shared by "
            f"{sorted(m.code for m in markets_for_catalog(pk))} and belongs to "
            f"no single market. Use markets_for_catalog, or require_catalog if "
            f"you only need to know it is ours."
        )
    raise MarketScopeViolation(
        f"catalog {pk} is not one of ours ({sorted(ALLOWED_CATALOGS)})."
    )


def markets_for_catalog(pk: int) -> tuple[Market, ...]:
    """Every market that reads this catalogue. Empty when it is not ours."""
    return tuple(
        m for m in MARKETS.values()
        if pk == m.catalog_pk or pk in m.shared_catalog_pks
    )


def market_for_channel(pk: int) -> Market:
    for m in MARKETS.values():
        if m.channel_pk == pk:
            return m
    raise MarketScopeViolation(
        f"channel {pk} is not one of ours ({sorted(ALLOWED_CHANNELS)})."
    )


def market_for_price_list(pk: int) -> Market:
    for m in MARKETS.values():
        if m.price_list_pk == pk:
            return m
    raise MarketScopeViolation(
        f"price list {pk} is not one of ours ({sorted(ALLOWED_PRICE_LISTS)})."
    )


def market_for_stock_list(pk: int) -> Market:
    for m in MARKETS.values():
        if m.stock_list_pk == pk:
            return m
    raise MarketScopeViolation(
        f"stock list {pk} is not one of ours ({sorted(ALLOWED_STOCK_LISTS)})."
    )


# ── guards ─────────────────────────────────────────────────────────────────


def require_catalog(pk: int) -> int:
    """Assert a catalogue is ours, and return it so guards compose inline.

    Membership, not ownership: a shared catalogue passes. The guard's job is to
    stop a read reaching outside our markets, and 601 is inside them.
    """
    if pk not in ALLOWED_CATALOGS:
        raise MarketScopeViolation(
            f"catalog {pk} is not one of ours ({sorted(ALLOWED_CATALOGS)})."
        )
    return pk


def require_channel(pk: int) -> int:
    market_for_channel(pk)
    return pk


def require_price_list(pk: int) -> int:
    market_for_price_list(pk)
    return pk


def require_stock_list(pk: int) -> int:
    market_for_stock_list(pk)
    return pk


def require_scoped(params: dict[str, object], *, scope_keys: tuple[str, ...]) -> dict[str, object]:
    """Refuse a query that does not restrict itself to one of our markets.

    The failure this exists to stop is not a wrong id — it is a *missing*
    filter. Omnitron silently ignores query parameters it does not recognise
    and returns the full unfiltered set with HTTP 200 (verified 2026-08-14:
    nine candidate product filters all returned all 288,990 rows). So a
    scoping filter that is merely misspelled looks exactly like a successful
    query, and the resulting sync would pull the whole tenant while reporting
    success.

    Callers pass the parameter names that carry scope for the endpoint being
    read; at least one must be present and hold an id we allow.
    """
    present = [k for k in scope_keys if k in params]
    if not present:
        raise MarketScopeViolation(
            f"unscoped read: none of {scope_keys} in {sorted(params)}. Omnitron "
            "ignores unknown filters and returns the whole tenant, so an "
            "unscoped query is not a wide query — it is a silent one."
        )
    for key in present:
        value = params[key]
        for raw in str(value).split(","):
            raw = raw.strip()
            if not raw:
                continue
            pk = int(raw)
            if key.startswith("catalog"):
                require_catalog(pk)
            elif key.startswith("channel"):
                require_channel(pk)
            elif "price_list" in key:
                require_price_list(pk)
            elif "stock_list" in key:
                require_stock_list(pk)
    return params


# ── drift guard ────────────────────────────────────────────────────────────


def verify_against_live(api: OmnitronClient) -> list[str]:
    """Re-read the four catalogues and confirm the ids above still hold.

    The constants were read on one day. If someone repoints SK's catalogue at a
    different price list, nothing here would notice — the sync would keep
    reading list 167 and keep succeeding, and the numbers would quietly become
    another market's. This is the check that turns that into a loud failure.

    Returns a list of human-readable drift descriptions; empty means clean.
    Callers decide whether drift is fatal — it always is for a scheduled run,
    and is worth a warning rather than a stop when someone is investigating.
    """
    drift: list[str] = []
    for market in MARKETS.values():
        body = api.get(f"/api/v1/catalogs/{market.catalog_pk}/")
        for field, expected in (
            ("price_list", market.price_list_pk),
            ("stock_list", market.stock_list_pk),
            ("category_tree", market.category_tree_pk),
        ):
            actual = body.get(field)
            if actual != expected:
                drift.append(
                    f"{market.code}: catalogue {market.catalog_pk}.{field} is "
                    f"{actual}, expected {expected}"
                )
        extras = tuple(body.get("extra_price_lists") or ())
        if extras != market.extra_price_list_pks:
            drift.append(
                f"{market.code}: catalogue {market.catalog_pk}.extra_price_lists "
                f"is {list(extras)}, expected {list(market.extra_price_list_pks)}"
            )
    # A shared catalogue has no price list of its own that we depend on, so
    # the checks above do not apply to it. What we depend on is the reason it
    # serves this market at all: that it carries the market's price list among
    # its extras. If Northwind drops PL's list from 601, PL stops being sold
    # from it and our membership becomes fiction — silently, because every id
    # still resolves.
    for market in MARKETS.values():
        for shared in market.shared_catalog_pks:
            body = api.get(f"/api/v1/catalogs/{shared}/")
            extras = set(body.get("extra_price_lists") or ())
            if market.price_list_pk not in extras:
                drift.append(
                    f"{market.code}: shared catalogue {shared} no longer "
                    f"carries price list {market.price_list_pk} "
                    f"(extras: {sorted(extras)}), so it no longer serves "
                    f"{market.code}"
                )

    drift.extend(unmodelled_channels(api))

    if drift:
        log.error("market_scope_drift", drift=drift)
    else:
        log.info("market_scope_verified", markets=list(MARKET_CODES))
    return drift


def unmodelled_channels(api: OmnitronClient) -> list[str]:
    """Active channels pointing at catalogues we do not read.

    The check the eight-month miss argues for. Everything else in this module
    verifies that the ids we hold still mean what they meant; none of it can
    see a channel we never held an id for. `Northwind Global` was exactly that —
    active, pointing at catalogue 601, carrying most of what example.com sells,
    and invisible to a guard that only re-read catalogues 4, 40, 41 and 172.

    Inactive channels are ignored on purpose. `SHOP PL` and `SHOP CZE` are
    themselves inactive today and are still the catalogues we read, so an
    inactive channel is not evidence of anything by itself — which is precisely
    the inference that went wrong in the other direction on 2026-08-14, when the
    PL flag was dismissed because a control table reading the same stale
    catalogue disagreed with it. What is reported here is the positive fact: a
    live channel we have no model for.
    """
    findings: list[str] = []
    for record in api.iter_records("/api/v1/channels/", params={}, page_size=100):
        pk = record.get("pk")
        if pk is None or not record.get("is_active"):
            continue
        pk = int(pk)
        if pk in ALLOWED_CHANNELS or pk in KNOWN_FOREIGN_CHANNELS:
            continue
        findings.append(
            f"channel {pk} {record.get('name')!r} is active and points at "
            f"catalogue {record.get('catalog')}, which nothing in scope.py "
            f"reads. Classify it: add it to a market, or to "
            f"KNOWN_FOREIGN_CHANNELS with the reason."
        )
    return findings
