"""Coverage — how much of what a product needs is actually there.

Liveness answers "can this size be sold in this market today". Coverage answers
a different question, at a different grain, for different people: *for this set
of options and this definition of complete, what is missing, how much of it,
and whose desk does each piece land on.*

The two must not be conflated. A product can be live and still be missing its
Hungarian description; a product can be complete on every attribute we own and
still be dark because merch banned it. `Rules/Liveness` is explicit that there
is no such thing as "this ModelColor is live" — liveness is per SKU per market.
Coverage is per ModelColor, which is the grain the reports are read at.

## Four verdicts, not two

The temptation is `filled` / `not filled`. That is the mistake the README calls
out as having produced four rounds of confident, plausible, wrong numbers, so
this module makes it unrepresentable:

    met       we hold a value
    missing   we hold a source that would carry it, and it is empty
    unknown   we hold no source that would carry it — we never looked
    na        the attribute does not apply to this product at all

`unknown` never counts as a gap, and it never counts as met either. It is
reported in its own column, because "0 of 1,200 have a Slovak description" and
"we have never read a file containing Slovak descriptions" look identical in
any two-state model and mean entirely different things.

`na` is not a gap either. A wallet has no sleeve type, and putting one in
somebody's queue to go and fill in is how a report loses its reader.

## Requirements are chosen, not fixed

What counts as complete depends on the question. "Ready to launch in Poland" and
"the product card is filled in" are different sets of attributes, and next
season there will be a third. So a `Profile` is data — a named set of
`Requirement`s, each an attribute plus the markets it is wanted in — and the
presets below are starting points, not the definition of the word.

## Every gap names its remedy

A count nobody can act on is trivia. Each gap carries a `Remedy` derived from
the registry's `write_channel`, because who may write an attribute is exactly
who has to fix it being absent: values we upload, values only the Turkey team
can set, and values that arrive from a feed and are nobody's to type in.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

#: Stand-in market for an attribute that has one value across all markets.
#: Same sentinel `models.MARKET_AGNOSTIC` uses, and for the same reason: a
#: nullable market column makes every join and every group-by a special case.
SHARED = "*"

MARKETS: tuple[str, ...] = ("PL", "CZ", "SK", "HU")


class Verdict(Enum):
    """Where one (product, attribute, market) stands.

    Four rather than two. See the module docstring — collapsing `unknown` into
    either of the first two is the specific failure this module exists to make
    impossible.
    """

    MET = "met"
    MISSING = "missing"
    #: No source read that could carry this value. Not a gap, not a pass.
    UNKNOWN = "unknown"
    #: Does not apply to this product's sub-category. Not work.
    NA = "na"

    @property
    def counts_as_gap(self) -> bool:
        return self is Verdict.MISSING


class Owner(Enum):
    """Who can act. Derived from the registry's `write_channel`, never guessed."""

    US = "us"
    PARTNER = "partner"
    SYSTEM = "system"


#: `write_channel` → who fixes it. The registry already answers the question
#: "who may write this", and a gap is only ever fixed by someone who may write.
OWNER_BY_CHANNEL: Mapping[str, Owner] = {
    "omnitron_import": Owner.US,
    "product_card": Owner.PARTNER,
    "read_only": Owner.SYSTEM,
}


@dataclass(frozen=True, slots=True)
class Remedy:
    """What to actually do about a gap, and through which channel.

    The channel is the deliverable: a gap fixed by an Omnitron import file and
    a gap fixed by a request to Turkey are the same shape on a dashboard and
    entirely different amounts of work, and only one of them is ours to
    schedule.
    """

    owner: Owner
    #: The artefact that carries the fix. Named for the thing produced, so a
    #: count of gaps converts directly into a count of files to generate.
    channel: str
    action: str


REMEDIES: Mapping[Owner, Remedy] = {
    Owner.US: Remedy(
        Owner.US,
        "omnitron_import",
        "Ours. Goes out on the next Omnitron import file.",
    ),
    Owner.PARTNER: Remedy(
        Owner.PARTNER,
        "product_card",
        "Turkey's to write. Request it on the next product-card file; we cannot set it.",
    ),
    Owner.SYSTEM: Remedy(
        Owner.SYSTEM,
        "feed",
        "Arrives from a feed. Nothing to upload — chase the source, or accept the absence.",
    ),
}


