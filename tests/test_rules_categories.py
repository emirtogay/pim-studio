"""Main-category inference — a stopgap, pinned so replacing it is visible."""

from __future__ import annotations

import pytest

from pim_studio.rules import main_category


@pytest.mark.parametrize(
    ("sub_category", "expected"),
    [
        ("Sneakers", "Footwear"),
        ("Men's Shoes", "Footwear"),
        ("Sandals", "Footwear"),
        ("Bags", "Leather Goods"),
        ("Wallet", "Leather Goods"),
        ("Caps", "Leather Goods"),
        ("Accessories", "Leather Goods"),
        ("Briefs", "Underwear"),
        ("Trunks", "Underwear"),
        ("Polo", "Textile"),
        ("Sweatshirt", "Textile"),
    ],
)
def test_keyword_mapping(sub_category: str, expected: str) -> None:
    assert main_category(sub_category, "TH2568.144") == expected


def test_watches_are_recognised_by_model_color_shape() -> None:
    """Watch styles are four digits then a dot. Keying off the identifier
    rather than the category is the stopgap-est part of a stopgap."""
    assert main_category("", "3196.130") == "Watches"


def test_a_category_wins_over_the_watch_shape() -> None:
    assert main_category("Sneakers", "3196.130") == "Footwear"


def test_nothing_to_infer_from_returns_the_placeholder() -> None:
    assert main_category("", "TH2568.144") == "—"
    assert main_category("", "") == "—"


def test_known_defect_watches_is_not_in_the_seeded_vocabulary() -> None:
    """Documented rather than fixed, because the fix is to ingest the
    product-cards file and delete this module. The registry would reject the
    value this produces. See Findings/Audit 2026-08-14.
    """
    from pim_studio.seed import VOCABULARIES

    _, approved = VOCABULARIES["main_category"]
    assert main_category("", "3196.130") not in approved


def test_known_defect_keyword_match_misfiles_a_bag_shaped_name() -> None:
    """The objection Rules/Ban list raises about keyword matching applies here
    too. Acceptable only because a wrong facet is visibly wrong, while a wrong
    ban takes a selling product offline."""
    assert main_category("Baggy Fit Trousers", "TH2568.144") == "Leather Goods"
