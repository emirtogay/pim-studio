"""The go-live funnel, computed from the database.

`rules/funnel` decides what a stage means. This module knows where the facts
live, and it builds on `reports/coverage.load_catalogue` rather than beside it:
the funnel's shared stages *are* coverage requirements, and a second join over
the same tables would be a second set of numbers to reconcile.

What it adds to that read is grain. Coverage works at ModelColor level, which
is all a requirement needs. Liveness does not: catalogue membership, price and
stock are per SKU per market, and the rules have to be ANDed per SKU before the
"any size counts" rollup — the finding recorded as F5 in
`Findings/Audit 2026-08-20`. So four extra queries pull the SKU-grain facts and
`rules.funnel.market_liveness` does the deciding.

Two callers share it — the CLI report and the extract generator — which is the
same arrangement, and the same reason, as coverage: a number that can only be
produced by loading a web page cannot be checked against a spreadsheet.

## Writing is the generator's job, not this module's

`record_snapshot` exists here because it writes what `build_overview` produced
and the two must not drift. It is called only by the generator. The reports CLI
stays read-only, as its own docstring promises — a report that mutates the
database as a side effect of being run is a report nobody can run twice while
debugging.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import (
    Attribute,
    AttributeValue,
    CatalogueMembership,
    OverviewSnapshot,
    Product,
    ValueConflict,
)
from ..rules.coverage import (
    ISSUES_BY_KEY,
    MARKETS,
    REMEDIES,
    SHARED,
    STOCK_CAVEAT,
    AttributeMeta,
    Owner,
)
from ..rules.funnel import (
    BASE_ATTRIBUTES,
    COMPUTABLE_STAGES,
    IMAGE_ATTRIBUTE,
    LOCALISED_ATTRIBUTES,
    STAGES,
    MarketVerdict,
    PipelineFacts,
    cumulative,
    defect_english,
    defect_name_detail,
    fold_seasons,
    market_liveness,
    missing_label,
    pick_baseline,
    stage_checks,
)
from ..rules.language import FLAG_THRESHOLD, english_confidence
from ..rules.liveness import STOCK_THRESHOLD, Blocker
from ..sync.storefront import load_listings
from .coverage import Catalogue, load_catalogue

#: Bumped when a stage's definition changes. A snapshot written under a
#: different version is not a baseline — see `OverviewSnapshot.version`.
OVERVIEW_VERSION = 1

log = logging.getLogger(__name__)

#: The content attributes wrong-language detection is run over.
LANGUAGE_FIELDS = ("product_name", "product_detail", "description")

#: The slice key meaning "no season filter".
ALL_SLICE = "all"

#: What a product with no season reads as. Matches the generator's facet value.
NO_SEASON = "—"


# ── Loading ────────────────────────────────────────────────────────────────


@dataclass
class FunnelData:
    """One read of the mirror, in the shape the funnel wants."""

    generated_at: datetime
    catalogue: Catalogue
    products: dict[str, PipelineFacts]
    #: ModelColor → the season spelling the page should show.
    seasons: dict[str, str]
    #: ModelColor → attribute key → market → English confidence, above the
    #: flagging threshold only.
    en_flags: dict[str, dict[str, dict[str, float]]]
    #: ModelColor → markets whose name and detail disagree.
    name_detail: dict[str, tuple[str, ...]]
    #: Attribute key → how many ModelColors have SKUs disagreeing on it.
    conflicts: dict[str, int]
    #: Market → the options its storefront was last seen selling. Empty for a
    #: market the pull has never reached, which is why `is_live` asks whether
    #: the set is populated rather than whether an option is in it.
    listed: dict[str, frozenset[str]] = field(default_factory=dict)
    #: When the storefronts were last read. `None` until the stage first runs.
    #: Every live count carries this; one presented without it is what let a
    #: figure 18 days old read as today's.
    observed_at: datetime | None = None

    @property
    def registry(self) -> dict[str, AttributeMeta]:
        return self.catalogue.registry

    @property
    def population(self) -> int:
        return len(self.products)

    def is_live(self, model_color: str, market: str) -> bool:
        """Whether an option is live in a market — observed, or failing that,
        derived.

        The storefront is the definition and the six rules are the explanation,
        so an observation always wins. On 2026-09-07 the rules said 3,931 for
        Poland and the shop said 4,592; the rules were reading the catalogue of
        a switched-off channel and had no way to notice.

        Falling back is not a soft option, it is the honest one: before the
        first pull there is nothing to observe, and a market that answers with
        an empty set has not told us its range is empty — it has told us
        nothing. Emptiness is therefore read as "no observation" rather than
        "nothing is live", which is the same discipline `rules/coverage` keeps
        between missing and never-read.
        """
        observed = self.listed.get(market)
        if observed:
            return model_color in observed
        facts = self.products.get(model_color)
        return bool(facts and facts.live.get(market, MarketVerdict(False)).live)

    def observed_markets(self) -> tuple[str, ...]:
        """Markets whose count comes from the shop rather than from the rules.

        Shown on the page beside the numbers. A panel mixing measured and
        computed figures without saying which is which invites exactly the
        mistake that produced this class: two numbers that look alike, one of
        which cannot be wrong and one of which was.
        """
        return tuple(m for m in MARKETS if self.listed.get(m))


def load_funnel(db: Session) -> FunnelData:
    """Read everything the funnel needs: coverage's join, plus SKU grain."""
    catalogue = load_catalogue(db)
    attr_key = {row[0]: row[1] for row in db.execute(select(Attribute.id, Attribute.key))}
    key_id = {key: aid for aid, key in attr_key.items()}

    mc_skus: dict[str, list[str]] = defaultdict(list)
    sku_to_mc: dict[str, str] = {}
    for sku, mc in db.execute(
        select(Product.sku, Product.model_color).where(Product.is_size())
    ):
        mc_skus[mc].append(sku)
        sku_to_mc[sku] = mc

    member: dict[str, set[str]] = defaultdict(set)
    for sku, market in db.execute(
        select(Product.sku, CatalogueMembership.market)
        .join(CatalogueMembership, CatalogueMembership.product_id == Product.id)
        .where(CatalogueMembership.left_at.is_(None), Product.is_size())
    ):
        member[sku].add(market)

    priced: dict[str, set[str]] = defaultdict(set)
    stock: dict[str, dict[str, int]] = defaultdict(dict)
    markets_priced: set[str] = set()
    markets_stocked: set[str] = set()
    wanted = {key_id.get("price"), key_id.get("stock")} - {None}
    if wanted:
        for entity_key, attribute_id, market, value in db.execute(
            select(
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.market,
                AttributeValue.value,
            ).where(
                AttributeValue.state_lane == "current",
                AttributeValue.attribute_id.in_(wanted),
            )
        ):
            key = attr_key.get(attribute_id)
            # A row that exists at all is evidence the column was read, value
            # or not. Same ordering as `load_catalogue`, and for the same
            # reason: this is the distinction the whole report turns on.
            if key == "price":
                markets_priced.add(market)
            elif key == "stock":
                markets_stocked.add(market)
            if value is None or not str(value).strip():
                continue
            if key == "price":
                priced[entity_key].add(market)
            else:
                try:
                    stock[entity_key][market] = int(float(value))
                except ValueError:
                    continue

    en_flags, name_detail = _content_defects(db, attr_key, sku_to_mc)
    seasons = _seasons(catalogue)

    images_synced = any(row.image for row in catalogue.rows.values())
    products: dict[str, PipelineFacts] = {}
    for mc, row in catalogue.rows.items():
        skus = mc_skus.get(mc, ())
        in_markets = frozenset(row.markets)
        products[mc] = PipelineFacts(
            model_color=mc,
            facts=catalogue.facts[mc],
            in_markets=in_markets,
            priced_markets=frozenset(
                m for s in skus for m in priced.get(s, ()) if m in in_markets
            ),
            stocked_markets=frozenset(
                m
                for s in skus
                for m, qty in stock.get(s, {}).items()
                if qty > STOCK_THRESHOLD and m in in_markets
            ),
            markets_priced=frozenset(markets_priced),
            markets_stocked=frozenset(markets_stocked),
            live=market_liveness(
                MARKETS,
                skus,
                in_markets=in_markets,
                member=member,
                priced=priced,
                stock=stock,
                markets_priced=markets_priced,
                markets_stocked=markets_stocked,
                banned="removed" in row.flags,
                images_synced=images_synced,
                has_image=bool(row.image),
            ),
        )

    listed, observed_at = load_listings(db)
    return FunnelData(
        generated_at=catalogue.generated_at,
        listed={m: frozenset(v) for m, v in listed.items()},
        observed_at=observed_at,
        catalogue=catalogue,
        products=products,
        seasons=seasons,
        en_flags=en_flags,
        name_detail=name_detail,
        conflicts=_conflicts(db, attr_key),
    )


