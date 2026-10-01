"""The funnel rules, tested as pure functions.

`market_liveness` gets the most attention here. It moved out of the generator,
where it had no direct test despite encoding two findings that cost real
numbers: the per-SKU AND (F5 in `Findings/Audit 2026-08-20`) and the
unknown-versus-zero stock guard, which inflated the live count by ~350 a market
on its first attempt. Both are pinned below, so a future simplification of that
function fails a test rather than a quarter's reporting.
"""

from __future__ import annotations

from datetime import date

import pytest

from pim_studio.rules.coverage import SHARED, AttributeMeta, Owner, ProductFacts
from pim_studio.rules.funnel import (
    BASE_ATTRIBUTES,
    BLOCKER_ORDER,
    COMPUTABLE_STAGES,
    IMAGE_ATTRIBUTE,
    LOCALISED_ATTRIBUTES,
    STAGES,
    MarketVerdict,
    PipelineFacts,
    StageVerdict,
    cumulative,
    defect_english,
    defect_name_detail,
    fold_seasons,
    judge_localised,
    judge_shared,
    market_liveness,
    missing_label,
    pick_baseline,
    stage_checks,
)
from pim_studio.rules.liveness import Blocker

MARKETS = ("PL", "CZ", "SK", "HU")


def registry() -> dict[str, AttributeMeta]:
    reg = {
        key: AttributeMeta(key, key.replace("_", " ").title(), Owner.PARTNER, False)
        for key in BASE_ATTRIBUTES
    }
    reg[IMAGE_ATTRIBUTE] = AttributeMeta(IMAGE_ATTRIBUTE, "Image", Owner.US, False)
    for key in LOCALISED_ATTRIBUTES:
        reg[key] = AttributeMeta(key, key.title(), Owner.US, True)
    return reg


def facts(**over) -> ProductFacts:
    """A product with everything shared present, overridable per test."""
    filled = {key: [SHARED] for key in BASE_ATTRIBUTES}
    filled[IMAGE_ATTRIBUTE] = [SHARED]
    sourced = dict(filled)
    filled.update(over.pop("filled", {}))
    sourced.update(over.pop("sourced", {}))
    return ProductFacts(
        over.pop("model_color", "L3558.132"),
        sub_category=over.pop("sub_category", "Polo"),
        filled=filled,
        sourced=sourced,
    )


# ── Stage metadata ─────────────────────────────────────────────────────────


def test_display_order_groups_shared_stages_before_per_market_ones():
    per_market = [stage.per_market for stage in STAGES]
    # No shared stage may follow a per-market one, or a drop between them
    # would compare a per-market count against a whole-catalogue count.
    assert per_market == sorted(per_market)


def test_every_brief_stage_is_present_exactly_once():
    assert sorted(stage.brief_index for stage in STAGES) == list(range(1, 9))


def test_assortment_is_declared_uncomputable_rather_than_omitted():
    assortment = next(s for s in STAGES if s.key == "assortment")
    assert assortment.computable is False
    assert assortment not in COMPUTABLE_STAGES


def test_live_stage_has_no_owner_because_nobody_is_assigned_it():
    assert next(s for s in STAGES if s.key == "live").owner is None


# ── Shared and localised stage verdicts ────────────────────────────────────


def test_shared_stage_passes_when_every_attribute_is_filled():
    assert judge_shared(facts(), BASE_ATTRIBUTES, registry()) is StageVerdict.PASSED


def test_one_missing_attribute_fails_the_whole_stage():
    missing = facts(filled={"composition": []})
    assert judge_shared(missing, BASE_ATTRIBUTES, registry()) is StageVerdict.FAILED


def test_an_unread_attribute_is_unknown_not_a_gap():
    unread = facts(filled={"composition": []}, sourced={"composition": []})
    assert judge_shared(unread, BASE_ATTRIBUTES, registry()) is StageVerdict.UNKNOWN


def test_a_real_gap_outranks_an_unread_one():
    # Something is genuinely missing and something else was never read. The
    # stage is work, not a question mark.
    mixed = facts(filled={"gender": [], "composition": []}, sourced={"composition": []})
    assert judge_shared(mixed, BASE_ATTRIBUTES, registry()) is StageVerdict.FAILED


def test_an_attribute_the_registry_never_heard_of_abstains():
    assert judge_shared(facts(), ("no_such_attribute",), registry()) is StageVerdict.UNKNOWN


