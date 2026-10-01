"""The coverage rules.

Most of these test one property: that `unknown` stays separate from `missing`
at every level of the rollup. That distinction is the module's reason to exist
and it is the one a future refactor is most likely to quietly collapse, since
every intermediate structure would still typecheck without it.
"""

from __future__ import annotations

import pytest

from pim_studio.rules.coverage import (
    ISSUES,
    PRESETS,
    PRESETS_BY_KEY,
    SHARED,
    STOCK_CAVEAT,
    AttributeMeta,
    Owner,
    ProductFacts,
    Profile,
    Requirement,
    Standing,
    Verdict,
    bucket,
    evaluate_product,
    summarise,
    tally_requirements,
)

REGISTRY = {
    "product_name": AttributeMeta(
        "product_name", "Product name", Owner.US, per_market=True
    ),
    "description": AttributeMeta("description", "Description", Owner.US, per_market=True),
    "image_0": AttributeMeta("image_0", "First image", Owner.SYSTEM, per_market=False),
    "sleeve_type": AttributeMeta(
        "sleeve_type",
        "Sleeve type",
        Owner.PARTNER,
        per_market=False,
        applies_when_subcategory="Polo,T-shirt,Shirt",
    ),
}


def facts(**kwargs) -> ProductFacts:
    kwargs.setdefault("model_color", "L3558.132")
    return ProductFacts(**kwargs)


# ── verdicts ───────────────────────────────────────────────────────────────


def test_a_value_we_hold_is_met():
    p = facts(filled={"product_name": ["PL"]}, sourced={"product_name": ["PL"]})
    assert p.verdict(REGISTRY["product_name"], "PL") is Verdict.MET


def test_empty_with_a_source_is_missing():
    p = facts(filled={}, sourced={"product_name": ["PL", "CZ"]})
    assert p.verdict(REGISTRY["product_name"], "CZ") is Verdict.MISSING


def test_empty_with_no_source_is_unknown_not_missing():
    """The whole point. A column absent from an export says nothing about the
    value, and calling it missing is the mistake the README says produced four
    rounds of wrong numbers."""
    p = facts(filled={"product_name": ["PL"]}, sourced={"product_name": ["PL"]})
    assert p.verdict(REGISTRY["product_name"], "SK") is Verdict.UNKNOWN


def test_inapplicable_attribute_is_na_even_when_sourced_and_empty():
    """A wallet has no sleeve type. Having read the column does not make its
    emptiness work for anybody."""
    p = facts(sub_category="Wallet", sourced={"sleeve_type": [SHARED]})
    assert p.verdict(REGISTRY["sleeve_type"], SHARED) is Verdict.NA


def test_applicable_sub_category_is_judged_normally():
    p = facts(sub_category="Polo", sourced={"sleeve_type": [SHARED]})
    assert p.verdict(REGISTRY["sleeve_type"], SHARED) is Verdict.MISSING


def test_unknown_sub_category_applies_everything():
    """Over-report rather than under-report: an invisible gap is worse than a
    visible one somebody can dismiss."""
    p = facts(sub_category=None, sourced={"sleeve_type": [SHARED]})
    assert p.verdict(REGISTRY["sleeve_type"], SHARED) is Verdict.MISSING


def test_sub_category_matching_ignores_case_and_padding():
    p = facts(sub_category="  polo ", sourced={"sleeve_type": [SHARED]})
    assert p.verdict(REGISTRY["sleeve_type"], SHARED) is Verdict.MISSING


# ── requirements ───────────────────────────────────────────────────────────


def test_market_agnostic_attribute_is_evaluated_once():
    """Passing markets for a shared attribute must not multiply one value into
    four gaps — that inflates the size of the work fourfold."""
    requirement = Requirement("image_0", ("PL", "CZ", "SK", "HU"))
    assert requirement.scopes(per_market=False) == (SHARED,)


def test_per_market_requirement_defaults_to_all_four():
    assert Requirement("product_name").scopes(per_market=True) == ("PL", "CZ", "SK", "HU")


# ── per-product rollup ─────────────────────────────────────────────────────


def test_complete_when_every_requirement_is_met():
    profile = Profile("t", "t", "", (Requirement("product_name", ("PL",)),))
    p = facts(filled={"product_name": ["PL"]}, sourced={"product_name": ["PL"]})
    result = evaluate_product(p, profile, REGISTRY)
    assert result.standing is Standing.COMPLETE
    assert result.gaps == ()