def _seasons(catalogue: Catalogue) -> dict[str, str]:
    """ModelColor → display season, folded to one spelling per season."""
    canon = fold_seasons(row.season for row in catalogue.rows.values())
    return {
        mc: canon.get((row.season or "").casefold(), row.season or NO_SEASON) or NO_SEASON
        for mc, row in catalogue.rows.items()
    }


def _content_defects(
    db: Session,
    attr_key: Mapping[int, str],
    sku_to_mc: Mapping[str, str],
) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, tuple[str, ...]]]:
    """Wrong-language flags and name/detail disagreements, per ModelColor.

    Run here, at generation, so the number ships with the data and nothing
    downstream re-implements it — `rules.language` is explicit about that.
    Only the English verdict is trusted, only local-market fields are judged,
    and only confident calls are kept: below the threshold the flag is noise,
    and a flag that cries wolf stops being read.
    """
    wanted = {aid for aid, key in attr_key.items() if key in LANGUAGE_FIELDS}
    en_flags: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    texts: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    if not wanted:
        return {}, {}

    for entity_kind, entity_key, attribute_id, market, value in db.execute(
        select(
            AttributeValue.entity_kind,
            AttributeValue.entity_key,
            AttributeValue.attribute_id,
            AttributeValue.market,
            AttributeValue.value,
        ).where(
            AttributeValue.state_lane == "current",
            AttributeValue.attribute_id.in_(wanted),
            AttributeValue.market != SHARED,
        )
    ):
        key = attr_key.get(attribute_id)
        if key is None or value is None or not str(value).strip():
            continue
        mc = entity_key if entity_kind == "model_color" else sku_to_mc.get(entity_key)
        if mc is None:
            continue
        text = str(value)
        texts[mc][market][key] = text
        confidence = english_confidence(text)
        if confidence >= FLAG_THRESHOLD:
            en_flags[mc].setdefault(key, {})[market] = round(confidence, 2)

    name_detail: dict[str, tuple[str, ...]] = {}
    for mc, by_market in texts.items():
        differing = tuple(
            sorted(
                market
                for market, fields in by_market.items()
                if fields.get("product_name")
                and fields.get("product_detail")
                and fields["product_name"] != fields["product_detail"]
            )
        )
        if differing:
            name_detail[mc] = differing
    return dict(en_flags), name_detail


