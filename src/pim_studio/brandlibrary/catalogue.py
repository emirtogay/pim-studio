"""Matching library assets to the options that need them.

Pure functions over data already fetched, so all of it is testable without a
network. Three decisions carry the weight, and the first one changed once the
original client was read.

**Match on the library's own metadata, not on the filename.** Every asset
carries `property_BI_PRODUCT_ID` and `property_BI_COLOR_ID` — the library's own
statement of which option a photo belongs to. That is the index. Parsing
filenames works until a shoot prefix or a naming change breaks it, and it
breaks silently: a photo simply stops being found and the option is recorded as
one the brand has never shot.

Filename matching is kept as a **fallback** for assets whose metadata is
missing, because some are, and a photo we can identify from its name is better
than an absence we report to the brand. It matches whole runs of name parts and
never a raw string prefix: `squash("L3558.13")` is a prefix of
`squash("L3558_132_01")`, so a startswith test would file colour 132's photo
under colour 13 — a wrong photo on a real product page.

**Footwear needs its alias.** Omnitron writes `774RMA3517.255`; the library
holds `74RMA3517.255`. `rules.identifiers.code_variants` is the one place that
knows, and every lookup goes through it.

**One asset per filename, preferring the Zalando edition.** See `dedupe` — it
cost the first run 228 uploads.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ..rules.identifiers import code_variants, squash
from .client import ORIGINAL, Asset

#: Colour chips, not product photography. Publishing one puts a thumbnail-sized
#: square on a product page, which reads as a broken image rather than a small
#: one — the original app filtered these and so does this.
SKIP_NAME_PREFIXES = ("swatch_",)

#: How many adjacent parts of a filename can form a ModelColor, in the fallback.
MAX_KEY_PARTS = 3

_SEPARATORS = re.compile(r"[\s\-_/\\.|]+")

#: Suffix marking the Zalando edition on the photo server.
#:
#: `_ZAL`, matching `FtpUploader.SUFFIX` in the original app — 1,603 files are
#: already published under it, and the resume check compares filenames against
#: the remote listing. A tidier suffix would re-upload every one of them.
EDITION_SUFFIXES = {"zalando": "ZAL", "emea": "EMEA", "apac": "APAC", "outlet": "OUTLET"}


def metadata_key(asset: Asset) -> str | None:
    """The library's own answer to "which option is this?", squashed.

    None when either half is missing, which is what sends the asset to the
    filename fallback rather than into a wrong bucket.
    """
    if not asset.product_id or not asset.color_id:
        return None
    return squash(f"{asset.product_id}{asset.color_id}")


def candidate_keys(asset_name: str) -> list[str]:
    """Squashed keys a filename could stand for, best evidence first.

    Every contiguous run of up to `MAX_KEY_PARTS` parts, longest first and then
    leftmost. `3K_AF8242_ADY_37` offers `3K_AF8242_ADY` and `AF8242_ADY_37`
    before `AF8242_ADY`, which is the one that matches — the shoot prefix means
    the code does not start the name.

    Order is the safety argument: a longer window is stronger evidence, so a
    two-part window is only consulted after every three-part one has failed.
    """
    parts = [p for p in _SEPARATORS.split((asset_name or "").strip()) if p]
    keys: list[str] = []
    for width in range(min(len(parts), MAX_KEY_PARTS), 0, -1):
        for start in range(len(parts) - width + 1):
            key = squash("".join(parts[start : start + width]))
            if key and key not in keys:
                keys.append(key)
    return keys


def is_product_photo(asset: Asset) -> bool:
    return not (asset.name or "").lower().startswith(SKIP_NAME_PREFIXES)


def match(assets: list[Asset], model_colors: list[str]) -> dict[str, list[Asset]]:
    """Group assets under the ModelColors we hold.

    `model_colors` is the oracle: rather than parse an identifier into style and
    colour — which cannot be done reliably, since base codes run from five to
    nine characters and colour codes are not always three — every candidate is
    tried against codes known to exist. A match is evidence, and an asset that
    matches nothing is left out rather than turned into a plausible ModelColor
    no product has.

    Only options with at least one asset appear; the rest are absences, and
    `publish` records them as such.
    """
    known: dict[str, str] = {}
    for code in model_colors:
        for variant in code_variants(code):
            known.setdefault(squash(variant), code)

    found: dict[str, list[Asset]] = defaultdict(list)
    for asset in assets:
        if not is_product_photo(asset):
            continue

        # The library's own metadata first. It is an assertion, not an inference.
        code = known.get(metadata_key(asset) or "")
        if code is None and asset.name:
            for key in candidate_keys(asset.name):
                code = known.get(key)
                if code:
                    break
        if code:
            found[code].append(asset)

    return dict(found)


def dedupe(assets: list[Asset]) -> list[Asset]:
    """One asset per filename, preferring the one carrying a Zalando edition.

    Bynder holds distinct assets under identical names — the same photo, two
    records, one of them with a marketplace edition. The remote filename is
    derived from the name, so without this the second upload silently
    overwrites the first: 136 filenames collided on the original run and 228
    uploads were spent rewriting the same paths.

    Preferring the Zalando-carrying record is what makes one upload serve both
    outputs, so the choice is not arbitrary between two equals.
    """
    best: dict[str, Asset] = {}
    for asset in assets:
        name = asset.filename or asset.id
        current = best.get(name)
        if current is None or (asset.has_zalando and not current.has_zalando):
            best[name] = asset
    return list(best.values())


def remote_name(asset: Asset, edition: str) -> str:
    """What the file is called on the photo server.

    Derived from the asset's own filename, extension included: not every asset
    is a JPEG, and publishing a PNG as `.jpg` produces a file browsers refuse
    to render. The edition suffix goes before the extension — `NAME_ZAL.jpg`,
    matching what is already published — because the photo server serves one
    flat folder and a directory split would collide at the same public path.
    """
    filename = asset.filename
    if edition == ORIGINAL:
        return filename
    suffix = EDITION_SUFFIXES.get(edition, edition.upper())
    stem, dot, extension = filename.rpartition(".")
    return f"{stem}_{suffix}.{extension}" if dot else f"{filename}_{suffix}"


def brandlibrary_key(model_color: str) -> str:
    """The library's spelling of a ModelColor — `AF0276.J2G` -> `AF0276_J2G`.

    Stored alongside the dotted form rather than derived on read: the two
    conventions are a fact of this business, and a stored value survives
    somebody changing their mind about the derivation.
    """
    return model_color.replace(".", "_")
