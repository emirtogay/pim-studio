"""Identifier normalisation.

ModelColor is the reporting grain; the size-suffixed base code is what the
exports carry. Deriving one from the other is a single rule and belongs in one
place, because every source spells the suffix slightly differently and each
new spelling would otherwise be patched wherever it was noticed.
"""

from __future__ import annotations

import re

# `.T04`, `.TXL`, `.T2XL` — the apparel size suffix. Kept only for the
# two-part case described in `model_color`; the general rule no longer needs it.
_SIZE_SUFFIX = re.compile(r"\.T[^.]*$")

# Values that mean "no value" once stringified. `0` and `0.0` are in here
# because the Omnitron exports use a numeric zero as the empty cell for text
# columns — measured, not assumed, but it *is* a per-source quirk rather than a
# universal truth, so it stays in one function that can be given a per-source
# override when a source disagrees.
_BLANKS = frozenset({"", "None", "null", "0.0", "0"})


def model_color(base_code_format: str) -> str:
    """`TH2568.144.T04` → `TH2568.144`. Everything before the second dot.

    ModelColor is STYLE.COLOR, so the rule is positional: take the first two
    dot-separated parts. It used to strip a `.T<size>` suffix by pattern, which
    is the same answer for apparel and **wrong for everything else** — watches
    and accessories carry `-` as their size, so `2011169.-.-` kept its suffix
    and yielded `2011169.-.-` instead of `2011169.-`. That would have created a
    distinct, wrong ModelColor for every watch and accessory in the catalogue,
    silently, since the value still looks plausible.

    Found 2026-08-14 when the product set moved from an apparel-heavy export to
    full catalogue membership. The `.T` pattern was never a rule about sizes —
    it was a rule about the products we happened to have seen.
    """
    parts = (base_code_format or "").upper().split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2])
    # Two parts or fewer: already a ModelColor, though a stray `.T04` on a
    # two-part value would still be a size, so that case keeps the old strip.
    return _SIZE_SUFFIX.sub("", ".".join(parts))


def normalise_blank(value: object) -> str:
    """Stringify and trim, mapping the export's several spellings of empty to "".

    Note the consequence, which matters wherever a count is derived from this:
    a genuine zero and an unpopulated cell both become "". A caller that needs
    to tell "known to be zero" from "never reported" cannot get that from this
    function and must look at whether the source row existed at all.
    """
    text = str(value).strip() if value is not None else ""
    return "" if text in _BLANKS else text


# Everything that separates STYLE from COLOUR across the five conventions the
# README lists: dotted in Omnitron, underscored in BrandLibrary, two columns in
# the product-card template, concatenated in the order file, spaced when
# somebody retypes one. Sizes ride along on the end in yet more spellings.
_SEPARATORS = re.compile(r"[\s\-_/\\|]+")


def dotted(code: str) -> str:
    """Every separator turned into a dot, upper-cased. `l3558 132` -> `L3558.132`.

    The counterpart to `squash`: that one removes the boundary to compare two
    spellings, this one normalises it to the spelling this codebase writes. Used
    wherever a written-down code has to be stored or sent on rather than merely
    matched.
    """
    return _SEPARATORS.sub(".", (code or "").strip()).upper()


def squash(code: str) -> str:
    """Every separator removed, upper-cased. The form two spellings share.

    `L3558.132`, `L3558_132`, `l3558 132` and `L3558132` all squash to
    `L3558132`, which is what makes them matchable without deciding where the
    colour code starts. Guessing that boundary — "the last three characters" —
    is right for most of the catalogue and wrong for enough of it to matter,
    and the wrong answers look exactly like the right ones.
    """
    return _SEPARATORS.sub("", (code or "").replace(".", "")).upper()


# Footwear carries a leading `7` in Omnitron that the brand library drops — it
# stores those codes without it. `774RMA3517.255` here
# is `74RMA3517.255` there.
#
# The pattern is deliberately narrow: 7, then two digits, three letters and four
# digits, and the next character must not be alphanumeric. Anything looser also
# strips the 7 from plain numeric product ids that merely start with one —
# `7006103_433` is a real code and is not a shoe.
_SHOE_CODE = re.compile(r"^7(\d{2}[A-Za-z]{3}\d{4}(?![A-Za-z0-9]).*)$")


def shoe_alias(code: str) -> str | None:
    """`774RMA3517.255` -> `74RMA3517.255`, or None when it is not a shoe code.

    The fifth of the five identifier conventions, and the one with teeth: this
    is not a spelling difference that normalisation absorbs. Without it every
    footwear option fails to match in the library and is recorded as a photo
    the brand has never shot.
    """
    match = _SHOE_CODE.match((code or "").strip())
    return match.group(1) if match else None


def code_variants(code: str) -> list[str]:
    """The code as written, then any known alias. Try them in this order.

    As written first, always: an alias is a fallback for a convention we know
    upstream uses, not a replacement for the code we actually hold.
    """
    alias = shoe_alias(code)
    return [code, alias] if alias and alias != code else [code]


def resolve_model_color(token: str, known: dict[str, str]) -> str | None:
    """Match one written-down code against the ModelColors we actually hold.

    `known` maps `squash(model_color)` to the canonical spelling, and is the
    oracle: rather than parse a token into STYLE and COLOUR — which cannot be
    done reliably, since base codes run from five characters to nine and colour
    codes are not always three — this tries progressively lossier forms of the
    token against codes we know exist. A match is therefore evidence, not a
    guess, and a token that matches nothing is reported as unmatched instead of
    being turned into a plausible ModelColor that no product has.

    Order matters. The size suffix is stripped *after* the full form has been
    tried, so a genuine three-part ModelColor — if one ever exists — is not
    quietly truncated to two.
    """
    raw = (token or "").strip()
    if not raw:
        return None

    dotted = _SEPARATORS.sub(".", raw).upper()
    for candidate in (raw.upper(), dotted, model_color(dotted)):
        found = known.get(squash(candidate))
        if found:
            return found
    return None