def _conflicts(db: Session, attr_key: Mapping[int, str]) -> dict[str, int]:
    """How many options have SKUs disagreeing on one attribute's value.

    "Name differs between sizes" in the design brief. The conflicts table is
    replaced wholesale each run, so this is a snapshot of the latest sync —
    which is what the page wants.
    """
    counts: dict[str, int] = defaultdict(int)
    for (attribute_id,) in db.execute(select(ValueConflict.attribute_id)):
        key = attr_key.get(attribute_id)
        if key:
            counts[key] += 1
    return dict(counts)


# ── Building the extract ───────────────────────────────────────────────────


def _link(page: str, *params: tuple[str, str]) -> dict:
    """A deep link as data, never as a formatted string.

    The frontend renders it; Python decides it. Three copies of a facet value
    in three languages is three chances for a work queue to silently come back
    empty, and an empty queue looks exactly like a job already done.
    """
    return {"page": page, "params": [list(p) for p in params]}


def _reason(
    key: str,
    label: str,
    count: int,
    owner: Owner,
    link: dict | None,
    remedy: str = "",
) -> dict:
    """One reason a stage stopped an option, and what to do about it.

    The remedy defaults to the owner's generic one — right for a gap in a
    field somebody types. For the issue-shaped stages it is passed explicitly
    from `rules.coverage.ISSUES`, where the advice is concrete and already
    written: the generic text would tell a reader that a missing image
    "arrives from a feed, nothing to upload" when in fact there is a script
    that publishes it, and that a merch ban goes out "on the next import
    file" when the correct action is to leave it alone.
    """
    return {
        "key": key,
        "label": label,
        "count": count,
        "owner": owner.value,
        "remedy": remedy or REMEDIES[owner].action,
        "link": link,
    }


