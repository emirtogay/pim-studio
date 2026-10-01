"""The go-live funnel: how far each option got, and where it stopped.

Coverage asks "what is missing across the catalogue". This asks the ordered
question: of the options we intend to sell, how many got a record, then base
attributes, then localisation, then a price, then stock, then onto the site —
and at which step did the rest stop. The answer is a work queue per gap, which
is why the stages are ordered and cumulative rather than independent tallies.

Three things make that harder than a `GROUP BY`:

**Unknown is a third answer.** A stage whose source was never read is neither
passed nor failed. Folding it into either is the mistake `rules.coverage`
exists to prevent, and it is worse here: a funnel is read as a shape, and one
mis-bucketed stage bends the whole curve. Unknown flows through to an
`indeterminate` count that sits beside the bar, never inside it.

**The order is a display choice, and the brief's order does not work.** The
design brief lists Localised before Imaged, but its own counts rise across
that pair, which cannot happen in a cumulative funnel. Shared-scope stages are
therefore grouped first and per-market stages after, so that every adjacency is
monotonic and each drop names exactly one queue. `Stage.brief_index` keeps the
brief's numbering so the two can still be read against each other.

**Live is not the last stage.** A product can be live in Poland with no Czech
description and no composition: the site enforces the six liveness rules, not
our content pipeline. So stage `live` here means "live with every earlier stage
complete", and the count of options actually live is reported separately rather
than reconciled away. `reports.coverage` makes the same distinction and for the
same reason.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .coverage import SHARED, AttributeMeta, Owner, ProductFacts, Verdict
from .liveness import Blocker, LivenessInput, first_failure

# ── Stages ─────────────────────────────────────────────────────────────────

#: The four attributes that make a product describable at all. All four are
#: Turkey's to write, which is why stage `base` reads as a partner queue.
BASE_ATTRIBUTES: tuple[str, ...] = (
    "main_category",
    "sub_category",
    "gender",
    "composition",
)

#: The `localised` preset, verbatim: a name and a description per market.
#: `product_detail` is deliberately not here — the preset does not require it,
#: and adding it would silently raise the bar the page reports against.
LOCALISED_ATTRIBUTES: tuple[str, ...] = ("product_name", "description")

#: The attribute carrying the first product image.
IMAGE_ATTRIBUTE = "image_0"


class StageVerdict(Enum):
    """One stage's answer for one product in one market.

    `UNKNOWN` is not a failure: it means no source we have read could say. It
    passes through the cumulative fold rather than stopping it, so an option
    with an unread Slovak description and no Polish price is counted in the
    price queue, where somebody can actually act on it.
    """

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Stage:
    """One step of the pipeline, and who is on the hook for it."""

    key: str
    label: str
    #: The stage's number in the design brief, kept through the reordering so
    #: the page and the brief can be read against each other.
    brief_index: int
    owner: Owner | None
    #: Whether the stage is asked once per product or once per market.
    per_market: bool
    #: False when no source we hold could answer it. Such a stage is shown,
    #: greyed, rather than omitted: the missing ingest is itself the finding.
    computable: bool
    blurb: str
    attributes: tuple[str, ...] = ()


#: Display order. Shared-scope stages first, then the per-market ladder — see
#: the module docstring on why this is not the brief's order.
STAGES: tuple[Stage, ...] = (
    Stage(
        key="assortment",
        label="In assortment",
        brief_index=1,
        owner=Owner.PARTNER,
        per_market=False,
        computable=False,
        blurb=(
            "What we intend to sell. No source for this is ingested, so the "
            "drop between it and the next stage is invisible from here."
        ),
    ),
    Stage(
        key="opened",
        label="Opened in Omnitron",
        brief_index=2,
        owner=Owner.PARTNER,
        per_market=False,
        computable=True,
        blurb=(
            "The product record exists. Every option we hold passes by "
            "construction — the mirror is where our product set comes from."
        ),
    ),
    Stage(
        key="base",
        label="Base attributes filled",
        brief_index=3,
        owner=Owner.PARTNER,
        per_market=False,
        computable=True,
        blurb="Category, sub-category, gender and composition all present.",
        attributes=BASE_ATTRIBUTES,
    ),
    Stage(
        key="imaged",
        label="Imaged",
        brief_index=5,
        owner=Owner.US,
        per_market=False,
        computable=True,
        blurb="At least one product image.",
        attributes=(IMAGE_ATTRIBUTE,),
    ),
    Stage(
        key="localised",
        label="Localised",
        brief_index=4,
        owner=Owner.US,
        per_market=True,
        computable=True,
        blurb="A product name and a description in that market's language.",
        attributes=LOCALISED_ATTRIBUTES,
    ),
    Stage(
        key="priced",
        label="Priced",
        brief_index=6,
        owner=Owner.US,
        per_market=True,
        computable=True,
        blurb="In that market's catalogue, and carrying a price.",
    ),
    Stage(
        key="stocked",
        label="In stock",
        brief_index=7,
        owner=Owner.US,
        per_market=True,
        computable=True,
        blurb="At least one size with stock above the threshold.",
    ),
    Stage(
        key="live",
        label="Live",
        brief_index=8,
        #: A consequence, not a queue. Nobody is assigned "make it live".
        owner=None,
        per_market=True,
        computable=True,
        blurb=(
            "Every earlier stage complete and the liveness rules passing. "
            "Category assignment is not synced, so it is not checked."
        ),
    ),
)

COMPUTABLE_STAGES: tuple[Stage, ...] = tuple(s for s in STAGES if s.computable)

STAGE_BY_KEY: Mapping[str, Stage] = {s.key: s for s in STAGES}


# ── Liveness, at ModelColor grain ──────────────────────────────────────────

#: How far through the rules a SKU got before failing, most fundamental first.
#: `max()` over this picks the *binding* constraint — the thing that, if fixed,
#: moves the product closest to sellable.
BLOCKER_ORDER: Mapping[str, int] = {
    Blocker.BANNED: 0,
    Blocker.NOT_IN_CATALOGUE: 1,
    Blocker.NO_PRICE: 2,
    Blocker.OUT_OF_STOCK: 3,
    Blocker.NO_IMAGE: 4,
    "": 5,
}


@dataclass(frozen=True, slots=True)
class MarketVerdict:
    """Whether one ModelColor is live in one market, and what stopped it."""

    live: bool
    #: "" when live; otherwise a `Blocker` string.
    blocker: str = ""


def market_liveness(
    markets: Sequence[str],
    skus: Sequence[str],
    *,
    in_markets: Collection[str],
    member: Mapping[str, Collection[str]],
    priced: Mapping[str, Collection[str]],
    stock: Mapping[str, Mapping[str, int]],
    markets_priced: Collection[str],
    markets_stocked: Collection[str],
    banned: bool,
    images_synced: bool,
    has_image: bool,
) -> dict[str, MarketVerdict]:
    """Decide liveness per market for one ModelColor, from its SKUs.

    Grain is the whole point. Banned and image are ModelColor-level facts;
    catalogue, price and stock are per SKU per market. So the two MC-level
    rules are evaluated once, and the rest go through `first_failure` per SKU
    with the "any SKU counts" rollup applied *after* the AND, never before —
    rolling up first would let a size that is in the catalogue borrow the price
    of a size that is not. Recorded as F5 in `Findings/Audit 2026-08-20`.

    Rule 5 (catalogue membership) is not modelled in `first_failure`, which is
    honest that it is not checkable from an export. It is checkable here, so it
    is checked here.
    """
    verdicts: dict[str, MarketVerdict] = {}
    for m in markets:
        if banned:
            verdicts[m] = MarketVerdict(False, Blocker.BANNED)
            continue
        if m not in in_markets:
            verdicts[m] = MarketVerdict(False, Blocker.NOT_IN_CATALOGUE)
            continue

        failures = []
        for s in skus:
            if m not in member.get(s, ()):
                continue
            failures.append(
                first_failure(
                    LivenessInput(
                        banned=False,
                        has_price_source=m in markets_priced,
                        price="set" if m in priced.get(s, ()) else "",
                        # Absence of a stock row means **zero** here, not
                        # unknown — the sync reads `stock__gt=0`, so it only
                        # ever writes rows for SKUs that have stock, and a full
                        # pass retires the ones that lose it. That is true only
                        # of a market the stock sync actually read, hence the
                        # guard rather than a bare 0: passing None instead read
                        # as "unknown", which the rule correctly abstains on,
                        # and inflated the live count by ~350 a market.
                        stock=stock.get(s, {}).get(
                            m, 0 if m in markets_stocked else None
                        ),
                        has_image_source=images_synced,
                        has_image=has_image,
                    )
                )
            )

        if "" in failures:
            verdicts[m] = MarketVerdict(True)
            continue
        # The blocker of the SKU that got furthest — the binding constraint,
        # and the thing worth fixing. Reporting the most fundamental failure
        # across sizes would name a price problem on a size nobody can buy
        # anyway while a stocked size sits one image away from selling.
        verdicts[m] = MarketVerdict(
            False,
            max(failures, key=BLOCKER_ORDER.__getitem__)
            if failures
            else Blocker.NOT_IN_CATALOGUE,
        )
    return verdicts


# ── Stage verdicts ─────────────────────────────────────────────────────────


def _fold_verdicts(verdicts: Iterable[Verdict]) -> StageVerdict:
    """A stage passes only if every attribute in it does.

    `NA` is not a requirement and is skipped. A stage of nothing but `NA` —
    every attribute inapplicable to this sub-category — passes: there is no
    work to do, which is what `NA` means.
    """
    seen_unknown = False
    for v in verdicts:
        if v is Verdict.NA:
            continue
        if v is Verdict.MISSING:
            return StageVerdict.FAILED
        if v is Verdict.UNKNOWN:
            seen_unknown = True
    return StageVerdict.UNKNOWN if seen_unknown else StageVerdict.PASSED


def judge_shared(
    facts: ProductFacts,
    keys: Sequence[str],
    registry: Mapping[str, AttributeMeta],
) -> StageVerdict:
    """A market-agnostic stage: every attribute present at the shared key.

    An attribute the registry has never heard of is `UNKNOWN`, not a failure —
    the same discipline as an unread source, for the same reason.
    """
    verdicts = []
    for key in keys:
        meta = registry.get(key)
        if meta is None:
            verdicts.append(Verdict.UNKNOWN)
            continue
        verdicts.append(facts.verdict(meta, SHARED))
    return _fold_verdicts(verdicts)


def judge_localised(
    facts: ProductFacts,
    market: str,
    registry: Mapping[str, AttributeMeta],
) -> StageVerdict:
    """The `localised` preset, restricted to one market."""
    verdicts = []
    for key in LOCALISED_ATTRIBUTES:
        meta = registry.get(key)
        if meta is None:
            verdicts.append(Verdict.UNKNOWN)
            continue
        verdicts.append(facts.verdict(meta, market if meta.per_market else SHARED))
    return _fold_verdicts(verdicts)


@dataclass(frozen=True, slots=True)
class PipelineFacts:
    """Everything the funnel needs about one ModelColor, already joined.

    Assembled by `reports.overview`, which knows where these live; consumed
    here, which knows what they mean.
    """

    model_color: str
    facts: ProductFacts
    #: Markets whose catalogue this option is in.
    in_markets: frozenset[str] = frozenset()
    #: Markets where at least one SKU carries a price / has stock.
    priced_markets: frozenset[str] = frozenset()
    stocked_markets: frozenset[str] = frozenset()
    #: Markets whose price / stock source was read at all. Outside these,
    #: the corresponding stage abstains.
    markets_priced: frozenset[str] = frozenset()
    markets_stocked: frozenset[str] = frozenset()
    live: Mapping[str, MarketVerdict] = field(default_factory=dict)


def stage_checks(
    pf: PipelineFacts,
    registry: Mapping[str, AttributeMeta],
    markets: Sequence[str] = (),
) -> dict[str, list[StageVerdict]]:
    """Every computable stage's raw verdict, per market, in display order.

    Shared stages produce the same verdict in all four columns. That is
    deliberate: the fold downstream is per market, and a shared failure should
    stop all four ladders at the same rung.
    """
    markets = tuple(markets) or tuple(pf.live) or MARKET_ORDER
    shared = {
        "opened": StageVerdict.PASSED,
        "base": judge_shared(pf.facts, BASE_ATTRIBUTES, registry),
        "imaged": judge_shared(pf.facts, (IMAGE_ATTRIBUTE,), registry),
    }

    out: dict[str, list[StageVerdict]] = {}
    for m in markets:
        row = []
        for stage in COMPUTABLE_STAGES:
            if not stage.per_market:
                row.append(shared[stage.key])
            elif stage.key == "localised":
                row.append(judge_localised(pf.facts, m, registry))
            elif stage.key == "priced":
                row.append(_priced_verdict(pf, m))
            elif stage.key == "stocked":
                row.append(_stocked_verdict(pf, m))
            else:
                row.append(
                    StageVerdict.PASSED
                    if pf.live.get(m, MarketVerdict(False)).live
                    else StageVerdict.FAILED
                )
        out[m] = row
    return out


MARKET_ORDER: tuple[str, ...] = ("PL", "CZ", "SK", "HU")


def _priced_verdict(pf: PipelineFacts, market: str) -> StageVerdict:
    """Priced folds catalogue membership in, rather than deferring it to live.

    Membership is a filter, not a verdict — an option outside a catalogue is
    not "missing a price" there, it is not sold there. Surfacing it at this
    stage keeps the price queue actionable and matches the generator, which
    only emits `"{m} price"` for members.
    """
    if market not in pf.in_markets:
        return StageVerdict.FAILED
    if market not in pf.markets_priced:
        return StageVerdict.UNKNOWN
    return StageVerdict.PASSED if market in pf.priced_markets else StageVerdict.FAILED


def _stocked_verdict(pf: PipelineFacts, market: str) -> StageVerdict:
    if market not in pf.in_markets:
        return StageVerdict.FAILED
    if market not in pf.markets_stocked:
        return StageVerdict.UNKNOWN
    return StageVerdict.PASSED if market in pf.stocked_markets else StageVerdict.FAILED


def cumulative(checks: Sequence[StageVerdict]) -> tuple[list[str], int | None]:
    """Fold raw verdicts into cumulative standings, and find the first failure.

    Returns one status per stage — `passed`, `indeterminate` or `failed` — and
    the index of the stage that first failed, which is the queue the option
    belongs in. Unknowns taint everything after them as `indeterminate` but do
    not stop the walk: an option can be indeterminate at one stage and still
    fail a later one, and the later failure is the actionable fact.
    """
    statuses: list[str] = []
    first_failed: int | None = None
    seen_unknown = False
    for i, v in enumerate(checks):
        if first_failed is not None:
            statuses.append("failed")
            continue
        if v is StageVerdict.FAILED:
            first_failed = i
            statuses.append("failed")
            continue
        if v is StageVerdict.UNKNOWN:
            seen_unknown = True
        statuses.append("indeterminate" if seen_unknown else "passed")
    return statuses, first_failed


# ── Strings that cross the Python/JSON/TypeScript boundary ─────────────────
#
# Defined once, here. The `Blocker` class earned this: three copies of "No
# price" in three languages is three chances for a facet link to point at
# nothing, and the failure is silent — an empty work queue looks like a job
# already done.


def missing_label(market: str, field_name: str) -> str:
    """A `missing[]` entry, e.g. "PL price". Must match the generator byte for
    byte: the Products page facets on this exact string."""
    return f"{market} {field_name}"


def defect_english(market: str, field_label: str) -> str:
    return f"{market} {field_label} reads as English"


def defect_name_detail(market: str) -> str:
    return f"{market} name ≠ detail"


# ── Seasons ────────────────────────────────────────────────────────────────


def fold_seasons(raw_values: Iterable[str]) -> dict[str, str]:
    """Map each spelling of a season to the one the page should show.

    Case is noise — `OUTLET`, `Outlet` and `outlet` are one season — but
    everything else is signal: `26AW` and `AW26` are left alone, because we
    cannot tell a typo from a convention we have not learned yet, and merging
    them would silently move products between slices.
    """
    counts: dict[str, dict[str, int]] = {}
    for raw in raw_values:
        value = (raw or "").strip()
        if not value:
            continue
        counts.setdefault(value.casefold(), {}).setdefault(value, 0)
        counts[value.casefold()][value] += 1
    # Ties keep the spelling seen first, which is what `max` over an
    # insertion-ordered mapping does. Arbitrary, but stable across runs — and
    # a season label that flickers between builds would move products between
    # slices for no reason a reader could see.
    return {
        folded: max(spellings, key=lambda spelling: spellings[spelling])
        for folded, spellings in counts.items()
    }


# ── Baselines ──────────────────────────────────────────────────────────────

#: How old a snapshot must be to serve as "last week". A week, so the number
#: answers the question the page asks rather than whichever run happened last.
BASELINE_MIN_AGE_DAYS = 7


def pick_baseline(
    dates: Sequence[date],
    today: date,
    min_age_days: int = BASELINE_MIN_AGE_DAYS,
) -> date | None:
    """The snapshot to compare against: the newest one at least a week old.

    Falling back to the oldest available when nothing is old enough means a
    young install shows *some* movement rather than nothing, and the page
    always prints the baseline's date beside the delta — a delta whose period
    is unstated is a delta nobody can check.
    """
    past = sorted(d for d in dates if d < today)
    if not past:
        return None
    cutoff = today.toordinal() - min_age_days
    old_enough = [d for d in past if d.toordinal() <= cutoff]
    return old_enough[-1] if old_enough else past[0]