def test_indeterminate_when_nothing_is_missing_but_something_is_unknown():
    """Not complete. Claiming a pass on a requirement we never had a source for
    is exactly the confident-and-wrong number this module is built to avoid."""
    profile = Profile("t", "t", "", (Requirement("product_name", ("PL", "SK")),))
    p = facts(filled={"product_name": ["PL"]}, sourced={"product_name": ["PL"]})
    result = evaluate_product(p, profile, REGISTRY)
    assert result.standing is Standing.INDETERMINATE
    assert (result.met, result.missing, result.unknown) == (1, 0, 1)


def test_missing_outranks_unknown_for_standing():
    profile = Profile(
        "t", "t", "", (Requirement("product_name", ("PL", "CZ", "SK")),)
    )
    p = facts(filled={}, sourced={"product_name": ["PL"]})
    result = evaluate_product(p, profile, REGISTRY)
    assert result.standing is Standing.INCOMPLETE
    assert (result.missing, result.unknown) == (1, 2)


def test_na_is_excluded_from_the_denominator():
    profile = Profile("t", "t", "", (Requirement("sleeve_type"),))
    result = evaluate_product(facts(sub_category="Wallet"), profile, REGISTRY)
    assert result.na == 1
    assert result.required == 0
    assert result.standing is Standing.COMPLETE


def test_score_ignores_unknown_on_both_sides():
    """One met, one missing, two never looked at: 50%, not 25%."""
    profile = Profile(
        "t",
        "t",
        "",
        (Requirement("product_name"), Requirement("description", ("PL",))),
    )
    p = facts(
        filled={"product_name": ["PL"]},
        sourced={"product_name": ["PL", "CZ"], "description": []},
    )
    result = evaluate_product(p, profile, REGISTRY)
    assert result.score == pytest.approx(0.5)


def test_score_is_none_when_nothing_is_judgeable():
    profile = Profile("t", "t", "", (Requirement("product_name", ("PL",)),))
    assert evaluate_product(facts(), profile, REGISTRY).score is None


def test_unknown_attribute_key_is_skipped_not_counted_missing():
    """A typo in a profile is a bug in the profile. Reporting it as a product
    gap sends somebody looking for a field that does not exist."""
    profile = Profile("t", "t", "", (Requirement("no_such_attribute"),))
    result = evaluate_product(facts(), profile, REGISTRY)
    assert (result.met, result.missing, result.unknown, result.na) == (0, 0, 0, 0)


def test_gap_carries_its_owner_and_remedy():
    profile = Profile("t", "t", "", (Requirement("sleeve_type"),))
    result = evaluate_product(
        facts(sub_category="Polo", sourced={"sleeve_type": [SHARED]}), profile, REGISTRY
    )
    (gap,) = result.gaps
    assert gap.owner is Owner.PARTNER
    assert gap.remedy.channel == "product_card"
    assert gap.scope == "ALL"


def test_per_market_gap_scope_is_the_market():
    profile = Profile("t", "t", "", (Requirement("product_name", ("HU",)),))
    result = evaluate_product(
        facts(sourced={"product_name": ["HU"]}), profile, REGISTRY
    )
    assert result.gaps[0].scope == "HU"


# ── population rollups ─────────────────────────────────────────────────────


def _population() -> list[ProductFacts]:
    return [
        # complete
        ProductFacts(
            "A",
            "Polo",
            filled={"product_name": ["PL"], "sleeve_type": [SHARED]},
            sourced={"product_name": ["PL"], "sleeve_type": [SHARED]},
        ),
        # missing the partner-owned attribute
        ProductFacts(
            "B",
            "Polo",
            filled={"product_name": ["PL"]},
            sourced={"product_name": ["PL"], "sleeve_type": [SHARED]},
        ),
        # missing our own attribute, and n/a on the partner one
        ProductFacts(
            "C",
            "Wallet",
            filled={},
            sourced={"product_name": ["PL"], "sleeve_type": [SHARED]},
        ),
        # never read for either
        ProductFacts("D", "Polo", filled={}, sourced={}),
    ]


PROFILE = Profile(
    "t",
    "t",
    "",
    (Requirement("product_name", ("PL",)), Requirement("sleeve_type")),
)