def _stage_reasons(
    stage_key: str,
    market: str | None,
    members: Sequence[PipelineFacts],
    data: FunnelData,
) -> list[dict]:
    """Why this stage's drop happened, ordered by how many it stopped."""
    reasons: list[dict] = []
    if stage_key == "base":
        for key in BASE_ATTRIBUTES:
            meta = data.registry.get(key)
            if meta is None:
                continue
            count = sum(
                1 for pf in members if pf.facts.verdict(meta, SHARED).counts_as_gap
            )
            if count:
                reasons.append(
                    _reason(
                        key,
                        f"no {meta.label.lower()}",
                        count,
                        meta.owner,
                        _link("coverage", ("req", ",".join(BASE_ATTRIBUTES))),
                    )
                )
    elif stage_key == "imaged":
        meta = data.registry.get(IMAGE_ATTRIBUTE)
        if meta is not None:
            count = sum(
                1 for pf in members if pf.facts.verdict(meta, SHARED).counts_as_gap
            )
            if count:
                reasons.append(
                    _reason(
                        IMAGE_ATTRIBUTE,
                        "no image",
                        count,
                        # The stage's owner, not the attribute's write channel.
                        # `image_0` is read-only in the registry because we
                        # cannot put a URL in an import file — but publishing
                        # the image is still our job, as the issue's remedy
                        # says. The channel is not the desk.
                        Owner.US,
                        _link("products", ("defect", "No image")),
                        ISSUES_BY_KEY["no_image"].remedy,
                    )
                )
    elif stage_key == "localised" and market:
        for key, field_name in zip(LOCALISED_ATTRIBUTES, ("name", "description"), strict=True):
            meta = data.registry.get(key)
            if meta is None:
                continue
            count = sum(
                1
                for pf in members
                if pf.facts.verdict(meta, market if meta.per_market else SHARED).counts_as_gap
            )
            if count:
                reasons.append(
                    _reason(
                        key,
                        f"no {market} {field_name}",
                        count,
                        meta.owner,
                        _link("products", ("missing", missing_label(market, field_name))),
                    )
                )
    elif stage_key == "priced" and market:
        outside = [pf for pf in members if market not in pf.in_markets]
        nowhere = [pf for pf in outside if not pf.in_markets]
        partial = [pf for pf in outside if pf.in_markets]
        if nowhere:
            reasons.append(
                _reason(
                    "not_in_catalogue",
                    "in no catalogue",
                    len(nowhere),
                    Owner.US,
                    _link("products", ("defect", "Not in any catalogue")),
                    ISSUES_BY_KEY["not_in_catalogue"].remedy,
                )
            )
        if partial:
            reasons.append(
                _reason(
                    "not_in_market_catalogue",
                    f"not in the {market} catalogue",
                    len(partial),
                    Owner.US,
                    _link("products", ("missing", missing_label(market, "catalogue"))),
                    ISSUES_BY_KEY["not_in_catalogue"].remedy,
                )
            )
        unpriced = sum(
            1
            for pf in members
            if market in pf.in_markets and market not in pf.priced_markets
        )
        if unpriced:
            reasons.append(
                _reason(
                    "no_price",
                    f"no {market} price",
                    unpriced,
                    Owner.US,
                    _link("products", ("missing", missing_label(market, "price"))),
                    ISSUES_BY_KEY["no_price"].remedy,
                )
            )
    elif stage_key == "stocked" and market:
        count = sum(1 for pf in members if market in pf.in_markets)
        if count:
            reasons.append(
                _reason(
                    "no_stock",
                    f"no {market} stock",
                    count,
                    Owner.US,
                    _link("products", ("missing", missing_label(market, "stock"))),
                    ISSUES_BY_KEY["no_stock"].remedy,
                )
            )
    elif stage_key == "live" and market:
        by_blocker: dict[str, int] = defaultdict(int)
        for pf in members:
            by_blocker[pf.live.get(market, MarketVerdict(False)).blocker] += 1
        for blocker, count in sorted(by_blocker.items(), key=lambda kv: -kv[1]):
            if not count:
                continue
            # Banned has no facet of its own on the Products page, so this
            # reason carries its count and its remedy but no link. Saying so
            # beats linking somewhere that would quietly filter to nothing.
            banned = blocker == Blocker.BANNED
            reasons.append(
                _reason(
                    "banned" if banned else "blocked",
                    blocker.lower() if blocker else "blocked",
                    count,
                    Owner.US,
                    None,
                    ISSUES_BY_KEY["removed"].remedy if banned else "",
                )
            )
    return sorted(reasons, key=lambda r: -r["count"])