def test_not_applicable_attributes_are_skipped_rather_than_failed():
    reg = registry()
    reg["composition"] = AttributeMeta(
        "composition", "Composition", Owner.PARTNER, False, applies_when_subcategory="Bag"
    )
    polo = facts(filled={"composition": []})
    assert judge_shared(polo, BASE_ATTRIBUTES, reg) is StageVerdict.PASSED


def test_localised_judges_each_market_on_its_own():
    pf = facts(
        filled={"product_name": ["PL"], "description": ["PL"]},
        sourced={"product_name": ["PL", "CZ"], "description": ["PL", "CZ"]},
    )
    reg = registry()
    assert judge_localised(pf, "PL", reg) is StageVerdict.PASSED
    assert judge_localised(pf, "CZ", reg) is StageVerdict.FAILED
    # Slovak has no source at all — the never-read case the page must not
    # render as nobody having done any work.
    assert judge_localised(pf, "SK", reg) is StageVerdict.UNKNOWN


# ── market_liveness ────────────────────────────────────────────────────────


def liveness(**over) -> dict[str, MarketVerdict]:
    kwargs = dict(
        in_markets=frozenset(MARKETS),
        member={"T4": frozenset(MARKETS)},
        priced={"T4": frozenset(MARKETS)},
        stock={"T4": {m: 5 for m in MARKETS}},
        markets_priced=frozenset(MARKETS),
        markets_stocked=frozenset(MARKETS),
        banned=False,
        images_synced=True,
        has_image=True,
    )
    skus = over.pop("skus", ("T4",))
    kwargs.update(over)
    return market_liveness(MARKETS, skus, **kwargs)


def test_a_healthy_option_is_live_everywhere():
    assert all(v.live for v in liveness().values())


def test_a_banned_option_is_blocked_in_every_market():
    verdicts = liveness(banned=True)
    assert all(not v.live and v.blocker == Blocker.BANNED for v in verdicts.values())


def test_a_market_outside_the_catalogue_is_not_a_missing_price():
    verdicts = liveness(in_markets=frozenset({"PL", "CZ"}))
    assert verdicts["PL"].live
    assert verdicts["SK"].blocker == Blocker.NOT_IN_CATALOGUE


def test_rules_are_anded_per_sku_before_the_any_sku_rollup():
    """The F5 case: three sizes each satisfying a different rule.

    Rolling up per rule first would call this live — some SKU is a member,
    some SKU is priced, some SKU has stock. No single size can actually be
    bought, so it is not live.
    """
    verdicts = market_liveness(
        ("PL",),
        ("T4", "T6", "T8"),
        in_markets=frozenset({"PL"}),
        member={"T4": frozenset({"PL"}), "T6": frozenset({"PL"}), "T8": frozenset({"PL"})},
        priced={"T6": frozenset({"PL"})},
        stock={"T8": {"PL": 5}},
        markets_priced=frozenset({"PL"}),
        markets_stocked=frozenset({"PL"}),
        banned=False,
        images_synced=True,
        has_image=True,
    )
    assert not verdicts["PL"].live


def test_one_good_sku_makes_the_option_live():
    verdicts = market_liveness(
        ("PL",),
        ("T4", "T6"),
        in_markets=frozenset({"PL"}),
        member={"T4": frozenset({"PL"}), "T6": frozenset({"PL"})},
        priced={"T6": frozenset({"PL"})},
        stock={"T6": {"PL": 3}},
        markets_priced=frozenset({"PL"}),
        markets_stocked=frozenset({"PL"}),
        banned=False,
        images_synced=True,
        has_image=True,
    )
    assert verdicts["PL"].live


def test_a_stock_source_never_read_abstains_rather_than_reading_as_zero():
    # No stock rows and no stock sync for SK. The rule must not invent an
    # out-of-stock; this is the ~350-per-market inflation guard.
    verdicts = liveness(stock={}, markets_stocked=frozenset({"PL"}))
    assert verdicts["SK"].live
    assert not verdicts["PL"].live
    assert verdicts["PL"].blocker == Blocker.OUT_OF_STOCK


def test_absent_stock_inside_a_read_market_means_zero():
    verdicts = liveness(stock={}, markets_stocked=frozenset(MARKETS))
    assert verdicts["PL"].blocker == Blocker.OUT_OF_STOCK