def test_summary_splits_the_three_standings():
    summary = summarise(
        (evaluate_product(p, PROFILE, REGISTRY) for p in _population()),
        PROFILE,
        REGISTRY,
    )
    assert summary.population == 4
    assert (summary.complete, summary.incomplete, summary.indeterminate) == (1, 2, 1)


def test_completion_rate_excludes_the_indeterminate_product():
    """1 complete of 3 judged, not of 4 — the fourth was never readable."""
    summary = summarise(
        (evaluate_product(p, PROFILE, REGISTRY) for p in _population()),
        PROFILE,
        REGISTRY,
    )
    assert summary.judged == 3
    assert summary.completion_rate == pytest.approx(1 / 3)


def test_gaps_by_owner_counts_gaps_and_products_separately():
    """900 gaps across 40 products is an afternoon; across 900 it is a season.
    Both numbers are needed and they are not interchangeable."""
    summary = summarise(
        (evaluate_product(p, PROFILE, REGISTRY) for p in _population()),
        PROFILE,
        REGISTRY,
    )
    # B misses sleeve_type; C misses product_name (sleeve_type is n/a there).
    assert summary.gaps_by_owner[Owner.PARTNER] == 1
    assert summary.gaps_by_owner[Owner.US] == 1
    assert summary.products_by_owner[Owner.PARTNER] == 1
    assert summary.products_by_owner[Owner.US] == 1


def test_stock_caveat_appears_only_for_stock_backed_profiles():
    registry = {**REGISTRY, "stock": AttributeMeta("stock", "Stock", Owner.SYSTEM, False)}
    without = summarise([], PROFILE, registry)
    assert without.caveats == []

    stocked = Profile("s", "s", "", (Requirement("stock"),))
    assert STOCK_CAVEAT in summarise([], stocked, registry).caveats


def test_requirement_tally_reports_four_verdicts_per_row():
    tallies = tally_requirements(_population(), PROFILE, REGISTRY)
    rows = {(t.attribute, t.market): t for t in tallies}

    name = rows[("product_name", "PL")]
    assert (name.met, name.missing, name.unknown, name.na) == (2, 1, 1, 0)
    assert name.fill_rate == pytest.approx(2 / 3)

    sleeve = rows[("sleeve_type", SHARED)]
    assert (sleeve.met, sleeve.missing, sleeve.unknown, sleeve.na) == (1, 1, 1, 1)


def test_requirement_tally_sorts_worst_first():
    rows = tally_requirements(_population(), PROFILE, REGISTRY)
    assert [r.missing for r in rows] == sorted((r.missing for r in rows), reverse=True)


def test_fill_rate_is_none_rather_than_zero_when_nothing_is_judgeable():
    """0% and "we have never looked" must not render the same."""
    (row,) = tally_requirements(
        [ProductFacts("A", "Polo")],
        Profile("t", "t", "", (Requirement("product_name", ("PL",)),)),
        REGISTRY,
    )
    assert row.judgeable == 0
    assert row.fill_rate is None


# ── issue buckets ──────────────────────────────────────────────────────────


def test_no_flags_is_clean():
    assert bucket([]) == "clean"


def test_one_flag_is_an_only_bucket():
    assert bucket(["no_image"]) == "only:no_image"


def test_bucket_order_is_stable_regardless_of_input_order():
    """The bucket is a key that gets grouped on; two spellings of the same
    combination would split one row into two."""
    assert bucket(["no_image", "no_price"]) == bucket(["no_price", "no_image"])


def test_buckets_partition_the_population():
    flag_sets = [[], ["no_price"], ["no_price", "no_image"], ["no_image"]]
    assert len({bucket(f) for f in flag_sets}) == 4


# ── presets ────────────────────────────────────────────────────────────────


def test_every_preset_has_a_unique_key():
    keys = [p.key for p in PRESETS]
    assert len(keys) == len(set(keys)) == len(PRESETS_BY_KEY)


def test_no_preset_requires_an_attribute_the_seed_does_not_define():
    """A preset naming a key the registry lacks silently evaluates to nothing,
    so it would read as "everything complete" rather than as broken."""
    from pim_studio.seed import ATTRIBUTES

    seeded = {row[0] for row in ATTRIBUTES}
    for preset in PRESETS:
        unknown = set(preset.attribute_keys()) - seeded
        assert not unknown, f"{preset.key} requires unseeded {sorted(unknown)}"


def test_every_issue_has_a_remedy_sentence():
    for issue in ISSUES:
        assert issue.remedy.strip()
        assert issue.definition.strip()