def _slice(key: str, members: Sequence[PipelineFacts], data: FunnelData) -> dict:
    """One season's funnel — or the whole catalogue, when key is `all`."""
    stage_keys = [stage.key for stage in COMPUTABLE_STAGES]
    counts: dict[str, dict] = {
        stage.key: ({"markets": {}} if stage.per_market else {"shared": None})
        for stage in COMPUTABLE_STAGES
    }
    # stage index → market → the products that first failed there.
    dropped: dict[int, dict[str, list[PipelineFacts]]] = defaultdict(
        lambda: defaultdict(list)
    )
    passed: dict[str, dict[str, int]] = {m: defaultdict(int) for m in MARKETS}
    indeterminate: dict[str, dict[str, int]] = {m: defaultdict(int) for m in MARKETS}

    # Live on the site while the funnel still has it stopped earlier. Counted
    # per product, in the market where it is actually live — comparing a
    # live-anywhere total against a per-market stage count would be two
    # different populations wearing one label.
    live_despite_gaps = 0

    for pf in members:
        incomplete_but_live = False
        for market, row in stage_checks(pf, data.registry, MARKETS).items():
            statuses, first = cumulative(row)
            for stage_key, status in zip(stage_keys, statuses, strict=True):
                if status == "passed":
                    passed[market][stage_key] += 1
                elif status == "indeterminate":
                    indeterminate[market][stage_key] += 1
            if first is not None:
                dropped[first][market].append(pf)
                if pf.live.get(market, MarketVerdict(False)).live:
                    incomplete_but_live = True
        if incomplete_but_live:
            live_despite_gaps += 1

    for stage in COMPUTABLE_STAGES:
        if stage.per_market:
            counts[stage.key]["markets"] = {
                m: {
                    "passed": passed[m][stage.key],
                    "indeterminate": indeterminate[m][stage.key],
                }
                for m in MARKETS
            }
        else:
            # A shared stage answers the same in every market, so reporting it
            # four times would invite a reader to compare four equal numbers
            # and wonder what they mean. Take the first market's column.
            head = MARKETS[0]
            counts[stage.key]["shared"] = {
                "passed": passed[head][stage.key],
                "indeterminate": indeterminate[head][stage.key],
            }

    drops = []
    for index in range(1, len(COMPUTABLE_STAGES)):
        stage = COMPUTABLE_STAGES[index]
        here = dropped.get(index, {})
        if stage.per_market:
            entry = {
                "from": COMPUTABLE_STAGES[index - 1].key,
                "to": stage.key,
                "markets": {
                    m: {
                        "count": len(here.get(m, ())),
                        "reasons": _stage_reasons(stage.key, m, here.get(m, ()), data),
                    }
                    for m in MARKETS
                },
            }
        else:
            members_here = here.get(MARKETS[0], ())
            entry = {
                "from": COMPUTABLE_STAGES[index - 1].key,
                "to": stage.key,
                "shared": {
                    "count": len(members_here),
                    "reasons": _stage_reasons(stage.key, None, members_here, data),
                },
            }
        drops.append(entry)

    live_now = {
        m: sum(1 for pf in members if data.is_live(pf.model_color, m))
        for m in MARKETS
    }
    live_anywhere = sum(
        1 for pf in members if any(data.is_live(pf.model_color, m) for m in MARKETS)
    )
    # Live on the shop while our own rules say it is blocked. Not noise — it is
    # the reconciliation queue, and it is where a market's move onto catalogue
    # 601 shows up as a number until `scope` follows it.
    unexplained = {
        m: sum(
            1
            for pf in members
            if data.is_live(pf.model_color, m)
            and not pf.live.get(m, MarketVerdict(False)).live
        )
        for m in MARKETS
    }
    return {
        "key": key,
        "population": len(members),
        "counts": counts,
        "drops": drops,
        "headline": {
            "live": live_anywhere,
            "blocked": len(members) - live_anywhere,
            # A shared stage fails identically in all four markets, so one
            # market's bucket is the product count. Summing across markets
            # would report every blocked option four times.
            "waitingOnPartner": len(
                dropped.get(stage_keys.index("base"), {}).get(MARKETS[0], ())
            ),
        },
        "liveNow": live_now,
        "liveNowAnywhere": live_anywhere,
        #: Per market, how many of `liveNow` came from the shop rather than the
        #: rules, and how many the two disagree about.
        "observedMarkets": list(data.observed_markets()),
        "observedAt": data.observed_at.isoformat() if data.observed_at else None,
        "unexplained": unexplained,
        # Not a contradiction: the site enforces the six liveness rules, not
        # our content pipeline, so an option with no Czech description can be
        # live in Czechia. Shown rather than reconciled away.
        "liveDespiteGaps": live_despite_gaps,
        "defects": _defects(members, data),
    }


