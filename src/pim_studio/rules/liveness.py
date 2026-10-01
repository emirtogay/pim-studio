"""Liveness — the six conditions for a product to show on the website.

Implements `Rules/Liveness`. A product is live when all six hold: price filled,
stock above the buffer, image uploaded, category assigned, in the catalog, and
not banned.

Four of the six are checkable from what we hold. Category assignment and
catalogue membership are not exposed by any export we have — we can see a
category *name* in a column but not whether it is assigned in Omnitron — so a
pass on our four is necessary and not sufficient, and this module refuses to
guess at the other two rather than returning a confident wrong answer.

Two properties of this function matter more than they look:

**Order is the contract, not an implementation detail.** The reason shown to a
user is the *most fundamental* failing condition, not the first one some loop
happened to notice. Banned outranks everything; a banned product's missing
image is not the thing to go fix.

**A rule can only fail on data we hold.** No source row for a market means its
price and stock are unknown, and unknown is not zero. Asserting otherwise
invented ~1,460 false "out of stock" verdicts on the first pass through the
real data. That is why the input carries `has_price_source` and
`has_image_source` separately from the values themselves — the caller has to
say whether it looked, and a rule never infers absence from an empty field.

Grain: this is per-SKU and per-market and is never rolled up. `Rules/Liveness`
is explicit that there is no such thing as "this ModelColor is live" —
completeness and coverage report at ModelColor level, liveness at size level,
and the two answer different questions.
"""

from __future__ import annotations

from dataclasses import dataclass


class Blocker:
    """The reason strings, defined once.

    These cross a language boundary — the generator writes them into JSON and
    the frontend reads them back to decide which readiness bucket a product
    falls in. Matching prose across that boundary has already gone wrong once:
    `NOT_OPENED` is in the frontend's `readinessOf` but was never emitted by
    the generator, so the entire "not opened" bucket was dead on real data and
    every non-live product read as "blocked". See
    `Findings/Audit 2026-08-14`. Anything added here needs adding on the
    TypeScript side too, and the pair belongs in one generated file eventually.
    """

    BANNED = "Banned"
    NO_PRICE = "No price"
    OUT_OF_STOCK = "Out of stock"
    NO_IMAGE = "No image"
    #: Rule 5. Not returned by `first_failure` — that function only judges what
    #: an export can show, and catalogue membership is not in one. It is named
    #: here because a caller that *can* check it (the API sync can) needs the
    #: same string the frontend already keys on, rather than inventing a second
    #: spelling. The mismatch between these constants and the strings the
    #: TypeScript side tests for has bitten once already; see `NOT_OPENED`.
    NOT_IN_CATALOGUE = "Not in catalogue"
    #: Our four checkable rules pass but the control table says otherwise, so
    #: the failure is in one of the two we cannot see.
    NOT_IN_CONTROL_TABLE = "Not live per control table (category or catalogue)"
    #: The product does not exist in Omnitron at all. Nothing can be uploaded
    #: against it, which routes the work to an entirely different person than
    #: any of the blockers above — which is the whole reason the distinction
    #: exists. Not currently emitted by the demo generator.
    NOT_OPENED = "Not opened in Omnitron"


# Stock must be **greater than 0** — on the stock list with at least one unit.
#
# This was 1 (a buffer of one unit) from the first implementation until
# 2026-08-20, carried as to-be-confirmed because nobody knew where the buffer
# came from. It came from us. Three independent checks against the live tenant,
# recorded in `Findings/Control table reconciliation`:
#
#   * Akinon's own definition of sellable is price list + stock list + category
#     node. On the stock list — not "with more than one unit".
#   * The tenant defines no safety stock. `/api/v1/stock_list/` carries only
#     pk/name/code/is_auto_sync, and `stock_list_rule`, `stock_provider` and
#     `stock_location` all 404.
#   * `product_stock` has no `reserved_stock`, and every `salable_stock_query`
#     endpoint 404s here — so the theory that the buffer stood in for
#     reservations had no data behind it either.
#
# And the measurement. Scoring both thresholds against the control table's own
# set of live ModelColors:
#
#     market   control     T=1     T=0    match T=1   match T=0
#     PL         4,116   3,542   4,179       84.8%       99.6%
#     CZ         4,111   3,658   4,225       86.5%       99.6%
#     SK         4,109   3,590   4,142       86.6%       99.5%
#     HU         4,192   3,659   4,275       85.5%       99.5%
#
# If a buffer against overselling the last unit is wanted, it is a real policy
# and belongs *beside* liveness as its own measure. Folding it into the
# definition of live is what made this panel disagree with Looker by ~500
# products a market, and sent an investigation after category assignment.
STOCK_THRESHOLD = 0


@dataclass(frozen=True, slots=True)
class LivenessInput:
    """The facts one (SKU, market) needs, already resolved from their sources.

    Joining sources is the caller's job; deciding is this module's. That split
    is what lets the demo generator, the importer and the tests share one
    definition of live while reading entirely different files.
    """

    banned: bool
    #: Whether we hold any record that would carry price and stock for this
    #: market. False means unknown, and unknown never fails a rule.
    has_price_source: bool
    #: Empty string when absent. Price is per-market.
    price: str
    #: None when unknown. Note this is distinct from 0, which is a real
    #: out-of-stock — and since 2026-08-20 that is the only value that fails,
    #: so the two are no longer separated by a buffer and the distinction
    #: matters more, not less: unknown must still never read as zero.
    stock: int | None
    #: Whether we hold the product-level record that carries the image.
    has_image_source: bool
    has_image: bool


def first_failure(facts: LivenessInput) -> str:
    """The most fundamental liveness rule this market fails, or "" if none we
    can check do.

    Returning "" means "nothing we can see is wrong", not "live" — rules 4 and
    5 are unchecked. Callers that have a source for those two decide among
    candidates this function allows; they must not treat "" as a pass.
    """
    if facts.banned:
        return Blocker.BANNED

    if facts.has_price_source:
        if not facts.price:
            return Blocker.NO_PRICE
        if facts.stock is not None and facts.stock <= STOCK_THRESHOLD:
            return Blocker.OUT_OF_STOCK

    if facts.has_image_source and not facts.has_image:
        return Blocker.NO_IMAGE

    return ""