def test_an_unpriced_market_that_was_read_fails_on_price():
    verdicts = liveness(priced={})
    assert verdicts["PL"].blocker == Blocker.NO_PRICE


def test_the_blocker_reported_is_the_binding_constraint_not_the_deepest():
    """Two sizes fail differently; the one that got furthest is the one to fix.

    T4 has no price at all, T6 is priced and stocked but the option has no
    image. Naming the price problem would point at a size nobody can buy while
    T6 sits one image away from selling.
    """
    verdicts = market_liveness(
        ("PL",),
        ("T4", "T6"),
        in_markets=frozenset({"PL"}),
        member={"T4": frozenset({"PL"}), "T6": frozenset({"PL"})},
        priced={"T6": frozenset({"PL"})},
        stock={"T6": {"PL": 3}},
        markets_priced=frozenset({"PL"}),
        markets_stocked=frozenset({"PL"}),
        banned=False,
        images_synced=True,
        has_image=False,
    )
    assert verdicts["PL"].blocker == Blocker.NO_IMAGE
    assert BLOCKER_ORDER[Blocker.NO_IMAGE] > BLOCKER_ORDER[Blocker.NO_PRICE]


def test_a_member_market_with_no_member_skus_falls_back_to_catalogue():
    verdicts = liveness(member={})
    assert verdicts["PL"].blocker == Blocker.NOT_IN_CATALOGUE


# ── The cumulative fold ────────────────────────────────────────────────────


def test_a_failure_taints_every_later_stage():
    statuses, first = cumulative(
        [StageVerdict.PASSED, StageVerdict.FAILED, StageVerdict.PASSED]
    )
    assert first == 1
    assert statuses == ["passed", "failed", "failed"]


def test_an_unknown_taints_later_stages_without_stopping_the_walk():
    statuses, first = cumulative(
        [StageVerdict.PASSED, StageVerdict.UNKNOWN, StageVerdict.PASSED]
    )
    assert first is None
    assert statuses == ["passed", "indeterminate", "indeterminate"]


def test_a_failure_after_an_unknown_is_still_the_queue_it_belongs_in():
    """Unread Slovak description, then no price. The price queue is actionable;
    the unknown is not, so it must not swallow the failure."""
    _, first = cumulative(
        [StageVerdict.PASSED, StageVerdict.UNKNOWN, StageVerdict.FAILED]
    )
    assert first == 2


def test_a_clean_product_passes_every_stage():
    statuses, first = cumulative([StageVerdict.PASSED] * 4)
    assert first is None
    assert set(statuses) == {"passed"}


def test_population_is_conserved_across_the_funnel():
    """The invariant the page rests on: everything is either through, waiting
    on a source, or sitting in exactly one queue."""
    reg = registry()
    products = [
        # Live everywhere.
        PipelineFacts(
            "A",
            facts(
                filled={k: list(MARKETS) for k in LOCALISED_ATTRIBUTES},
                sourced={k: list(MARKETS) for k in LOCALISED_ATTRIBUTES},
            ),
            in_markets=frozenset(MARKETS),
            priced_markets=frozenset(MARKETS),
            stocked_markets=frozenset(MARKETS),
            markets_priced=frozenset(MARKETS),
            markets_stocked=frozenset(MARKETS),
            live={m: MarketVerdict(True) for m in MARKETS},
        ),
        # Fails base — the partner queue.
        PipelineFacts(
            "B",
            facts(
                filled={"composition": [], **{k: list(MARKETS) for k in LOCALISED_ATTRIBUTES}},
                sourced={k: list(MARKETS) for k in LOCALISED_ATTRIBUTES},
            ),
            in_markets=frozenset(MARKETS),
            priced_markets=frozenset(MARKETS),
            stocked_markets=frozenset(MARKETS),
            markets_priced=frozenset(MARKETS),
            markets_stocked=frozenset(MARKETS),
            live={m: MarketVerdict(True) for m in MARKETS},
        ),
        # Description never read anywhere — indeterminate, not a gap.
        PipelineFacts(
            "C",
            facts(
                filled={"product_name": list(MARKETS)},
                sourced={"product_name": list(MARKETS), "description": []},
            ),
            in_markets=frozenset(MARKETS),
            priced_markets=frozenset(MARKETS),
            stocked_markets=frozenset(MARKETS),
            markets_priced=frozenset(MARKETS),
            markets_stocked=frozenset(MARKETS),
            live={m: MarketVerdict(True) for m in MARKETS},
        ),
    ]

    for market in MARKETS:
        passed = indeterminate = 0
        drops: dict[int, int] = {}
        for pf in products:
            statuses, first = cumulative(stage_checks(pf, reg, MARKETS)[market])
            if first is not None:
                drops[first] = drops.get(first, 0) + 1
            elif statuses[-1] == "indeterminate":
                indeterminate += 1
            else:
                passed += 1
        assert passed + indeterminate + sum(drops.values()) == len(products)


