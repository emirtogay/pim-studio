"""Rules/Liveness.

Two properties carry most of the weight: the order of the checks, because the
reason shown decides who goes and fixes it, and the refusal to fail a rule on
data we do not hold, because that is the mistake this codebase has made three
times.
"""

from __future__ import annotations

from pim_studio.rules import Blocker, LivenessInput, first_failure
from pim_studio.rules.liveness import STOCK_THRESHOLD


def facts(**overrides) -> LivenessInput:
    """A product that passes every rule we can check, minus the overrides."""
    base = {
        "banned": False,
        "has_price_source": True,
        "price": "349.00",
        "stock": 12,
        "has_image_source": True,
        "has_image": True,
    }
    return LivenessInput(**{**base, **overrides})


def test_all_checkable_rules_passing_returns_empty() -> None:
    assert first_failure(facts()) == ""


def test_empty_means_nothing_visible_is_wrong_not_live() -> None:
    """Rules 4 and 5 — category assigned, in the catalogue — are not checkable
    from any export we hold. A caller must not read "" as a pass."""
    assert first_failure(facts()) == ""  # and the docstring says why


# ── order ──────────────────────────────────────────────────────────────────


def test_banned_outranks_every_other_failure() -> None:
    """A banned product's missing image is not the thing to go fix."""
    everything_wrong = facts(
        banned=True, price="", stock=0, has_image=False,
    )
    assert first_failure(everything_wrong) == Blocker.BANNED


def test_price_outranks_stock_and_image() -> None:
    assert first_failure(facts(price="", stock=0, has_image=False)) == Blocker.NO_PRICE


def test_stock_outranks_image() -> None:
    assert first_failure(facts(stock=0, has_image=False)) == Blocker.OUT_OF_STOCK


# ── unknown is not zero ────────────────────────────────────────────────────


def test_no_price_source_cannot_fail_the_price_rule() -> None:
    """No source row for this market means price is unknown. Reporting "No
    price" would be a confident wrong answer — it produced ~1,460 false
    verdicts the first time."""
    assert first_failure(facts(has_price_source=False, price="", stock=None)) == ""


def test_no_price_source_cannot_fail_the_stock_rule_either() -> None:
    assert first_failure(facts(has_price_source=False, price="", stock=0)) == ""


def test_unknown_stock_with_a_price_source_does_not_fail() -> None:
    """We hold a row, it carries a price, but stock was never populated.
    Unknown stock abstains; only a known zero fails."""
    assert first_failure(facts(stock=None)) == ""


def test_no_image_source_cannot_fail_the_image_rule() -> None:
    """A ModelColor in the control table but absent from the Check_list export
    has no PL record, so its image is unknown rather than missing. Claiming
    otherwise reported ~3,900 phantom gaps."""
    assert first_failure(facts(has_image_source=False, has_image=False)) == ""


# ── thresholds ─────────────────────────────────────────────────────────────


def test_zero_stock_fails() -> None:
    """Zero is the only stock value that blocks a market.

    The rule carried a buffer of one unit until 2026-08-20 — `> 1` rather than
    `> 0` — on the assumption that Omnitron modelled one. It does not: no
    safety stock on the stock list, no `reserved_stock` on `product_stock`, and
    Akinon's own definition of sellable says "on the stock list" with no
    quantity attached. See `Findings/Control table reconciliation`.
    """
    assert first_failure(facts(stock=0)) == Blocker.OUT_OF_STOCK


def test_a_single_unit_is_sellable() -> None:
    """The regression this whole reconciliation was about.

    One unit in stock is live. Treating it as out of stock hid ~500
    ModelColors per market — 96–97% of everything the panel disagreed with the
    control table about.
    """
    assert first_failure(facts(stock=1)) == ""


def test_the_threshold_is_the_only_thing_that_defines_the_boundary() -> None:
    """Guards against the constant and the comparison drifting apart."""
    assert first_failure(facts(stock=STOCK_THRESHOLD)) == Blocker.OUT_OF_STOCK
    assert first_failure(facts(stock=STOCK_THRESHOLD + 1)) == ""


def test_negative_stock_fails_rather_than_wrapping() -> None:
    assert first_failure(facts(stock=-3)) == Blocker.OUT_OF_STOCK


def test_empty_price_string_is_the_absent_price() -> None:
    assert first_failure(facts(price="")) == Blocker.NO_PRICE


def test_zero_price_is_treated_as_absent() -> None:
    """`normalise_blank` maps the export's numeric-zero empty cell to "", so a
    zero price never reaches here as "0". Pinned because a future source that
    means a real zero would need the normaliser changed, not this."""
    assert first_failure(facts(price="")) == Blocker.NO_PRICE
