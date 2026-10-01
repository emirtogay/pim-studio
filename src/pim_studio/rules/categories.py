"""Main category, derived from the sub-category name.

A stopgap, and worth being honest about which kind. `main_category` is a
`product_card` attribute — the Turkey team owns it and it reaches Omnitron on
their file. Until we ingest that file we have no supplied value, so this
infers one from the sub-category string in order to make the Products page's
category facet usable at all.

It is a keyword match, which is the shape `Rules/Ban list` argues against for
bans, and the same objection applies here: it will misfile the first
sub-category that mentions "bag" without being one. The difference is that a
wrong ban takes a selling product offline while a wrong category facet is
visibly wrong to anyone reading the page. That makes it acceptable as a
stopgap and unacceptable as a destination.

Two known defects, both preserved deliberately so the extraction of these
rules changed no output:

- `Watches` is returned but is not in the seeded `main_category` vocabulary
  (`Textile`, `Leather Goods`, `Underwear`, `Footwear`), so the registry would
  reject the value this produces.
- The watch test reads the ModelColor shape rather than the category, which
  works because watch styles are four digits, and stops working the day
  another line adopts that shape.

Replace wholesale when the product-cards file is ingested.
"""

from __future__ import annotations

import re

# Watch styles are four digits then a dot — `3196.130`. See the docstring for
# why keying off the identifier shape is a stopgap.
_WATCH_MODEL_COLOR = re.compile(r"^\d{4}\.")

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Footwear", ("shoe", "sneaker", "footwear", "sandal")),
    ("Leather Goods", ("bag", "wallet", "belt", "cap", "leather", "accessor")),
    ("Underwear", ("brief", "trunk", "bra", "thong", "underwear")),
)


def main_category(sub_category: str, model_color: str) -> str:
    """Infer the main category. Returns "—" when there is nothing to infer from."""
    haystack = (sub_category or "").lower()
    for label, keywords in _KEYWORDS:
        if any(k in haystack for k in keywords):
            return label
    if _WATCH_MODEL_COLOR.match(model_color or ""):
        return "Watches"
    return "Textile" if sub_category else "—"