def test_stage_checks_stops_all_four_ladders_on_a_shared_failure():
    pf = PipelineFacts(
        "D",
        facts(filled={"gender": []}),
        in_markets=frozenset(MARKETS),
        priced_markets=frozenset(MARKETS),
        stocked_markets=frozenset(MARKETS),
        markets_priced=frozenset(MARKETS),
        markets_stocked=frozenset(MARKETS),
        live={m: MarketVerdict(True) for m in MARKETS},
    )
    checks = stage_checks(pf, registry(), MARKETS)
    base_index = [s.key for s in COMPUTABLE_STAGES].index("base")
    assert {cumulative(row)[1] for row in checks.values()} == {base_index}


def test_a_market_outside_the_catalogue_drops_at_priced_not_at_live():
    """Membership is a filter, not a verdict — and the price queue must stay
    actionable, so a non-member surfaces before the live stage."""
    pf = PipelineFacts(
        "E",
        facts(
            filled={k: list(MARKETS) for k in LOCALISED_ATTRIBUTES},
            sourced={k: list(MARKETS) for k in LOCALISED_ATTRIBUTES},
        ),
        in_markets=frozenset({"PL"}),
        priced_markets=frozenset({"PL"}),
        stocked_markets=frozenset({"PL"}),
        markets_priced=frozenset(MARKETS),
        markets_stocked=frozenset(MARKETS),
        live={"PL": MarketVerdict(True)}
        | {m: MarketVerdict(False, Blocker.NOT_IN_CATALOGUE) for m in ("CZ", "SK", "HU")},
    )
    keys = [s.key for s in COMPUTABLE_STAGES]
    checks = stage_checks(pf, registry(), MARKETS)
    assert cumulative(checks["CZ"])[1] == keys.index("priced")


# ── Boundary strings ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("market", "field_name", "expected"),
    [
        ("PL", "price", "PL price"),
        ("HU", "stock", "HU stock"),
        ("CZ", "catalogue", "CZ catalogue"),
    ],
)
def test_missing_labels_match_the_facet_strings_products_filters_on(
    market, field_name, expected
):
    assert missing_label(market, field_name) == expected


def test_defect_strings_match_the_generator():
    assert defect_english("PL", "name") == "PL name reads as English"
    assert defect_name_detail("HU") == "HU name ≠ detail"


# ── Seasons and baselines ──────────────────────────────────────────────────


def test_season_spellings_fold_on_case_and_keep_the_majority():
    assert fold_seasons(["Outlet", "OUTLET", "OUTLET"])["outlet"] == "OUTLET"


def test_seasons_that_differ_by_more_than_case_are_left_alone():
    folded = fold_seasons(["26AW", "AW26"])
    assert set(folded.values()) == {"26AW", "AW26"}


def test_blank_seasons_are_not_a_season():
    assert fold_seasons(["", "   ", None]) == {}


def test_the_baseline_is_the_newest_snapshot_at_least_a_week_old():
    today = date(2026, 8, 25)
    dates = [date(2026, 8, 1), date(2026, 8, 17), date(2026, 8, 24)]
    assert pick_baseline(dates, today) == date(2026, 8, 17)


def test_exactly_a_week_old_counts():
    today = date(2026, 8, 25)
    assert pick_baseline([date(2026, 8, 18)], today) == date(2026, 8, 18)


def test_a_young_install_falls_back_to_the_oldest_it_has():
    today = date(2026, 8, 25)
    assert pick_baseline([date(2026, 8, 23), date(2026, 8, 24)], today) == date(2026, 8, 23)


def test_no_history_and_today_only_have_no_baseline():
    today = date(2026, 8, 25)
    assert pick_baseline([], today) is None
    assert pick_baseline([today], today) is None
