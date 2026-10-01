"""Liveness rule 6 — which `PL_comment` values block a product.

Implements `Rules/Ban list`. Two things in that note are load-bearing and are
easy to lose if this is ever rewritten as a regex:

A stored list, not a keyword match. `FR` and `TR` are market codes and must not
ban. A rule on "remov" would ban the first comment reading "do not remove
before AW27", and would miss a future ban phrased without the word.

Unknown means *someone decide*, not *fine*. A comment on neither list is a
third outcome, not a quiet pass. The field held exactly ten distinct values
across 3,054 SKUs when it was profiled, so the human cost of classifying a new
one is close to zero, and it removes the whole class of silent
misclassification.

Matching is case-insensitive and whitespace-trimmed, which is what folds
`Removed` into `REMOVED` without needing a row for each casing.
"""

from __future__ import annotations

from enum import Enum

# The confirmed classification of every value observed in the 2026-04-16
# export. This is the seed for `ban_comments`, not the runtime source of truth:
# the table is editable in the panel because new comment conventions appear as
# merch invents them and that must not need a deploy. `seed.py` loads this in;
# callers with a database should pass the table's contents instead.
#
# comment, bans, note
SEEDED_BAN_COMMENTS: tuple[tuple[str, bool, str], ...] = (
    ("REMOVED", True, "confirmed ban"),
    ("Removed", True, "casing variant of REMOVED; matching is case-insensitive anyway"),
    ("removed high discount", True, "confirmed ban"),
    ("Closed 27.03 low GM", True, "confirmed ban"),
    ("(hight markdown) removed items_10.03.2026", True, "confirmed ban"),
    ("Removed, wrong image", True, "confirmed ban 2026-08-13"),
    ("removed since 20.03.2025", True, "confirmed ban 2026-08-13"),
    ("FR", False, "market code, not a ban"),
    ("TR", False, "market code, not a ban"),
    ("Season changed as 26SS, 09.04", False, "administrative note"),
)


class BanVerdict(Enum):
    """Three outcomes, because two would force unknowns into one of them."""

    BANNED = "banned"
    NOT_BANNED = "not_banned"
    #: On neither list. Not a pass — a request for classification.
    UNKNOWN = "unknown"


def _default_classifications() -> dict[str, bool]:
    return {c.strip().lower(): bans for c, bans, _ in SEEDED_BAN_COMMENTS}


def classify_comment(
    comment: str | None,
    classifications: dict[str, bool] | None = None,
) -> BanVerdict:
    """Classify one `PL_comment` value.

    `classifications` maps a lowercased, trimmed comment to whether it bans;
    pass the `ban_comments` table's contents. Defaults to the seeded set so the
    rule is usable without a database, which is what lets the demo generator
    and the tests call it.

    An empty comment is `NOT_BANNED`, not `UNKNOWN`: no comment is a known
    state of the field, and the overwhelming majority of rows are in it.
    """
    key = (comment or "").strip().lower()
    if not key:
        return BanVerdict.NOT_BANNED
    table = _default_classifications() if classifications is None else classifications
    if key not in table:
        return BanVerdict.UNKNOWN
    return BanVerdict.BANNED if table[key] else BanVerdict.NOT_BANNED