def _defects(members: Sequence[PipelineFacts], data: FunnelData) -> list[dict]:
    """Things that are wrong rather than missing. Never part of the funnel.

    A filled field can still be defective — a Slovak description holding
    English passes every not-null check there is. Different work, different
    person, so it sits in its own panel.
    """
    keys = {pf.model_color for pf in members}
    rows: list[dict] = []

    for key, field_label in zip(LANGUAGE_FIELDS, ("name", "detail", "description"), strict=True):
        for market in MARKETS:
            count = sum(
                1
                for mc in keys
                if market in data.en_flags.get(mc, {}).get(key, {})
            )
            if count:
                label = defect_english(market, field_label)
                rows.append(
                    {
                        "key": f"en:{key}:{market}",
                        "label": label,
                        "count": count,
                        "link": _link("products", ("defect", label)),
                    }
                )

    for market in MARKETS:
        count = sum(1 for mc in keys if market in data.name_detail.get(mc, ()))
        if count:
            label = defect_name_detail(market)
            rows.append(
                {
                    "key": f"name_detail:{market}",
                    "label": label,
                    "count": count,
                    "link": _link("products", ("defect", label)),
                }
            )

    return sorted(rows, key=lambda r: -r["count"])


def _caveats(data: FunnelData) -> list[dict]:
    """What these numbers do not know, stated beside them rather than in a
    tooltip — a caveat that only exists in the browser does not survive being
    emailed."""
    caveats = [
        {
            "key": "assortment",
            "stage": "assortment",
            "text": (
                "No source for the assortment is ingested, so the drop between "
                "what we intend to sell and what exists in Omnitron cannot be "
                "measured here. The funnel starts at the second stage."
            ),
        },
        {
            "key": "category",
            "stage": "live",
            "text": (
                "Five of the six go-live rules are checked. Category assignment "
                "is not synced, so nothing here claims all six hold."
            ),
        },
        {
            "key": "hu_locale",
            "stage": "localised",
            "text": (
                "Hungarian content may not be what it appears. Omnitron's "
                "per-locale store is known to be unreliable on this tenant, and "
                "whether the flat Hungarian columns were written under a Polish "
                "session is unverified — run scripts/probe_locale.py against a "
                "known SKU before trusting the Hungarian figure."
            ),
        },
    ]
    # A read-guard is a catalogue-level fact, identical on every product, so
    # any one of them answers whether stock was synced at all.
    stocked_markets = next(
        (pf.markets_stocked for pf in data.products.values()), frozenset()
    )
    if stocked_markets:
        caveats.append({"key": "stock", "stage": "stocked", "text": STOCK_CAVEAT})

    funnel_attributes = set(BASE_ATTRIBUTES) | set(LOCALISED_ATTRIBUTES) | {IMAGE_ATTRIBUTE}
    unread = [
        f"{key} ({market})"
        for key, market in data.catalogue.unread
        if key in funnel_attributes
    ]
    if unread:
        caveats.append(
            {
                "key": "unread",
                "text": (
                    "Never read, so counted as indeterminate rather than as a "
                    "gap: " + ", ".join(unread) + "."
                ),
            }
        )
    return caveats


