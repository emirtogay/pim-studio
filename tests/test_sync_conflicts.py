"""The rules that decide whether a SKU-grain disagreement needs a person.

Cases are taken from the first full sync's log rather than invented, so a
change in behaviour shows up against real data shapes.
"""

from pim_studio.sync.conflicts import CASE, TRUNCATION, WHITESPACE, normalise, resolve


def by(*pairs: tuple[str, int]) -> dict[str, list[str]]:
    """{value: [source, ...]} with `n` sources, the shape values.py builds."""
    return {v: [f"col@sku{i}" for i in range(n)] for v, n in pairs}


class TestNormalise:
    def test_collapses_escaped_and_literal_whitespace(self):
        assert normalise("a\nb   c") == normalise("a b\tc") == "a b c"

    def test_strips_outer_quotes_some_columns_arrive_with(self):
        assert normalise('"Northwind"') == "Northwind"

    def test_leaves_case_alone(self):
        assert normalise("Regular Fit") != normalise("REGULAR FIT")

    def test_removes_invisible_characters(self):
        assert normalise("a\u00a0b\u200b") == "a b"


class TestResolve:
    def test_a_single_candidate_is_not_a_conflict(self):
        assert resolve(by(("Regular Fit", 3))) is None

    def test_whitespace_only_difference_resolves(self):
        # The real NH7287IX.000 shape: same copy, one with escaped newlines.
        r = resolve(by(("<p>Freedom of movement.</p>", 1),
                       ("<p>Freedom of\nmovement.</p>", 1)))
        assert r is not None and r.rule == WHITESPACE
        assert r.value == "<p>Freedom of movement.</p>"

    def test_truncation_takes_the_longest(self):
        r = resolve(by(("Regular", 4), ("Regular Fit", 1)))
        assert r is not None and r.rule == TRUNCATION
        # The majority is the shorter one; length wins because a truncated
        # variant carries strictly less information.
        assert r.value == "Regular Fit"

    def test_case_only_takes_the_majority(self):
        r = resolve(by(("REGULAR FIT", 1), ("Regular Fit", 6)))
        assert r is not None and r.rule == CASE
        assert r.value == "Regular Fit"

    def test_case_tie_is_deterministic(self):
        a = resolve(by(("Regular Fit", 2), ("REGULAR FIT", 2)))
        b = resolve(by(("REGULAR FIT", 2), ("Regular Fit", 2)))
        assert a is not None and b is not None
        assert a.value == b.value, "a re-run must not churn attribute_values"

    def test_genuinely_different_names_abstain(self):
        # RA8432.FTJ in PL, five SKUs, three real names. A majority vote here
        # picks a 2-of-5 near-tie; one candidate is an English placeholder.
        r = resolve(by(("Skarpetki unisex", 2),
                       ("Długie skarpety unisex", 2),
                       ("Northwind  Socks", 1)))
        assert r is None

    def test_generic_majority_does_not_beat_a_real_description(self):
        # SH3855.HBM: eight SKUs carry the generic name, one the real copy.
        # Abstaining is the point — majority would enshrine the placeholder.
        r = resolve(by(("Northwind Unisex Sweatshirt", 8),
                       ("<p>An expert take on the essential hoodie.</p>", 1)))
        assert r is None

    def test_blank_candidates_are_not_a_resolution(self):
        assert resolve(by(("", 2), ("  ", 1))) is None


class TestSettle:
    """The import-anyway tiers (2026-08-18 reversal: flag, don't abstain)."""

    def test_cosmetic_still_wins_first(self):
        from pim_studio.sync.conflicts import WHITESPACE, settle
        r = settle(by(("a  b", 1), ("a b", 1)), market="PL")
        assert r is not None and r.rule == WHITESPACE

    def test_english_placeholder_is_disqualified_in_a_local_field(self):
        # The 5H5154.I90 case: 5 SKUs carry the Polish detail, 2 the English
        # placeholder. The English candidate is wrong-language content by the
        # same test that drives the EN? flags, so the local value wins.
        from pim_studio.sync.conflicts import LANGUAGE, settle
        r = settle(by(("3-pak bokserek z nadrukiem krokodyla", 5),
                      ("3-Pack Print Trunks", 2)), market="PL")
        assert r is not None and r.rule == LANGUAGE
        assert r.value == "3-pak bokserek z nadrukiem krokodyla"

    def test_language_rule_never_fires_market_agnostic(self):
        from pim_studio.sync.conflicts import MAJORITY, settle
        r = settle(by(("Northwind Socks", 5), ("Something Else", 2)), market="*")
        assert r is not None and r.rule == MAJORITY

    def test_two_local_values_fall_through_to_majority(self):
        # RA8432.FTJ: two real Polish names. No safe pick exists, so majority
        # writes one and the queue shows both for a person.
        from pim_studio.sync.conflicts import MAJORITY, settle
        r = settle(by(("Skarpetki unisex", 2),
                      ("Długie skarpety unisex", 2),
                      ("Northwind  Socks", 1)), market="PL")
        assert r is not None and r.rule == MAJORITY
        # Deterministic on the 2-2 tie: longer wins.
        assert r.value == "Długie skarpety unisex"

    def test_majority_is_deterministic_across_orderings(self):
        from pim_studio.sync.conflicts import settle
        a = settle(by(("Alpha", 2), ("Beta", 2)), market="*")
        b = settle(by(("Beta", 2), ("Alpha", 2)), market="*")
        assert a is not None and b is not None and a.value == b.value

    def test_all_blank_returns_none(self):
        from pim_studio.sync.conflicts import settle
        assert settle(by(("", 2), ("  ", 1)), market="PL") is None

    def test_turkish_is_disqualified_like_english(self):
        # 784RMA0950.2A9 in PL, 2026-09-16: seven sizes carried the Turkish
        # master under `pl`, six the Polish composition. The calibrated
        # detector has no Turkish and scored it 0.0 English — "local" — so the
        # vote crowned it in all four markets.
        from pim_studio.sync.conflicts import LANGUAGE, settle
        r = settle(by(("<p>Odyssa; gelecek için yeniden tasarlanmış arşivdir. Y2K silüeti. "
                       "Northwind spor ayakkabıda yüksek estetik ve yüksek teknoloji</p>", 7),
                      ("<li>CZĘŚĆ WIERZCHNIA: Tkanina 58% (Poliester z recyklingu 100%) "
                       "Materiał syntetyczny (zamsz) 42%</li>", 6)), market="PL")
        assert r is not None and r.rule == LANGUAGE
        assert r.value.startswith("<li>CZĘŚĆ WIERZCHNIA")

    def test_a_certainly_foreign_candidate_only_wins_a_vote_it_runs_alone_in(self):
        from pim_studio.sync.conflicts import MAJORITY, settle
        turkish = "Erkek Kısa Kollu Klasik Kesim Polo Yaka Tişört pamuklu kumaş"
        r = settle(by((turkish, 5),
                      ("Męska koszulka polo z krótkim rękawem z bawełny", 2),
                      ("Męska koszulka polo o klasycznym kroju z bawełny", 1)), market="PL")
        assert r is not None and r.rule == MAJORITY
        assert r.value == "Męska koszulka polo z krótkim rękawem z bawełny"
        # With nothing else on the ballot it still gets written, and flagged.
        english = "This shirt is made from soft cotton and it is designed for everyday comfort"
        r = settle(by((turkish, 3), (english, 2)), market="PL")
        assert r is not None and r.rule == MAJORITY and r.value == turkish
