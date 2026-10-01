"""Deciding what to do when a ModelColor's own SKUs disagree.

The value importer reads at SKU grain and writes at ModelColor grain. When
size T4 and size T7 of one product carry different values for the same
attribute, there is no honest single answer and the importer abstains
(`values.py`). That is right for a real disagreement and wasteful for a
cosmetic one, so this module separates the two.

Measured against the first full sync, 12,193 conflicts:

    genuinely different     10,706   87.8%
    case only                1,147    9.4%
    substring/truncation       326    2.7%
    whitespace/quoting only      14    0.1%

So roughly one in eight carries no meaning, and the rest are editorial. One
sampled case, `RA8432.FTJ` in PL across five SKUs: "Skarpetki unisex" (2),
"Długie skarpety unisex" (2), "Northwind  Socks" (1) — three real names, a
near-tie, and the English one a placeholder. A majority vote there picks a
coin-flip winner, and elsewhere picks the un-updated generic value. That is
the whole argument for abstaining: a visible gap beats an invisible wrong
answer, because only one of the two gets found and fixed.

Every automatic resolution returns the rule that fired, so a wrong call is
traceable rather than silent.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

#: Rules, most conservative first. The first three collapse differences that
#: carry no meaning; the last two are judgement calls and every value they
#: write is queued for human review (2026-08-18: import anyway and flag, an
#: explicit reversal of pure abstention — an empty cell next to a full
#: Omnitron column read as the panel being broken, not as caution).
WHITESPACE = "whitespace"
TRUNCATION = "truncation"
CASE = "case"
LANGUAGE = "language"
MAJORITY = "majority"

#: Rules whose output needs a person to be able to see and correct it.
JUDGEMENT_RULES = frozenset({LANGUAGE, MAJORITY})

#: Comparative language bounds for the LANGUAGE tier — see `settle`. Distinct
#: from `rules.language.FLAG_THRESHOLD` (0.90) on purpose: that one judges a
#: value alone, these judge candidates against each other.
_LOCAL_MAX = 0.30
_ENGLISH_MIN = 0.60

#: Stripped before the Turkish/English test only. The calibrated
#: `english_confidence` numbers above keep reading the text as they always have.
_HTML_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Resolution:
    """A conflict that could be settled without judgement."""

    value: str
    rule: str
    #: What was collapsed, for the log and the audit trail.
    candidates: int


def normalise(value: str) -> str:
    """Strip the differences that carry no meaning.

    Outer quotes because some source columns arrive quoted and some do not;
    escaped and literal newlines because the same copy is stored both ways;
    NBSP and friends because they are invisible. Case is deliberately *not*
    folded here — it is a separate, less safe rule.
    """
    text = unicodedata.normalize("NFC", value).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def resolve(by_value: Mapping[str, list[str]]) -> Resolution | None:
    """Settle a conflict, or return None if it needs a person.

    `by_value` maps each distinct raw value to the sources that reported it,
    so `len(sources)` is how many SKUs said that value.
    """
    if len(by_value) < 2:
        return None

    counts = {normalise(v): 0 for v in by_value}
    longest: dict[str, str] = {}
    for raw, sources in by_value.items():
        key = normalise(raw)
        counts[key] += len(sources)
        # Keep the raw spelling of the longest reporter, so a resolved value
        # is something that actually appeared upstream rather than a
        # reconstruction of it.
        if key not in longest or len(raw) > len(longest[key]):
            longest[key] = raw
    distinct = [k for k in counts if k]
    if not distinct:
        return None

    n = len(by_value)

    # 1. Identical once the noise is gone.
    if len(distinct) == 1:
        return Resolution(longest[distinct[0]].strip(), WHITESPACE, n)

    # 2. One candidate is contained in every other: a truncated variant
    #    carries strictly less, so the longest is the safe pick. Checked
    #    before case so "REGULAR" vs "Regular Fit" is not mistaken for case.
    shortest = min(distinct, key=len)
    if all(shortest in other for other in distinct):
        pick = max(distinct, key=len)
        return Resolution(longest[pick].strip(), TRUNCATION, n)

    # 3. Same text, different capitalisation. Majority wins; ties break on the
    #    lexicographically smallest so the same input always gives the same
    #    output — an importer that picks differently on a re-run would churn
    #    `attribute_values` and make divergence reports meaningless.
    folded = {k.casefold() for k in distinct}
    if len(folded) == 1:
        best = max(sorted(distinct), key=lambda k: counts[k])
        return Resolution(longest[best].strip(), CASE, n)

    return None


def settle(by_value: Mapping[str, list[str]], market: str) -> Resolution | None:
    """Pick a value for a disagreement, however it has to be picked.

    Tiers, in order of how defensible the pick is:

    1. The cosmetic rules (`resolve`) — the difference carries no meaning.
    2. Language disqualification, local-market fields only: candidates that
       read as English (>= 0.90, the only verdict the language rule trusts)
       are wrong-language content by the same test that drives the EN? flags.
       Discard them; if exactly one distinct value remains, it wins. Measured
       on the first full queue: 922 of 10,920 had this shape, and in every
       sampled case the English value was a placeholder. Turkish is
       disqualified the same way, through `foreign_language` — see below.
    3. Majority, ties broken by length then lexicographically, so a re-run
       can never churn the table. This is the tier that can be wrong — on
       SH3855.HBM it enshrines the generic name 8-to-1 over the real copy —
       which is why everything from tiers 2 and 3 lands in `value_conflicts`
       for a person to see and overrule. In a local-market field a candidate
       certainly in English or Turkish only gets a vote when nothing else is
       on the ballot.

    Returns None only when there is nothing writable at all (every candidate
    blank).
    """
    cosmetic = resolve(by_value)
    if cosmetic is not None:
        return cosmetic

    counts: dict[str, int] = {}
    longest: dict[str, str] = {}
    for raw, sources in by_value.items():
        key = normalise(raw)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + len(sources)
        if key not in longest or len(raw) > len(longest[key]):
            longest[key] = raw
    if not counts:
        return None
    n = len(by_value)
    ballot = list(counts)

    if market != "*":
        from ..rules.language import english_confidence, foreign_language

        # Comparative thresholds, not the 0.90 the EN? flag uses. Flagging
        # judges a value in isolation and must not cry wolf; here a clearly
        # local alternative exists in the same field, so the question is only
        # which candidate is the wrong-language one. Measured on the first
        # full queue (9,672 local-market conflicts): requiring one candidate
        # clearly not English (<0.30), every other clearly English (>=0.60)
        # and nothing in the ambiguous middle resolves 1,589, and every
        # sampled case was a placeholder — 'Northwind Headband' losing to
        # 'Opaska damska'. Anything in the middle band falls through.
        confs = {k: english_confidence(k) for k in counts}
        # Turkish needs the wide detector. The calibrated one has no Turkish,
        # so it scores Turkish text 0.0 English — "clearly local" to the test
        # above. On 784RMA0950.2A9 (2026-09-16) seven sizes carried the Turkish
        # master under `pl` and six the Polish text, and the Turkish won the
        # vote 7-6 in all four markets. Markup is stripped first because a
        # description is `<li>` soup and the detector reads tags as words.
        foreign = {k for k in counts if foreign_language(_HTML_TAG.sub(" ", k))}
        wrong = foreign | {k for k, c in confs.items() if c >= _ENGLISH_MIN}
        local = [k for k, c in confs.items() if c < _LOCAL_MAX and k not in foreign]
        if len(local) == 1 and len(wrong) == len(confs) - 1:
            return Resolution(longest[local[0]].strip(), LANGUAGE, n)
        ballot = [k for k in counts if k not in foreign] or ballot

    best = max(sorted(ballot), key=lambda k: (counts[k], len(k)))
    return Resolution(longest[best].strip(), MAJORITY, n)