# ── The provenance caveat ──────────────────────────────────────────────────

#: Stock is in the readiness score, and stock is not yet trustworthy.
#:
#: The ERP is the system of record for stock and is not connected. What we hold
#: comes from the Omnitron sync, which is a mirror of what Omnitron was told —
#: so a discrepancy between the two is invisible from here by construction.
#: Every surface that shows a stock-derived number carries this sentence, and
#: `CoverageSummary.caveats` is what puts it in the report as well as on the
#: page: a caveat that only exists in the UI is a caveat that does not survive
#: being emailed to someone.
STOCK_CAVEAT = (
    "Stock comes from the Omnitron sync and has not been reconciled against "
    "the ERP, which is the system of record. Stock-derived counts here — out of "
    "stock, and anything that depends on it — are provisional."
)

#: Attribute keys whose values are stock-derived, so a caller can tell whether
#: `STOCK_CAVEAT` applies to a given profile rather than always printing it.
STOCK_BACKED: frozenset[str] = frozenset({"stock"})


# ── Requirements and profiles ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Requirement:
    """One attribute, wanted in these markets.

    `markets` is empty for a market-agnostic attribute — the evaluation then
    runs once against `SHARED` rather than four times against a value that
    cannot differ. Passing markets for an attribute the registry says is not
    per-market is a caller error and is normalised, not honoured: it would
    otherwise report the same single value as four separate gaps and multiply
    the size of the work by four.
    """

    attribute: str
    markets: tuple[str, ...] = ()

    def scopes(self, per_market: bool) -> tuple[str, ...]:
        if not per_market:
            return (SHARED,)
        return self.markets or MARKETS


@dataclass(frozen=True, slots=True)
class Profile:
    """A named answer to "what does complete mean here".

    Data rather than code because the question changes: a season opening, a
    translation push and a filter cleanup want three different sets, and the
    presets below are only the ones worth having on day one.
    """

    key: str
    label: str
    blurb: str
    requirements: tuple[Requirement, ...]

    def attribute_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(r.attribute for r in self.requirements))


def _all_markets(*attributes: str) -> tuple[Requirement, ...]:
    return tuple(Requirement(a, MARKETS) for a in attributes)


def _shared(*attributes: str) -> tuple[Requirement, ...]:
    return tuple(Requirement(a) for a in attributes)


#: The seven attributes the storefront's product filters are built on — our
#: keys for the upstream set named in `reports/registry.FILTER_ATTRIBUTES`.
#: A gap in any of them is a filter that silently drops the product.
FILTER_ATTRIBUTES: tuple[str, ...] = (
    "collar_type",
    "fit",
    "sleeve_type",
    "pattern",
    "fabric_type",
    "length",
    "waist",
)

PRESETS: tuple[Profile, ...] = (
    Profile(
        "sellable",
        "Sellable",
        "The bare minimum for a product to appear and be bought: a name, an "
        "image, a price and a category.",
        (
            *_all_markets("product_name", "price"),
            *_shared("image_0", "sub_category"),
        ),
    ),
    Profile(
        "localised",
        "Localised everywhere",
        "Name and description present in all four market languages. The "
        "translation backlog, as a number.",
        _all_markets("product_name", "description"),
    ),
    Profile(
        "product_card",
        "Product card complete",
        "Everything the Turkey team fills in from the card we send them. "
        "Entirely partner-owned, so the whole number is a chase, not a task.",
        _shared(
            "main_category",
            "sub_category",
            "gender",
            "composition",
            "color_detail",
            "color_filter",
            *FILTER_ATTRIBUTES,
        ),
    ),
    Profile(
        "filters",
        "Filters usable",
        "The seven attributes the storefront filters are built on. A product "
        "missing one is invisible to anyone browsing by it.",
        _shared(*FILTER_ATTRIBUTES),
    ),
    Profile(
        "pl_launch",
        "PL launch ready",
        "Poland only, end to end — content, image, price and classification.",
        (
            Requirement("product_name", ("PL",)),
            Requirement("description", ("PL",)),
            Requirement("price", ("PL",)),
            *_shared("image_0", "sub_category", "gender", "pl_sezon"),
        ),
    ),
)

PRESETS_BY_KEY: Mapping[str, Profile] = {p.key: p for p in PRESETS}


