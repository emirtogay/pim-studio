"""Coverage, computed from the database.

`rules/coverage` decides what a verdict means. This module is the part that
knows where the facts live: it reads the mirror once and hands back a
`Catalogue` — every ModelColor's attribute presence, its issue flags, and the
dimensions worth slicing by.

Three callers share it, which is the reason it exists as its own module rather
than as a query inside the CLI report:

    reports/__main__          the terminal report
    admin/coverage_api        the workbook and the upload endpoints
    scripts/build_demo_data…  the extract the dashboard reads

Two of those already had their own partial version of this join. A third copy
would have guaranteed the three disagreed, which for coverage numbers is worse
than not having them.

## What "sourced" means here, and what it does not

A requirement can only be *missing* if we hold a source that would carry it.
This module answers that at attribute × market grain: a column counts as read
when at least one product in the mirror holds a value for it. That is a real
signal — it is the same test `build_demo_data_from_db` already applies to
prices, stock and images — but it is a claim about the *column*, not about any
individual product. A product absent from the file that filled the column is
indistinguishable here from one the file covered and left empty.

Doing better needs per-row provenance the importer does not record yet. Saying
so is the point: the approximation errs toward reporting a gap, which is
visible and dismissible, rather than toward hiding one.

## Catalogue membership is a filter, not a verdict

A product in no Hungarian catalogue has no Hungarian price, and calling that a
gap would put every PL-only product into the Hungarian work queue. It is also
not `na` — the product may well belong there and nobody has added it. So it is
neither: membership is an issue flag of its own, and narrowing to a market's
catalogue is something the caller chooses and can see, via `Scope`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import (
    Attribute,
    AttributeValue,
    BanComment,
    CatalogueMembership,
    Product,
)
from ..rules.ban import BanVerdict, classify_comment
from ..rules.coverage import (
    ISSUES,
    ISSUES_BY_KEY,
    MARKETS,
    OWNER_BY_CHANNEL,
    PRESETS,
    PRESETS_BY_KEY,
    SHARED,
    STOCK_CAVEAT,
    AttributeMeta,
    CoverageSummary,
    Owner,
    ProductFacts,
    Profile,
    Requirement,
    RequirementTally,
    Standing,
    bucket,
    evaluate_product,
    summarise,
    tally_requirements,
)
from ..rules.liveness import STOCK_THRESHOLD


@dataclass(frozen=True, slots=True)
class ProductRow:
    """One ModelColor as the detail sheet and the dashboard card show it.

    Deliberately separate from `ProductFacts`: that carries presence for the
    rule engine, this carries the identifying and slicing columns. Merging them
    would put a product name into every requirement evaluation.
    """

    model_color: str
    name: str
    season: str
    sub_category: str
    gender: str
    sku_count: int
    #: Market catalogues the option belongs to.
    markets: tuple[str, ...]
    #: Issue keys from `rules.coverage.ISSUES` that hold for this option.
    flags: tuple[str, ...]
    comment: str = ""
    image: str = ""
    #: Best stock any one SKU carries, in any market. Provisional — see
    #: `STOCK_CAVEAT`. Kept because "in stock and no image" is the cut worth
    #: fixing first, and that needs a number.
    best_stock: int = 0

    @property
    def bucket(self) -> str:
        return bucket(self.flags)


@dataclass
class Catalogue:
    """One read of the mirror, in the shape every coverage caller wants."""

    registry: dict[str, AttributeMeta]
    facts: dict[str, ProductFacts]
    rows: dict[str, ProductRow]
    #: attribute key → markets (or `SHARED`) a value has ever been seen for.
    sourced: dict[str, tuple[str, ...]]
    generated_at: datetime
    #: Attribute × market pairs no value has ever been seen for, so a report
    #: can say what it could not judge instead of scoring it zero.
    unread: list[tuple[str, str]] = field(default_factory=list)

    def model_colors(self) -> list[str]:
        return sorted(self.rows)


def _meta_from(attribute: Attribute) -> AttributeMeta:
    return AttributeMeta(
        key=attribute.key,
        label=attribute.label,
        owner=OWNER_BY_CHANNEL.get(attribute.write_channel, Owner.SYSTEM),
        per_market=bool(attribute.per_market),
        applies_when_subcategory=attribute.applies_when_subcategory or "",
        group=attribute.display_group,
    )


def load_catalogue(db: Session) -> Catalogue:
    """Read everything coverage needs, in four passes over the mirror.

    Ordered so nothing is scanned twice: the registry and the ban table are
    small, products establish the ModelColor spine, and the value scan — the
    only expensive one — runs once and fills presence, prices, stock and the
    handful of values the detail rows display.
    """
    registry = {a.key: _meta_from(a) for a in db.scalars(select(Attribute))}
    attr_key = {row[0]: row[1] for row in db.execute(select(Attribute.id, Attribute.key))}
    ban_table = {
        comment.strip().lower(): bans
        for comment, bans in db.execute(select(BanComment.comment, BanComment.bans))
    }

    sku_to_mc: dict[str, str] = {}
    sku_count: dict[str, int] = defaultdict(int)
    for sku, mc in db.execute(
        select(Product.sku, Product.model_color).where(Product.is_size())
    ):
        sku_to_mc[sku] = mc
        sku_count[mc] += 1

    mc_markets: dict[str, set[str]] = defaultdict(set)
    for mc, market in db.execute(
        select(Product.model_color, CatalogueMembership.market)
        .join(CatalogueMembership, CatalogueMembership.product_id == Product.id)
        .where(CatalogueMembership.left_at.is_(None), Product.is_size())
    ):
        mc_markets[mc].add(market)

    filled: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    seen: dict[str, set[str]] = defaultdict(set)
    display: dict[str, dict[str, str]] = defaultdict(dict)
    priced: dict[str, set[str]] = defaultdict(set)
    stocked: dict[str, set[str]] = defaultdict(set)
    best_stock: dict[str, int] = defaultdict(int)

    for entity_kind, entity_key, attribute_id, market, value in db.execute(
        select(
            AttributeValue.entity_kind,
            AttributeValue.entity_key,
            AttributeValue.attribute_id,
            AttributeValue.market,
            AttributeValue.value,
        ).where(AttributeValue.state_lane == "current")
    ):
        key = attr_key.get(attribute_id)
        if key is None:
            continue
        # A row that exists with no value is still evidence the column was
        # read. That is exactly the distinction this module turns on, so the
        # `seen` update happens before the empty check, never after it.
        seen[key].add(market)
        if value is None or not str(value).strip():
            continue
        mc = entity_key if entity_kind == "model_color" else sku_to_mc.get(entity_key)
        if mc is None:
            continue

        filled[mc][key].add(market)
        display[mc].setdefault(key, str(value))

        if key == "price":
            priced[mc].add(market)
        elif key == "stock":
            try:
                qty = int(float(value))
            except ValueError:
                continue
            best_stock[mc] = max(best_stock[mc], qty)
            # Imported from the liveness rule rather than restated. A second
            # copy of the threshold is how the panel and its own rule module
            # disagreed for four months.
            if qty > STOCK_THRESHOLD:
                stocked[mc].add(market)

    sourced = {key: tuple(sorted(markets)) for key, markets in seen.items()}
    unread = [
        (meta.key, market)
        for meta in registry.values()
        for market in (MARKETS if meta.per_market else (SHARED,))
        if market not in sourced.get(meta.key, ())
    ]

    prices_read = bool(seen.get("price"))
    stock_read = bool(seen.get("stock"))
    images_read = any(filled[mc].get("image_0") for mc in filled)

    facts: dict[str, ProductFacts] = {}
    rows: dict[str, ProductRow] = {}
    for mc in sku_count:
        values = display.get(mc, {})
        sub_category = values.get("sub_category", "")
        comment = values.get("pl_comment", "")
        verdict = classify_comment(comment or None, ban_table)

        flags: list[str] = []
        if verdict is BanVerdict.BANNED:
            flags.append("removed")
        elif verdict is BanVerdict.UNKNOWN:
            flags.append("unclassified_comment")
        # Each flag is guarded by whether its source was read at all. Raising
        # "no price" across a catalogue whose price sync has never finished is
        # the failure mode this codebase has hit four times.
        if prices_read and not priced.get(mc):
            flags.append("no_price")
        if images_read and not values.get("image_0"):
            flags.append("no_image")
        if stock_read and not stocked.get(mc):
            flags.append("no_stock")
        if not mc_markets.get(mc):
            flags.append("not_in_catalogue")

        facts[mc] = ProductFacts(
            model_color=mc,
            sub_category=sub_category or None,
            filled={k: frozenset(v) for k, v in filled.get(mc, {}).items()},
            sourced=sourced,
        )
        rows[mc] = ProductRow(
            model_color=mc,
            name=values.get("product_name", "") or values.get("storefront_name", ""),
            season=values.get("pl_sezon", ""),
            sub_category=sub_category,
            gender=values.get("gender", ""),
            sku_count=sku_count[mc],
            markets=tuple(sorted(mc_markets.get(mc, ()))),
            flags=tuple(f for f in (i.key for i in ISSUES) if f in set(flags)),
            comment=comment,
            image=values.get("image_0", ""),
            best_stock=best_stock.get(mc, 0),
        )

    return Catalogue(
        registry=registry,
        facts=facts,
        rows=rows,
        sourced=sourced,
        generated_at=datetime.now(UTC),
        unread=sorted(unread),
    )


# ── Scoping ────────────────────────────────────────────────────────────────

#: `Scope.priced` and `Scope.stocked` take a market code or this — "present at
#: all, anywhere". Spelled rather than left as the empty string because empty
#: already means "do not filter", and the two are opposite questions: `""`
#: returns the banned and unpriced along with everything else, `"any"` is the
#: filter that removes them.
PRESENCE_ANY = "any"


@dataclass
class Scope:
    """Which options an analysis is about, and how that was decided.

    Carried into the report rather than applied and forgotten: a number whose
    population is undocumented cannot be checked, and every coverage figure
    this team has had to re-derive was re-derived because nobody could say
    which products it counted.
    """

    #: Explicit ModelColors, from an uploaded list. Empty means the whole
    #: catalogue.
    model_colors: tuple[str, ...] = ()
    season: tuple[str, ...] = ()
    sub_category: tuple[str, ...] = ()
    gender: tuple[str, ...] = ()
    #: Restrict to options in this market's catalogue.
    in_catalogue: str = ""
    #: Price presence. `""` does not filter; `PRESENCE_ANY` keeps options
    #: priced in at least one market; a market code keeps those priced in
    #: that market.
    #:
    #: Per-market because a price *is* per-market — `filled["price"]` has
    #: carried the market set since the first extract, it was simply never
    #: reachable from a filter. Stock is not, deliberately: see `stocked`.
    #:
    #: Only the positive direction. "Priced nowhere" is `flags=("no_price",)`
    #: and is already spelled that way on the rail; offering it twice would
    #: be two controls that must agree forever and eventually will not.
    priced: str = ""
    #: Stock presence. `""` does not filter; `PRESENCE_ANY` keeps options with
    #: stock above `STOCK_THRESHOLD`.
    #:
    #: No market code, and that is a statement about the business rather than
    #: a shortcut: there is one pool of stock behind all four markets, so
    #: "stocked in PL" is not a different question from "stocked" and a
    #: control offering it would invite an answer that does not exist.
    #: Provisional in the same way `best_stock` is — see `STOCK_CAVEAT`.
    stocked: str = ""
    #: Issue keys from `rules.coverage.ISSUES`; an option matching any of them
    #: is kept. OR within the field, AND against everything else, which is the
    #: contract the rail already documents.
    #:
    #: Here rather than only in the browser because the workbook is downloaded
    #: from a filtered screen and has to be about the same population. It was
    #: not, and the page knew: `droppedFilters` in `Coverage.tsx` existed to
    #: warn that the file would be wider than the thing it was named after.
    flags: tuple[str, ...] = ()
    #: Requested ModelColors we hold nothing for. Reported, never dropped
    #: silently — an upload of 500 codes that matches 380 is itself a finding.
    not_found: tuple[str, ...] = ()

    def describe(self) -> str:
        parts = []
        if self.model_colors:
            # "codes", not "ModelColors" — some of them turned out not to be
            # one, which is the whole reason `not_found` exists.
            parts.append(f"{len(self.model_colors)} uploaded codes")
            if self.not_found:
                parts.append(f"{len(self.not_found)} not found")
        if self.season:
            parts.append("season " + ", ".join(self.season))
        if self.sub_category:
            parts.append("category " + ", ".join(self.sub_category))
        if self.gender:
            parts.append("gender " + ", ".join(self.gender))
        if self.in_catalogue:
            parts.append(f"in the {self.in_catalogue} catalogue")
        if self.priced:
            parts.append(
                "priced somewhere"
                if self.priced == PRESENCE_ANY
                else f"priced in {self.priced}"
            )
        if self.stocked:
            parts.append("in stock")
        if self.flags:
            labels = [ISSUES_BY_KEY[f].label for f in self.flags if f in ISSUES_BY_KEY]
            if labels:
                parts.append(" or ".join(labels))
        return " · ".join(parts) or "the whole catalogue"


def apply_scope(catalogue: Catalogue, scope: Scope) -> tuple[list[str], Scope]:
    """The ModelColors in scope, and the scope with `not_found` filled in."""
    if scope.model_colors:
        wanted = list(dict.fromkeys(scope.model_colors))
        found = [mc for mc in wanted if mc in catalogue.rows]
        missing = tuple(mc for mc in wanted if mc not in catalogue.rows)
        scope = Scope(**{**scope.__dict__, "not_found": missing})
        candidates = found
    else:
        candidates = catalogue.model_colors()

    def priced_ok(mc: str) -> bool:
        if not scope.priced:
            return True
        # `filled` is the same per-market presence the requirement engine
        # reads, so "priced in PL" here and "price missing in PL" on the
        # attribute matrix cannot come apart — they are one fact.
        markets = catalogue.facts[mc].filled.get("price", frozenset())
        if scope.priced == PRESENCE_ANY:
            return bool(markets)
        return scope.priced in markets

    def keep(mc: str) -> bool:
        row = catalogue.rows[mc]
        if scope.season and row.season not in scope.season:
            return False
        if scope.sub_category and row.sub_category not in scope.sub_category:
            return False
        if scope.gender and row.gender not in scope.gender:
            return False
        if scope.in_catalogue and scope.in_catalogue not in row.markets:
            return False
        if not priced_ok(mc):
            return False
        if scope.stocked and row.best_stock <= STOCK_THRESHOLD:
            return False
        return not scope.flags or bool(set(scope.flags) & set(row.flags))

    return [mc for mc in candidates if keep(mc)], scope


# ── The report ─────────────────────────────────────────────────────────────


@dataclass
class IssueTally:
    key: str
    label: str
    definition: str
    remedy: str
    count: int
    stock_backed: bool

    def share(self, population: int) -> float | None:
        return self.count / population if population else None


@dataclass
class CoverageReport:
    profile: Profile
    scope: Scope
    summary: CoverageSummary
    requirements: list[RequirementTally]
    issues: list[IssueTally]
    #: bucket key → how many options fall in exactly that combination.
    buckets: dict[str, int]
    rows: list[ProductRow]
    #: Per-product standing, for the detail sheet and the drill-down.
    standings: dict[str, Standing]
    gaps_by_product: dict[str, int]
    caveats: list[str]
    generated_at: datetime

    @property
    def population(self) -> int:
        return len(self.rows)


def build_report(
    catalogue: Catalogue,
    profile: Profile,
    scope: Scope | None = None,
) -> CoverageReport:
    """Everything the CLI prints and the workbook writes, computed once."""
    scope = scope or Scope()
    in_scope, scope = apply_scope(catalogue, scope)

    facts = [catalogue.facts[mc] for mc in in_scope]
    coverages = [evaluate_product(f, profile, catalogue.registry) for f in facts]
    summary = summarise(coverages, profile, catalogue.registry)
    requirements = tally_requirements(facts, profile, catalogue.registry)

    counts: dict[str, int] = {issue.key: 0 for issue in ISSUES}
    buckets: dict[str, int] = defaultdict(int)
    rows = []
    for mc in in_scope:
        row = catalogue.rows[mc]
        rows.append(row)
        for flag in row.flags:
            counts[flag] += 1
        buckets[row.bucket] += 1

    issues = [
        IssueTally(
            key=issue.key,
            label=issue.label,
            definition=issue.definition,
            remedy=issue.remedy,
            count=counts[issue.key],
            stock_backed=issue.stock_backed,
        )
        for issue in ISSUES
    ]

    # A conditional attribute that matched nothing at all.
    #
    # `applies_when_subcategory` is a list of sub-category names, matched
    # against whatever the mirror holds — which comes from a category
    # breadcrumb, not from the registry's vocabulary. When the two spellings
    # diverge (`Polo` against `Textile > Polo Shirts > Men`) every option
    # reads `na`, and the requirement drops out of the report entirely.
    #
    # That failure is silent and it under-reports, which is the dangerous
    # direction: an inflated queue gets complained about, an erased one does
    # not. So a requirement whose every option is `na` is called out rather
    # than left to be noticed in a column of zeroes.
    blind = [tally for tally in requirements if tally.blind]

    caveats = list(summary.caveats)
    if blind and len(in_scope):
        names = ", ".join(sorted({t.label for t in blind}))
        caveats.append(
            f"{len(blind)} requirement(s) applied to no option in scope and are "
            f"reported as not applicable throughout: {names}. Either this scope "
            "genuinely holds none of the sub-categories they apply to, or the "
            "sub-category names in the mirror do not match the registry's "
            "applies-when lists — in which case the requirement has dropped out "
            "of this report rather than showing as a gap. Check one product's "
            "sub-category against the registry before treating these as clean."
        )
    # The flags carry the caveat even when the profile does not: "out of stock"
    # is on every report by default, and a caveat that only appears when
    # somebody happens to tick the stock attribute is a caveat nobody sees.
    if any(i.stock_backed and i.count for i in issues) and STOCK_CAVEAT not in caveats:
        caveats.append(STOCK_CAVEAT)
    if catalogue.unread:
        caveats.append(
            f"{len(catalogue.unread)} attribute/market combinations have never "
            "been read from any source. They are counted as unknown, never as "
            "missing, and never toward a fill rate."
        )

    return CoverageReport(
        profile=profile,
        scope=scope,
        summary=summary,
        requirements=requirements,
        issues=issues,
        buckets=dict(buckets),
        rows=rows,
        standings={c.model_color: c.standing for c in coverages},
        gaps_by_product={c.model_color: c.missing for c in coverages},
        caveats=caveats,
        generated_at=catalogue.generated_at,
    )


def coverage_report(
    profile_key: str = "sellable",
    scope: Scope | None = None,
) -> CoverageReport:
    """Convenience for the CLI and the API — open a session, read, report."""
    profile = PRESETS_BY_KEY.get(profile_key)
    if profile is None:
        raise KeyError(
            f"unknown profile {profile_key!r}; "
            f"known: {', '.join(sorted(PRESETS_BY_KEY))}"
        )
    with session_scope() as db:
        return build_report(load_catalogue(db), profile, scope)


def gap_queue(
    catalogue: Catalogue,
    profile: Profile,
    model_colors: Iterable[str],
) -> dict[Owner, list[tuple[str, str, str]]]:
    """Every gap in scope, grouped by who fixes it.

    The answer to "what can we do about them": ours becomes rows in an Omnitron
    import file, the partner's becomes rows on a product card, and the system's
    becomes a conversation with whoever owns the feed.
    """
    queue: dict[Owner, list[tuple[str, str, str]]] = {o: [] for o in Owner}
    for mc in model_colors:
        facts = catalogue.facts.get(mc)
        if facts is None:
            continue
        for gap in evaluate_product(facts, profile, catalogue.registry).gaps:
            queue[gap.owner].append((gap.model_color, gap.label, gap.scope))
    return queue


# ── The extract ────────────────────────────────────────────────────────────
#
# `coverage.json` is the seam. The generator writes it from the database; the
# dashboard fetches it and evaluates profiles in the browser; the download
# endpoint reads it back and builds the workbook. One file, so the page and the
# spreadsheet cannot disagree — which they would within a week if the page read
# an extract and the download re-queried the mirror.
#
# Only *presence* travels, never values. That is what keeps a file covering
# 6,000 options small enough to load whole, and it means the extract carries no
# product copy, no prices and nothing a customer would recognise.

EXTRACT_VERSION = 1


def _markets_field(markets: Iterable[str]) -> str:
    """`["PL","CZ"]` → `"PL,CZ"`. Repeated ~160,000 times in a real extract,
    where the bracket-and-quote overhead of a JSON array is most of the byte
    count and none of the information."""
    return ",".join(sorted(markets))


def _markets_from(text: str) -> tuple[str, ...]:
    return tuple(part for part in (text or "").split(",") if part)


def to_extract(catalogue: Catalogue) -> dict:
    """The whole catalogue as the dashboard's `coverage.json`.

    The registry, the issue vocabulary and the preset profiles ride along, so
    the frontend renders whatever the seed defines rather than a list somebody
    remembered to keep in step. That mistake has already been made once on the
    attribute matrix, where three hardcoded rows claimed `missing` against
    13,339 real values.
    """
    return {
        "version": EXTRACT_VERSION,
        "generatedAt": catalogue.generated_at.isoformat(),
        "markets": list(MARKETS),
        "shared": SHARED,
        "attributes": [
            {
                "key": meta.key,
                "label": meta.label,
                "owner": meta.owner.value,
                "group": meta.group,
                "perMarket": meta.per_market,
                "appliesWhen": meta.applies_when_subcategory,
                # Which markets a value has ever been seen for. The frontend
                # needs this to tell `missing` from `unknown`, and it is a
                # catalogue-level fact, so it is stated once here rather than
                # repeated on every product.
                "sourced": _markets_field(catalogue.sourced.get(meta.key, ())),
            }
            for meta in sorted(catalogue.registry.values(), key=lambda m: (m.group, m.key))
        ],
        "issues": [
            {
                "key": issue.key,
                "label": issue.label,
                "definition": issue.definition,
                "remedy": issue.remedy,
                "stockBacked": issue.stock_backed,
            }
            for issue in ISSUES
        ],
        "profiles": [
            {
                "key": profile.key,
                "label": profile.label,
                "blurb": profile.blurb,
                "requirements": [
                    {"attribute": r.attribute, "markets": list(r.markets)}
                    for r in profile.requirements
                ],
            }
            for profile in PRESETS
        ],
        "stockCaveat": STOCK_CAVEAT,
        "unread": [[key, market] for key, market in catalogue.unread],
        "products": [
            {
                "mc": row.model_color,
                "name": row.name,
                "season": row.season,
                "sub": row.sub_category,
                "gender": row.gender,
                "skus": row.sku_count,
                "markets": _markets_field(row.markets),
                "flags": list(row.flags),
                "filled": {
                    key: _markets_field(markets)
                    for key, markets in sorted(
                        catalogue.facts[row.model_color].filled.items()
                    )
                    if markets
                },
                "comment": row.comment,
                "image": row.image,
                "stock": row.best_stock,
            }
            for row in (catalogue.rows[mc] for mc in catalogue.model_colors())
        ],
    }


def from_extract(payload: dict) -> Catalogue:
    """Rebuild a `Catalogue` from `coverage.json`.

    The inverse of `to_extract`, and the reason the downloaded workbook shows
    the same numbers as the page: the download path never touches the database,
    so there is no second read to drift from the first.
    """
    version = payload.get("version")
    if version != EXTRACT_VERSION:
        raise ValueError(
            f"coverage extract is version {version!r}, this build reads "
            f"{EXTRACT_VERSION}. Regenerate it with "
            "scripts/build_demo_data_from_db.py."
        )

    registry = {
        entry["key"]: AttributeMeta(
            key=entry["key"],
            label=entry["label"],
            owner=Owner(entry["owner"]),
            per_market=bool(entry["perMarket"]),
            applies_when_subcategory=entry.get("appliesWhen") or "",
            group=entry.get("group", "Other"),
        )
        for entry in payload.get("attributes", [])
    }
    sourced = {
        entry["key"]: _markets_from(entry.get("sourced", ""))
        for entry in payload.get("attributes", [])
    }

    facts: dict[str, ProductFacts] = {}
    rows: dict[str, ProductRow] = {}
    for entry in payload.get("products", []):
        mc = entry["mc"]
        facts[mc] = ProductFacts(
            model_color=mc,
            sub_category=entry.get("sub") or None,
            filled={
                key: _markets_from(text)
                for key, text in (entry.get("filled") or {}).items()
            },
            sourced=sourced,
        )
        rows[mc] = ProductRow(
            model_color=mc,
            name=entry.get("name", ""),
            season=entry.get("season", ""),
            sub_category=entry.get("sub", ""),
            gender=entry.get("gender", ""),
            sku_count=int(entry.get("skus", 0)),
            markets=_markets_from(entry.get("markets", "")),
            flags=tuple(entry.get("flags", ())),
            comment=entry.get("comment", ""),
            image=entry.get("image", ""),
            best_stock=int(entry.get("stock", 0)),
        )

    generated = payload.get("generatedAt")
    return Catalogue(
        registry=registry,
        facts=facts,
        rows=rows,
        sourced=sourced,
        generated_at=(
            datetime.fromisoformat(generated) if generated else datetime.now(UTC)
        ),
        unread=[(key, market) for key, market in payload.get("unread", [])],
    )


def profile_from_payload(payload: dict) -> Profile:
    """A profile posted by the panel — either a preset key or an explicit set.

    Ad-hoc profiles are how somebody asks a question nobody wrote a preset for,
    which is most questions. An empty requirement list is rejected rather than
    silently answered: it would report every option complete, which is a
    plausible number and a meaningless one.
    """
    key = payload.get("profile")
    if key and key in PRESETS_BY_KEY:
        return PRESETS_BY_KEY[key]

    raw = payload.get("requirements") or []
    requirements = tuple(
        Requirement(
            attribute=str(item["attribute"]),
            markets=tuple(str(m) for m in item.get("markets", ())),
        )
        for item in raw
        if item.get("attribute")
    )
    if not requirements:
        raise ValueError(
            "No requirements chosen. Pick at least one attribute, or name a "
            "preset — an empty definition of complete would report every "
            "option complete."
        )
    return Profile(
        key=str(payload.get("key") or "custom"),
        label=str(payload.get("label") or "Custom selection"),
        blurb=str(
            payload.get("blurb")
            or f"{len(requirements)} attributes chosen for this analysis."
        ),
        requirements=requirements,
    )
