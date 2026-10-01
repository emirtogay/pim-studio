"""Wrong-language content detection.

Implements `Rules/Language detection`. A market field holding text in the wrong
language is filled and defective — every not-null check passes it — so it has
to be detected rather than counted.

Three decisions from that note are implemented here and each one was measured,
not assumed:

High-accuracy mode is not optional. In fast mode `Bílé pánské tenisky` (Czech)
came back Slovak and `Koszulka Polo N.10.10 Classic Fit` (Polish) came back
Hungarian. Switching modes fixed six of seven sampled false positives. It costs
9,400 values/second instead of 20,000, which is nothing across 45,043 values.

Only the English verdict is acted on. English-in-a-local-field was right on
inspection (4,655 of them); the ~1,600 intra-CEE calls were frequently wrong,
because Czech and Slovak are close and product names are short and dominated by
brand tokens — `Polo`, `Classic Fit`, `N.10.10` — leaving little language to
judge. A value reading as the wrong *CEE* language is left alone.

Confidence is carried, not hidden. The detector is run once at ingest and the
number travels with the flag, so nothing downstream re-implements detection —
which is what happened before, in Python and TypeScript, kept in step by hand.

Per `Rules/Liveness` this warns and never blocks. These products are selling.
"""

from __future__ import annotations

from functools import lru_cache

# Flag at or above this confidence. Of the field-level flags measured across
# the real exports: 874 at >=0.90, 1,600 between 0.70 and 0.89, 2,181 below
# 0.70. The note says to start at the top band, and a threshold is how that
# instruction survives contact with a caller that just wants a boolean.
FLAG_THRESHOLD = 0.90

# Below this many characters there is nothing to judge and the detector's
# output is noise. `XL`, `—`, a bare colour code.
_MIN_LENGTH = 4


@lru_cache(maxsize=1)
def _detector():
    """Built once, lazily.

    Lazily because constructing it loads five language models and takes long
    enough to be felt on import; cached because rebuilding per call would make
    a 45,000-value pass unusable.
    """
    from lingua import Language, LanguageDetectorBuilder

    return (
        LanguageDetectorBuilder.from_languages(
            Language.POLISH,
            Language.CZECH,
            Language.SLOVAK,
            Language.HUNGARIAN,
            Language.ENGLISH,
        )
        # Constrained to the five languages that can legitimately appear.
        # Narrowing the candidate set is what makes short product names
        # tractable at all.
        .build()
    )


@lru_cache(maxsize=1)
def _wide_detector():
    """The five above plus Turkish, for `foreign_language` alone.

    Deliberately a second detector rather than Turkish added to the one above.
    Widening the shared detector would change every `english_confidence` number
    in the system at once — the `EN?` flag, the conflict resolver's language
    tier, the Overview counts — because adding a candidate redistributes the
    confidences. Some of that change would be an improvement (Turkish text is
    currently forced into one of five buckets and sometimes lands on English),
    but it is a measurement change and it deserves its own before-and-after
    rather than arriving as a side effect of a reader fix. So the calibrated
    detector keeps its five languages and its meaning, and the question "is
    this text in a language no CEE market field should ever hold" gets its own.
    """
    from lingua import Language, LanguageDetectorBuilder

    return (
        LanguageDetectorBuilder.from_languages(
            Language.POLISH,
            Language.CZECH,
            Language.SLOVAK,
            Language.HUNGARIAN,
            Language.ENGLISH,
            Language.TURKISH,
        )
        .build()
    )


def foreign_language(text: str | None, threshold: float = FLAG_THRESHOLD) -> str | None:
    """`"ENGLISH"` or `"TURKISH"` when the value is in neither market language.

    The two languages a PL/CZ/SK/HU content field can hold and be certainly
    wrong: English, which is the source copy nobody translated, and Turkish,
    which is the tenant's own default and what an unlocalised read returns.
    Both are typologically distant from all four markets, so unlike the
    intra-CEE verdicts this module refuses to report, these are trustworthy on
    short product text.

    `None` means "not certainly foreign" — including a value too short to
    judge, and including one this returns no confident verdict for. Callers
    use it to choose between two candidate values, so an unsure answer has to
    mean "leave it alone", never "replace it".
    """
    value = (text or "").strip()
    if len(value) < _MIN_LENGTH:
        return None
    detector = _wide_detector()
    verdict = detector.detect_language_of(value)
    if verdict is None or verdict.name not in ("ENGLISH", "TURKISH"):
        return None
    confidences = detector.compute_language_confidence_values(value)
    if not confidences or confidences[0].value < threshold:
        return None
    return verdict.name


def english_confidence(text: str | None) -> float:
    """How confident we are this value is English; 0.0 when it isn't.

    Returns 0.0 rather than the winning language's confidence when the verdict
    is some other language — the intra-CEE verdicts are not trusted, so
    reporting their confidence would invite a caller to act on them.
    """
    value = (text or "").strip()
    if len(value) < _MIN_LENGTH:
        return 0.0
    detector = _detector()
    verdict = detector.detect_language_of(value)
    if verdict is None or verdict.name != "ENGLISH":
        return 0.0
    confidences = detector.compute_language_confidence_values(value)
    return round(confidences[0].value, 2) if confidences else 0.0


def reads_as_english(text: str | None, threshold: float = FLAG_THRESHOLD) -> bool:
    """Whether the value should be *flagged* as English-in-a-local-field.

    Distinct from `english_confidence` on purpose. The confidence is a
    measurement and belongs on the value; this is the editorial decision about
    which measurements are worth showing someone, and it has a threshold
    because flagging 800 products incorrectly costs more trust than the work it
    saves.
    """
    return english_confidence(text) >= threshold


def detect_language(text: str | None) -> tuple[str, float]:
    """The raw verdict and its confidence. **Diagnostic only — never flag on it.**

    Everything else in this module refuses to report an intra-CEE verdict,
    because those calls were measured to be frequently wrong on short product
    names and a caller handed one will act on it. This returns the verdict
    anyway, for the one job that needs it: a human looking at a specific value
    and asking what language is actually sitting there.

    That question is not answerable any other way. Hungarian text filed under a
    Polish key is filled, in a real CEE language, and consistent with itself —
    `english_confidence` returns 0.0 for it and always will. Only a raw verdict
    beside the text, read by someone who can tell, settles it. See
    `scripts/probe_locale.py`, which is the caller this exists for.

    Returns `("", 0.0)` when the value is too short to judge, which is common:
    a product name is often nothing but brand tokens.
    """
    value = (text or "").strip()
    if len(value) < _MIN_LENGTH:
        return "", 0.0
    detector = _detector()
    verdict = detector.detect_language_of(value)
    if verdict is None:
        return "", 0.0
    confidences = detector.compute_language_confidence_values(value)
    return verdict.name, round(confidences[0].value, 2) if confidences else 0.0