def build_overview(data: FunnelData) -> dict:
    """The whole funnel as the dashboard's `overview.json`."""
    by_season: dict[str, list[PipelineFacts]] = defaultdict(list)
    for mc, pf in data.products.items():
        by_season[data.seasons.get(mc, NO_SEASON)].append(pf)

    everything = list(data.products.values())
    slices = {ALL_SLICE: _slice(ALL_SLICE, everything, data)}
    for season, members in by_season.items():
        slices[season] = _slice(season, members, data)

    return {
        "version": OVERVIEW_VERSION,
        "generatedAt": data.generated_at.isoformat(),
        "markets": list(MARKETS),
        "population": data.population,
        "stages": [
            {
                "key": stage.key,
                "label": stage.label,
                "briefIndex": stage.brief_index,
                "owner": stage.owner.value if stage.owner else None,
                "perMarket": stage.per_market,
                "computable": stage.computable,
                "blurb": stage.blurb,
                "attributes": list(stage.attributes),
            }
            for stage in STAGES
        ],
        "seasons": sorted(
            (
                {
                    "key": season,
                    "label": "No season" if season == NO_SEASON else season,
                    "count": len(members),
                }
                for season, members in by_season.items()
            ),
            key=lambda s: (-s["count"], s["key"]),
        ),
        "slices": slices,
        "conflicts": {
            "productName": data.conflicts.get("product_name", 0),
            "total": sum(data.conflicts.values()),
        },
        "caveats": _caveats(data),
        "deltas": None,
    }


# ── Snapshots and deltas ───────────────────────────────────────────────────


def _snapshot_payload(extract: Mapping) -> dict:
    """What is worth keeping from a build: the numbers, not the queues.

    Reasons and links are re-derivable and would multiply the row's size for
    no benefit — nobody asks what last week's work queue linked to.
    """
    return {
        "population": extract["population"],
        "slices": {
            key: {
                "population": slice_["population"],
                "headline": slice_["headline"],
                "liveNow": slice_["liveNow"],
                "liveNowAnywhere": slice_["liveNowAnywhere"],
            }
            for key, slice_ in extract["slices"].items()
        },
    }