# ── Evaluation ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AttributeMeta:
    """The registry facts one attribute contributes to a coverage verdict.

    A subset of the `Attribute` row, as plain values, so this module stays
    callable from the generator, the report and a test with no database.
    """

    key: str
    label: str
    owner: Owner
    per_market: bool
    #: Comma list of sub-categories the attribute applies to; empty means all.
    applies_when_subcategory: str = ""
    group: str = "Other"

    def applies_to(self, sub_category: str | None) -> bool:
        """Whether this attribute is a real requirement for that sub-category.

        An unknown sub-category applies everything rather than nothing: we
        cannot show that an attribute is inapplicable, and calling a gap `na`
        on that basis would quietly delete work from the queue. Over-reporting
        here is visible and correctable; under-reporting is neither.
        """
        allowed = [
            s.strip().casefold()
            for s in (self.applies_when_subcategory or "").split(",")
            if s.strip()
        ]
        if not allowed:
            return True
        if not sub_category:
            return True
        return sub_category.strip().casefold() in allowed


@dataclass(frozen=True, slots=True)
class ProductFacts:
    """What we hold for one ModelColor, as presence rather than values.

    Coverage never needs the value itself, only whether there is one — which
    is what lets the extract behind the dashboard be a fraction of the size of
    the one behind the product pages.
    """

    model_color: str
    sub_category: str | None = None
    #: attribute key → the markets (or `SHARED`) carrying a non-empty value.
    filled: Mapping[str, Collection[str]] = field(default_factory=dict)
    #: attribute key → the markets we have actually read a source for. An
    #: attribute absent from this mapping is `unknown` everywhere, which is
    #: the whole point: a column missing from an export says nothing.
    sourced: Mapping[str, Collection[str]] = field(default_factory=dict)

    def verdict(self, meta: AttributeMeta, market: str) -> Verdict:
        if not meta.applies_to(self.sub_category):
            return Verdict.NA
        if market in self.filled.get(meta.key, ()):
            return Verdict.MET
        if market in self.sourced.get(meta.key, ()):
            return Verdict.MISSING
        return Verdict.UNKNOWN


@dataclass(frozen=True, slots=True)
class Gap:
    """One missing value, with its remedy already attached."""

    model_color: str
    attribute: str
    label: str
    market: str
    owner: Owner

    @property
    def remedy(self) -> Remedy:
        return REMEDIES[self.owner]

    @property
    def scope(self) -> str:
        """How the gap reads on a page — `ALL` for market-agnostic."""
        return "ALL" if self.market == SHARED else self.market


