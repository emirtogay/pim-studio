"""Rules/Language detection.

Deliberately few cases: each one loads the detector and the point is the
policy, not the model's accuracy. The examples are the ones the vault note
cites as having been got wrong before.
"""

from __future__ import annotations

import pytest

from pim_studio.rules import FLAG_THRESHOLD, english_confidence, reads_as_english

# Not re-exported from `pim_studio.rules` on purpose: it is diagnostic-only,
# and promoting it to the curated surface would invite exactly the misuse its
# docstring warns about.
from pim_studio.rules.language import detect_language


@pytest.mark.parametrize(
    "text",
    [
        "Men's Short Sleeve Polo",
        "Classic Fit Long Sleeved N.10.10 Polo Shirt",
        "Men's V-Neck Pima Cotton T-shirt",
    ],
)
def test_english_in_a_local_field_is_detected(text: str) -> None:
    """The defect we are actually hunting. The last two carry one English
    stopword each and were missed by the previous two-stopword check."""
    assert english_confidence(text) > 0.0


@pytest.mark.parametrize(
    "text",
    [
        "Bílé pánské tenisky",  # Czech; came back Slovak in fast mode
        "Koszulka Polo N.10.10 Classic Fit",  # Polish; came back Hungarian
    ],
)
def test_cee_languages_are_never_reported_as_english(text: str) -> None:
    """Only the English verdict is acted on. A value reading as the wrong *CEE*
    language is left alone — those calls are frequently wrong, because Czech
    and Slovak are close and product names are short and dominated by brand
    tokens."""
    assert english_confidence(text) == 0.0
    assert reads_as_english(text) is False


@pytest.mark.parametrize("text", ["", "  ", None, "XL", "—", "L12"])
def test_too_short_to_judge_returns_zero(text: str | None) -> None:
    """Below four characters there is no language to detect, only noise."""
    assert english_confidence(text) == 0.0


def test_confidence_is_carried_not_hidden() -> None:
    """The inspector words itself from this number ("strongly reads as
    English"), and band filtering is a query over it. It must be a number, not
    a boolean and not prose."""
    confidence = english_confidence("Men's Short Sleeve Polo")
    assert isinstance(confidence, float)
    assert 0.0 < confidence <= 1.0


def test_flagging_applies_the_threshold_confidence_does_not() -> None:
    """The two are separate on purpose: the confidence is a measurement that
    belongs on the value, the flag is the editorial decision about which
    measurements are worth showing someone."""
    text = "Men's Short Sleeve Polo"
    assert reads_as_english(text) is (english_confidence(text) >= FLAG_THRESHOLD)
    # A threshold above any possible confidence flags nothing, however
    # confidently English the value reads.
    assert reads_as_english(text, threshold=1.01) is False
    # And one at the floor flags anything with a non-zero reading.
    assert reads_as_english(text, threshold=0.0) is True


def test_default_threshold_is_the_top_band() -> None:
    """Rules/Language detection: 874 flags at >=0.90, 1,600 between 0.70 and
    0.89, 2,181 below. The note says start at the top band."""
    assert FLAG_THRESHOLD == 0.90


def test_the_raw_verdict_is_available_for_diagnosis():
    """`detect_language` reports what `english_confidence` refuses to.

    The refusal is deliberate everywhere else — an intra-CEE verdict acted on
    is a wrong flag. But a Hungarian description sitting under a Polish key is
    exactly that shape, and nothing else in this module can see it.
    """
    polish = "Koszulka polo z bawelny pique z krotkim rekawem i haftowanym logo"
    language, confidence = detect_language(polish)
    assert language == "POLISH"
    assert confidence > 0.5
    # The flagging path stays silent about it, as designed.
    assert english_confidence(polish) == 0.0


def test_a_value_too_short_to_judge_says_so_rather_than_guessing():
    assert detect_language("") == ("", 0.0)
    assert detect_language(None) == ("", 0.0)


def test_a_name_of_brand_tokens_returns_a_junk_verdict_with_low_confidence():
    """Why this is diagnostic-only, in one assertion.

    "Polo" clears the minimum length and gets a confident-looking answer that
    is worthless — there is no language in it to judge. The caller is a human
    reading the confidence beside the text, which is the only reader that can
    tell the difference.
    """
    language, confidence = detect_language("Polo")
    assert language  # it will answer
    assert confidence < 0.7  # and the number says not to believe it


# ── foreign_language: the two languages a market field is certainly not in ──


def test_english_and_turkish_are_named_and_the_market_languages_are_not() -> None:
    from pim_studio.rules import foreign_language

    english = "Classic Fit Long Sleeved Polo Shirt in cotton fine pique fabric"
    assert foreign_language(english) == "ENGLISH"
    assert foreign_language("Erkek Kısa Kollu Klasik Kesim Polo Yaka Tişört") == "TURKISH"
    # A market's own language is not foreign, and neither is a value that is
    # merely one CEE language sitting in another's field — this module has
    # never trusted those calls and this function does not start.
    assert foreign_language("Koszulka polo z krótkim rękawem dla mężczyzn") is None
    assert foreign_language("Pánská polokošile s krátkým rukávem") is None


def test_an_unsure_or_too_short_value_is_not_called_foreign() -> None:
    """`None` has to mean 'leave it alone': the caller uses this to choose
    between two stored values, so a guess would overwrite good text."""
    from pim_studio.rules import foreign_language

    assert foreign_language(None) is None
    assert foreign_language("") is None
    assert foreign_language("XL") is None
    assert foreign_language("N.10.10") is None
    # Confident English, but not confident enough for a threshold above it.
    assert foreign_language("Men's Classic Fit Polo Shirt", threshold=1.01) is None


def test_turkish_is_invisible_to_the_calibrated_detector_and_named_by_this_one() -> None:
    """The gap this function exists to close, pinned.

    The calibrated detector knows five languages and no Turkish, so Turkish
    text is forced into one of them — measured here, it calls this Turkish
    product name *Polish* at 0.89, and `english_confidence` reports 0.0
    because the verdict was not English. Nothing downstream could see it.
    Turkish stays out of that detector on purpose (widening it would move
    every `EN?` number at once), so the new question gets its own.
    """
    from pim_studio.rules import english_confidence, foreign_language
    from pim_studio.rules.language import _detector

    turkish = "Erkek Kısa Kollu Klasik Kesim Polo Yaka Tişört"
    assert _detector().detect_language_of(turkish).name in (
        "POLISH", "CZECH", "SLOVAK", "HUNGARIAN",
    )
    assert english_confidence(turkish) == 0.0
    assert foreign_language(turkish) == "TURKISH"