def record_snapshot(db: Session, extract: Mapping, today: date) -> bool:
    """Store today's numbers, replacing today's row if one already exists.

    Select-then-update rather than a dialect-specific upsert: this runs on
    SQLite in development and Postgres in production, and one code path that
    works on both beats two that each work on one.

    Returns whether it was written, and never raises. The extract is the
    product; the snapshot is a baseline for next week's delta. On deploy the
    host regenerates the extract the moment the new image is up, racing that
    container's own `alembic upgrade head` — so on the release that first adds
    this table, the write can arrive before the table does. Letting that abort
    the run would cost `products.json` and `coverage.json` too, and the panel
    would serve a stale catalogue until the 03:00 sync to protect a number
    nobody can read yet. Same call the entrypoint makes about the changelog: a
    missing extra is not a reason to refuse the main job.
    """
    try:
        payload = json.dumps(_snapshot_payload(extract), separators=(",", ":"))
        existing = db.scalar(
            select(OverviewSnapshot).where(OverviewSnapshot.snapshot_date == today)
        )
        if existing is None:
            db.add(
                OverviewSnapshot(
                    snapshot_date=today, payload=payload, version=OVERVIEW_VERSION
                )
            )
        else:
            existing.payload = payload
            existing.version = OVERVIEW_VERSION
            existing.created_at = datetime.now(UTC)
        db.flush()
        return True
    except SQLAlchemyError:
        # Flushed here rather than left to the caller's commit, so a failure
        # surfaces inside this try instead of taking the whole session down on
        # the way out.
        db.rollback()
        log.warning("overview snapshot not recorded", exc_info=True)
        return False


def compute_deltas(db: Session, extract: Mapping, today: date) -> dict | None:
    """Movement since the baseline, or None when there is nothing to compare.

    None is a real answer and the page prints it as one. A delta of zero and
    "we have no history yet" look identical on a dashboard and mean opposite
    things.
    """
    try:
        rows = {
            row.snapshot_date: row
            for row in db.scalars(
                select(OverviewSnapshot).where(
                    OverviewSnapshot.version == OVERVIEW_VERSION
                )
            )
        }
    except SQLAlchemyError:
        # No table yet — the same deploy race `record_snapshot` documents.
        # "We cannot compare" is exactly what None already means here.
        db.rollback()
        log.warning("overview baseline unreadable", exc_info=True)
        return None
    baseline_date = pick_baseline(list(rows), today)
    if baseline_date is None:
        return None
    try:
        baseline = json.loads(rows[baseline_date].payload)
    except (TypeError, ValueError):
        return None

    age = today.toordinal() - baseline_date.toordinal()
    deltas: dict[str, dict] = {}
    for key, slice_ in extract["slices"].items():
        was = baseline.get("slices", {}).get(key)
        if not was:
            continue
        deltas[key] = {
            "baselineDate": baseline_date.isoformat(),
            "ageDays": age,
            "live": slice_["headline"]["live"] - was["headline"]["live"],
            "blocked": slice_["headline"]["blocked"] - was["headline"]["blocked"],
            "waitingOnPartner": (
                slice_["headline"]["waitingOnPartner"]
                - was["headline"]["waitingOnPartner"]
            ),
            "liveByMarket": {
                m: slice_["liveNow"].get(m, 0) - was.get("liveNow", {}).get(m, 0)
                for m in MARKETS
            },
        }
    return deltas or None


# ── The terminal report ────────────────────────────────────────────────────


@dataclass
class OverviewReport:
    """What the CLI prints, so a test can assert on structure not stdout."""

    extract: dict
    season: str = ""
    deltas: dict | None = None

    @property
    def slice(self) -> dict:
        return self.extract["slices"].get(
            self.season or ALL_SLICE, self.extract["slices"][ALL_SLICE]
        )


def overview_report(season: str = "") -> OverviewReport:
    """Build the funnel and hand it back. Never writes — see the module doc."""
    with session_scope() as db:
        data = load_funnel(db)
        extract = build_overview(data)
        deltas = compute_deltas(db, extract, datetime.now(UTC).date())
    extract["deltas"] = deltas
    return OverviewReport(extract=extract, season=season, deltas=deltas)
