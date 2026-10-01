"""Rules/Ban list.

The cases here are the ones the vault note argues about, not the happy path.
Each of the first three is a way a keyword regex on "remov" would have got the
answer wrong, which is why the rule is a stored list.
"""

from __future__ import annotations

import pytest

from pim_studio.rules import BanVerdict, classify_comment
from pim_studio.rules.ban import SEEDED_BAN_COMMENTS


@pytest.mark.parametrize(
    "comment",
    [
        "REMOVED",
        "Removed",  # casing variant, folded by matching rather than by a row
        "removed",
        "  REMOVED  ",  # trimmed
        "removed high discount",
        "Closed 27.03 low GM",
        "(hight markdown) removed items_10.03.2026",
        "Removed, wrong image",
        "removed since 20.03.2025",
    ],
)
def test_confirmed_bans(comment: str) -> None:
    assert classify_comment(comment) is BanVerdict.BANNED


@pytest.mark.parametrize("comment", ["FR", "TR", "fr", "tr"])
def test_market_codes_do_not_ban(comment: str) -> None:
    """`FR` and `TR` are market codes on 1,080 SKUs. A rule that banned them
    would take a tenth of the commented catalogue offline."""
    assert classify_comment(comment) is BanVerdict.NOT_BANNED


def test_administrative_note_does_not_ban() -> None:
    assert classify_comment("Season changed as 26SS, 09.04") is BanVerdict.NOT_BANNED


@pytest.mark.parametrize("comment", ["", "   ", None])
def test_no_comment_is_not_banned_rather_than_unknown(comment: str | None) -> None:
    """No comment is a known state of the field, and most rows are in it.
    Returning UNKNOWN here would ask someone to classify emptiness 10,000
    times."""
    assert classify_comment(comment) is BanVerdict.NOT_BANNED


@pytest.mark.parametrize(
    "comment",
    [
        "do not remove before AW27",  # a keyword rule bans this. It must not.
        "Closed for repricing",
        "banned pending legal",
        "wycofany",
    ],
)
def test_unseen_comments_are_unknown_not_a_quiet_pass(comment: str) -> None:
    """The whole point of the three-state verdict.

    An unrecognised comment is a request for classification. Collapsing it to
    not-banned is the silent misclassification the rule exists to prevent, and
    the first two cases show why a keyword rule cannot be the answer either:
    one contains "remove" and must not ban, one contains "closed" and we do not
    know.
    """
    assert classify_comment(comment) is BanVerdict.UNKNOWN


def test_classifications_come_from_the_caller_when_supplied() -> None:
    """The ban list is registry data, editable in the panel, because new
    comment conventions appear as merch invents them and that must not need a
    deploy. So a caller with the table passes it in and the seeded default is
    not consulted."""
    table = {"wycofany": True}
    assert classify_comment("Wycofany", table) is BanVerdict.BANNED
    # On the seeded list but not the caller's: unknown to *this* table.
    assert classify_comment("REMOVED", table) is BanVerdict.UNKNOWN


def test_seeded_list_matches_the_vault_note() -> None:
    """Rules/Ban list: ten distinct values, seven of which ban."""
    assert len(SEEDED_BAN_COMMENTS) == 10
    assert sum(1 for _, bans, _ in SEEDED_BAN_COMMENTS if bans) == 7


def test_seeded_list_has_no_contradictions_once_folded() -> None:
    """`REMOVED` and `Removed` are both seeded and fold to one key. That is
    harmless only while they agree — a future `removed` classified as
    not-banned would make the verdict depend on scan order."""
    folded: dict[str, bool] = {}
    for comment, bans, _ in SEEDED_BAN_COMMENTS:
        key = comment.strip().lower()
        assert folded.get(key, bans) == bans, f"contradictory classification for {key!r}"
        folded[key] = bans