class Standing(Enum):
    """One product's overall standing against a profile.

    Three, not two, for the same reason there are four verdicts: a product we
    cannot judge is not a product that passed.
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    #: Nothing missing, but at least one requirement we never had a source for.
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ProductCoverage:
    model_color: str
    standing: Standing
    met: int
    missing: int
    unknown: int
    na: int
    gaps: tuple[Gap, ...]

    @property
    def required(self) -> int:
        """Requirements that are real work for this product — `na` excluded."""
        return self.met + self.missing + self.unknown

    @property
    def score(self) -> float | None:
        """Share of judgeable requirements that are met, or None if none are.

        `unknown` is excluded from both halves rather than counted as a
        failure: a product we have never read a Slovak file for should not
        score worse than one we read and found empty. They are different
        problems and the second is the only one anybody can fix.
        """
        judgeable = self.met + self.missing
        return self.met / judgeable if judgeable else None


def evaluate_product(
    facts: ProductFacts,
    profile: Profile,
    registry: Mapping[str, AttributeMeta],
) -> ProductCoverage:
    """One ModelColor against one profile.

    Requirements naming an attribute the registry does not hold are skipped
    rather than counted as missing — an unknown key is a bug in the profile,
    not a gap in the product, and reporting it as the latter would send someone
    looking for a field that does not exist.
    """
    met = missing = unknown = na = 0
    gaps: list[Gap] = []

    for requirement in profile.requirements:
        meta = registry.get(requirement.attribute)
        if meta is None:
            continue
        for market in requirement.scopes(meta.per_market):
            verdict = facts.verdict(meta, market)
            if verdict is Verdict.MET:
                met += 1
            elif verdict is Verdict.NA:
                na += 1
            elif verdict is Verdict.UNKNOWN:
                unknown += 1
            else:
                missing += 1
                gaps.append(
                    Gap(
                        model_color=facts.model_color,
                        attribute=meta.key,
                        label=meta.label,
                        market=market,
                        owner=meta.owner,
                    )
                )

    if missing:
        standing = Standing.INCOMPLETE
    elif unknown:
        standing = Standing.INDETERMINATE
    else:
        standing = Standing.COMPLETE

    return ProductCoverage(
        model_color=facts.model_color,
        standing=standing,
        met=met,
        missing=missing,
        unknown=unknown,
        na=na,
        gaps=tuple(gaps),
    )


# ── Rollups ────────────────────────────────────────────────────────────────


@dataclass
class RequirementTally:
    """One row of the "what is wrong, and with how many" table.

    The denominator is `met + missing`, never the population: products the
    attribute does not apply to are not part of the question, and products we
    have no source for cannot answer it.
    """

    attribute: str
    label: str
    market: str
    owner: Owner
    met: int = 0
    missing: int = 0
    unknown: int = 0
    na: int = 0

    @property
    def judgeable(self) -> int:
        return self.met + self.missing

    @property
    def fill_rate(self) -> float | None:
        return self.met / self.judgeable if self.judgeable else None

    @property
    def blind(self) -> bool:
        """Every option came back `na`, so the requirement asked nothing.

        Distinct from an unread column, and they must not print alike: one
        means nobody has loaded the data, the other means the requirement
        matched no product — usually because the sub-category names it is
        conditioned on do not exist in that spelling. Both show a blank fill
        rate and they send you to entirely different places.
        """
        return bool(self.na) and not (self.met + self.missing + self.unknown)

    @property
    def remedy(self) -> Remedy:
        return REMEDIES[self.owner]


@dataclass
class CoverageSummary:
    """Everything the dashboard and the workbook both need, computed once."""

    profile: Profile
    population: int
    complete: int = 0
    incomplete: int = 0
    indeterminate: int = 0
    #: Per (attribute, market), most-missing first once sorted.
    requirements: list[RequirementTally] = field(default_factory=list)
    #: Total gaps by who has to fix them — the "what can we do" split.
    gaps_by_owner: dict[Owner, int] = field(default_factory=dict)
    #: Products carrying at least one gap owned by each party. Different from
    #: the above and the more useful of the two for planning: 900 gaps across
    #: 40 products is one afternoon, across 900 products it is a season.
    products_by_owner: dict[Owner, int] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    @property
    def judged(self) -> int:
        """Products we could reach a verdict on at all."""
        return self.complete + self.incomplete

    @property
    def completion_rate(self) -> float | None:
        return self.complete / self.judged if self.judged else None


def summarise(
    coverages: Iterable[ProductCoverage],
    profile: Profile,
    registry: Mapping[str, AttributeMeta],
) -> CoverageSummary:
    """Roll per-product verdicts up into the numbers a report prints.

    Takes evaluated coverages rather than raw facts so a caller can filter the
    population first — the scoped analysis the dashboard is built around —
    without this function needing to know what a scope is.
    """
    summary = CoverageSummary(profile=profile, population=0)

    tallies: dict[tuple[str, str], RequirementTally] = {}
    for requirement in profile.requirements:
        meta = registry.get(requirement.attribute)
        if meta is None:
            continue
        for market in requirement.scopes(meta.per_market):
            tallies[(meta.key, market)] = RequirementTally(
                attribute=meta.key, label=meta.label, market=market, owner=meta.owner
            )

    gap_owner_counts: dict[Owner, int] = {o: 0 for o in Owner}
    product_owner_counts: dict[Owner, int] = {o: 0 for o in Owner}

    for coverage in coverages:
        summary.population += 1
        if coverage.standing is Standing.COMPLETE:
            summary.complete += 1
        elif coverage.standing is Standing.INCOMPLETE:
            summary.incomplete += 1
        else:
            summary.indeterminate += 1

        owners_here: set[Owner] = set()
        for gap in coverage.gaps:
            gap_owner_counts[gap.owner] += 1
            owners_here.add(gap.owner)
        for owner in owners_here:
            product_owner_counts[owner] += 1

    summary.gaps_by_owner = gap_owner_counts
    summary.products_by_owner = product_owner_counts
    summary.requirements = list(tallies.values())
    if any(r.attribute in STOCK_BACKED for r in profile.requirements):
        summary.caveats.append(STOCK_CAVEAT)
    return summary


def tally_requirements(
    facts: Iterable[ProductFacts],
    profile: Profile,
    registry: Mapping[str, AttributeMeta],
) -> list[RequirementTally]:
    """Per-requirement counts across a population.

    Separate from `summarise` because it needs the facts rather than the
    per-product rollup: a `ProductCoverage` records how many requirements were
    unknown, not which ones, and "which" is the entire content of this table.
    """
    tallies: dict[tuple[str, str], RequirementTally] = {}
    for requirement in profile.requirements:
        meta = registry.get(requirement.attribute)
        if meta is None:
            continue
        for market in requirement.scopes(meta.per_market):
            tallies.setdefault(
                (meta.key, market),
                RequirementTally(
                    attribute=meta.key, label=meta.label, market=market, owner=meta.owner
                ),
            )

    for product in facts:
        for requirement in profile.requirements:
            meta = registry.get(requirement.attribute)
            if meta is None:
                continue
            for market in requirement.scopes(meta.per_market):
                tally = tallies[(meta.key, market)]
                verdict = product.verdict(meta, market)
                if verdict is Verdict.MET:
                    tally.met += 1
                elif verdict is Verdict.MISSING:
                    tally.missing += 1
                elif verdict is Verdict.UNKNOWN:
                    tally.unknown += 1
                else:
                    tally.na += 1

    return sorted(
        tallies.values(),
        key=lambda t: (-t.missing, -t.unknown, t.attribute, t.market),
    )


# ── The standing issue flags ───────────────────────────────────────────────
#
# These are not profile requirements. They are the five things the coverage
# report has always led with, they are asked about the whole catalogue rather
# than a chosen attribute set, and each has a remedy that has nothing to do
# with an import file — so they are their own vocabulary rather than four more
# rows in the requirements table.


@dataclass(frozen=True, slots=True)
class Issue:
    key: str
    label: str
    #: What it means, in the words the report prints.
    definition: str
    remedy: str
    #: True when the count rests on the unreconciled Omnitron stock read.
    stock_backed: bool = False


ISSUES: tuple[Issue, ...] = (
    Issue(
        "no_price",
        "No price",
        "No SKU of the option carries a price in any market we read prices for.",
        "Price arrives with the price list, not from us. Check the option is on "
        "the list for that market before treating it as a content gap.",
    ),
    Issue(
        "no_image",
        "No image",
        "The option has no image in Omnitron.",
        "Ours. The Images page finds it in the brand library, publishes it, "
        "and builds the bundle Turkey uploads.",
    ),
    Issue(
        "removed",
        "Removed / banned",
        "PL_comment classifies the option as removed by merch.",
        "Deliberate — no fix. Worth checking only when the comment looks like a "
        "misclassification; the ban table is editable.",
    ),
    Issue(
        "no_stock",
        "Out of stock",
        "No SKU has stock above zero in any market we read stock for.",
        "Nothing to upload. Provisional until the ERP is connected.",
        stock_backed=True,
    ),
    Issue(
        "not_in_catalogue",
        "Not in any catalogue",
        "The option belongs to none of the four market catalogues.",
        "Ours. Add it to the market catalogue in Omnitron; nothing else can go "
        "live until it is there.",
    ),
    Issue(
        "unclassified_comment",
        "Comment not classified",
        "PL_comment holds a value on neither the ban list nor the allow list, "
        "so whether the option is banned is unknown.",
        "One decision each, in Admin. Until then these options are counted as "
        "not banned, which may be wrong in either direction.",
    ),
)

ISSUES_BY_KEY: Mapping[str, Issue] = {i.key: i for i in ISSUES}


def bucket(flags: Collection[str]) -> str:
    """The one mutually-exclusive bucket an option falls into.

    Every option lands in exactly one, so the buckets sum to the population and
    a reader can check the arithmetic. Overlapping percentages that add to 180%
    are the reason this exists alongside the flag counts rather than instead of
    them.
    """
    ordered = [i.key for i in ISSUES if i.key in set(flags)]
    if not ordered:
        return "clean"
    if len(ordered) == 1:
        return f"only:{ordered[0]}"
    return "+".join(ordered)
