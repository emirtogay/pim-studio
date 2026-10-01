"""Identifier normalisation — the ModelColor rollup and the blank spellings."""

from __future__ import annotations

import pytest

from pim_studio.rules import model_color, normalise_blank


@pytest.mark.parametrize(
    ("article", "expected"),
    [
        # The three shapes seen in the catalogue: apparel, watches, footwear.
        ("TH2568.144.T04", "TH2568.144"),
        ("3196.130.T00", "3196.130"),
        ("60KFA0791.83J.T42", "60KFA0791.83J"),
        # Already at ModelColor grain — idempotent, which matters because the
        # sources disagree about which grain they ship.
        ("TH2568.144", "TH2568.144"),
        # Lowercase input, uppercase output. ModelColor is uppercase in the PIM
        # and the matcher joins by exact string.
        ("th2568.144.t04", "TH2568.144"),
        # Alphabetic sizes.
        ("PH9619.001.TXL", "PH9619.001"),
        ("PH9619.001.T2XL", "PH9619.001"),
        ("", ""),
    ],
)
def test_model_color(article: str, expected: str) -> None:
    assert model_color(article) == expected


def test_only_the_trailing_size_suffix_is_stripped() -> None:
    """The pattern is anchored to the end. A `.T`-looking fragment earlier in
    the code is part of the identifier, not a size."""
    assert model_color("TSM1YS.T33.T04") == "TSM1YS.T33"


@pytest.mark.parametrize("value", ["", "   ", None, "None", "null", "0", "0.0"])
def test_the_export_spellings_of_empty(value: object) -> None:
    """The Omnitron exports use a numeric zero as the empty cell for text
    columns, so `0` and `0.0` are blanks here. Measured, and a per-source
    quirk rather than a universal truth."""
    assert normalise_blank(value) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  Polo  ", "Polo"),
        ("349.00", "349.00"),
        (349, "349"),
        ("00", "00"),  # not one of the blank spellings
        ("0.00", "0.00"),
    ],
)
def test_values_survive(value: object, expected: str) -> None:
    assert normalise_blank(value) == expected


def test_zero_and_unpopulated_are_indistinguishable_after_normalising() -> None:
    """Pinned deliberately, because it is a live trap rather than a feature.

    A caller that needs "known to be zero" apart from "never reported" cannot
    get it from this function and must look at whether the source row existed.
    See Findings/Audit 2026-08-14, finding 10.
    """
    assert normalise_blank("0") == normalise_blank("")


# ── Footwear: the leading 7 ────────────────────────────────────────────────
#
# Our shoe codes carry a leading `7` that the brand library does not. Drop it
# and you have the code the way Bynder stores it. This is the
# fifth of the five identifier conventions and the only one that is a rule
# rather than a spelling — normalisation cannot absorb it.
#
# It gets more tests than its size suggests because of how it fails. A shoe
# searched under the wrong name is not an error: it is recorded as an option
# the brand has never photographed, and somebody emails the brand about it.


def test_a_shoe_code_loses_its_leading_seven() -> None:
    from pim_studio.rules.identifiers import shoe_alias

    assert shoe_alias("774RMA3517.255") == "74RMA3517.255"
    assert shoe_alias("714KMA1408.02H") == "14KMA1408.02H"
    assert shoe_alias("718RMA3943.148") == "18RMA3943.148"


def test_a_numeric_product_id_starting_with_seven_is_left_alone() -> None:
    """The reason the pattern is narrow rather than "strip a leading 7".

    `7006103.433` is a real product code and is not a shoe. A looser rule would
    silently rewrite it to `006103.433`, which matches nothing — turning a
    product that has photos into one that appears not to.
    """
    from pim_studio.rules.identifiers import shoe_alias

    assert shoe_alias("7006103.433") is None
    assert shoe_alias("7006103_433") is None


def test_a_code_already_in_library_form_has_no_alias() -> None:
    """Applying the rule twice must not eat another character."""
    from pim_studio.rules.identifiers import shoe_alias

    assert shoe_alias("74RMA3517.255") is None


def test_ordinary_apparel_codes_are_untouched() -> None:
    from pim_studio.rules.identifiers import shoe_alias

    for code in ("TH2568.144", "L3558.132", "AF0276.J2G", "2011169.-"):
        assert shoe_alias(code) is None, code


def test_variants_try_what_we_hold_before_the_alias() -> None:
    """As written first, always. An alias is a fallback for a convention we
    know upstream uses, not a replacement for the code we actually hold."""
    from pim_studio.rules.identifiers import code_variants

    assert code_variants("774RMA3517.255") == ["774RMA3517.255", "74RMA3517.255"]
    assert code_variants("L3558.132") == ["L3558.132"]


def test_variants_never_returns_an_empty_list() -> None:
    """Callers iterate it; an empty list would silently search for nothing."""
    from pim_studio.rules.identifiers import code_variants

    assert code_variants("") == [""]
    assert len(code_variants("anything")) >= 1
