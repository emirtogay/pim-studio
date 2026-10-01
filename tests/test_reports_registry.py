"""Reconciling our curated registry against the Omnitron mirror.

The reconciliation exists to decide what to change, so the behaviours worth
pinning are the ones that would mislead that decision: a column that is not an
attribute must not read as a gap, a market must not be attributed to the wrong
mapping, and a real gap must not be silently absorbed.
"""

from __future__ import annotations

import pytest

from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeColumn,
    OmnitronAttribute,
    OmnitronAttributeValue,
)
from pim_studio.reports.registry import FILTER_ATTRIBUTES, reconcile_registry

pytestmark = pytest.mark.usefixtures("temp_db")


def ours(key: str, write_channel: str = "omnitron_import") -> int:
    with session_scope() as db:
        a = Attribute(
            key=key, label=key, write_channel=write_channel, value_grain="model_color"
        )
        db.add(a)
        db.flush()
        return a.id


def theirs(pk: int, key: str, data_type: str = "text", **over) -> None:
    fields = {"pk": pk, "key": key, "name": key, "data_type": data_type, "erp_code": key}
    fields.update(over)
    with session_scope() as db:
        db.add(OmnitronAttribute(**fields))


def column(attribute_id: int, name: str, market: str | None = None) -> None:
    with session_scope() as db:
        db.add(
            AttributeColumn(
                attribute_id=attribute_id, source="omnitron", column_name=name, market=market
            )
        )


def vocabulary(attribute_pk: int, n: int) -> None:
    with session_scope() as db:
        for i in range(n):
            db.add(
                OmnitronAttributeValue(
                    pk=attribute_pk * 1000 + i, attribute=attribute_pk, value=str(i), label=str(i)
                )
            )


# ── the join ───────────────────────────────────────────────────────────────


def test_a_column_resolves_to_its_upstream_attribute() -> None:
    """The join is via attribute_columns, not on key: ours are semantic
    (`collar_type`), theirs operational (`integration_yaka`)."""
    ours_id = ours("collar_type")
    theirs(253, "integration_yaka", "dropdown")
    column(ours_id, "attributes__integration_yaka")

    report = reconcile_registry()
    assert [(m.our_key, m.omnitron_key) for m in report.mapped] == [
        ("collar_type", "integration_yaka")
    ]
    assert report.unmapped_ours == []


def test_an_attribute_with_no_column_is_unmapped() -> None:
    ours("fit", write_channel="product_card")
    theirs(245, "integration_kalip", "dropdown")

    report = reconcile_registry()
    assert report.unmapped_ours == [("fit", "product_card")]
    assert report.mapped == []


def test_erp_code_is_a_fallback_join() -> None:
    ours_id = ours("gender")
    theirs(100, "some_other_key", erp_code="integration_cinsiyet")
    column(ours_id, "attributes__integration_cinsiyet")

    report = reconcile_registry()
    assert [m.omnitron_key for m in report.mapped] == ["some_other_key"]


# ── markets belong to the column, not the attribute ────────────────────────


def test_each_mapping_carries_only_its_own_market() -> None:
    """`description` reaches five upstream keys, four of them market-suffixed.
    Taking the union across the attribute would report every mapping as
    covering all four markets — which reads as fully translated when the truth
    may be one column per market with gaps between them."""
    ours_id = ours("description")
    for pk, (key, market) in enumerate(
        [
            ("integration_kalem_icerik_pol", "PL"),
            ("integration_kalem_icerik_cz", "CZ"),
            ("integration_kalem_icerik_hu", "HU"),
            ("integration_kalem_icerik_sk", "SK"),
        ],
        start=300,
    ):
        theirs(pk, key, "area")
        column(ours_id, f"attributes__{key}", market=market)

    report = reconcile_registry()
    by_key = {m.omnitron_key: m.markets for m in report.mapped}
    assert by_key["integration_kalem_icerik_pol"] == ["PL"]
    assert by_key["integration_kalem_icerik_cz"] == ["CZ"]
    assert by_key["integration_kalem_icerik_sk"] == ["SK"]


# ── columns that are legitimately not attributes ───────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "active_price__price",
        "active_stock__stock",
        "active_categories__category__name",
        "productimage_set__0__image",
        "base_code",
        "name",
    ],
)
def test_membership_and_product_field_columns_are_not_gaps(name: str) -> None:
    """Prices, stock, categories and images are facts about a catalogue, not
    attributes. Counting them as gaps inflates the number and buries the real
    ones."""
    ours_id = ours("price")
    column(ours_id, name)

    report = reconcile_registry()
    assert name in [c for c, _ in report.non_attribute_columns]
    assert report.unexplained_columns == []


def test_label_projections_are_not_gaps() -> None:
    ours_id = ours("gender")
    column(ours_id, "attributes_kwargs__integration_cinsiyet__label")

    report = reconcile_registry()
    assert report.non_attribute_columns
    assert "projection" in report.non_attribute_columns[0][1]
    assert report.unexplained_columns == []


def test_a_column_pointing_at_a_missing_attribute_is_unexplained() -> None:
    """The case that must stay loud: the mapping names an upstream key that
    does not exist, so either the key is wrong or the attribute was deleted."""
    ours_id = ours("pattern")
    column(ours_id, "attributes__integration_ghost")

    report = reconcile_registry()
    assert report.unexplained_columns == ["attributes__integration_ghost"]


# ── the filter attributes ──────────────────────────────────────────────────


def test_filter_attributes_are_reported_even_when_entirely_unmapped() -> None:
    """All seven exist upstream with real vocabularies and none are mapped.
    Reported as their own section because they are the stated objective, and a
    131-row unmapped list would bury them."""
    theirs(253, "integration_yaka", "dropdown")
    vocabulary(253, 26)

    report = reconcile_registry()
    assert len(report.filter_attributes) == len(FILTER_ATTRIBUTES)

    by_key = {
        k: (present, values, mapped)
        for k, _, present, values, mapped in report.filter_attributes
    }
    assert by_key["integration_yaka"] == (True, 26, False)
    # Not seeded into the mirror in this test, so reported absent rather than
    # silently omitted.
    assert by_key["integration_bel"] == (False, 0, False)
    assert report.counts["filter_attributes_unmapped"] == len(FILTER_ATTRIBUTES)


def test_a_mapped_filter_attribute_is_reported_as_mapped() -> None:
    ours_id = ours("collar_type", write_channel="product_card")
    theirs(253, "integration_yaka", "dropdown")
    column(ours_id, "attributes__integration_yaka")

    report = reconcile_registry()
    mapped = {k: m for k, _, _, _, m in report.filter_attributes}
    assert mapped["integration_yaka"] is True
    assert report.counts["filter_attributes_unmapped"] == len(FILTER_ATTRIBUTES) - 1


# ── ordering ───────────────────────────────────────────────────────────────


def test_unmapped_upstream_is_ordered_by_vocabulary_size() -> None:
    """An unmapped dropdown carrying hundreds of values is a different prospect
    from an unmapped text field carrying none; alphabetical order hides that."""
    theirs(1, "aaa_small", "dropdown")
    vocabulary(1, 2)
    theirs(2, "zzz_big", "dropdown")
    vocabulary(2, 50)

    report = reconcile_registry()
    keys = [k for k, _, _ in report.unmapped_theirs]
    assert keys.index("zzz_big") < keys.index("aaa_small")


# ── localizable columns read in one language ───────────────────────────────


def test_a_localizable_column_routed_to_a_market_is_not_flagged() -> None:
    """`urun_adi_pl` at market PL goes through PL's locale chain, so every
    language behind it is read."""
    ours_id = ours("product_name")
    theirs(874, "urun_adi_pl", is_localizable=True)
    column(ours_id, "attributes__urun_adi_pl", market="PL")

    report = reconcile_registry()
    assert report.single_language_columns == []


def test_a_localizable_column_with_no_market_is_flagged() -> None:
    """The defect this check exists for: `integration_kalem_icerik` holds a
    different description per language, and a market-agnostic mapping reads
    only the tenant default — Turkish — while every count looks healthy."""
    ours_id = ours("description")
    theirs(232, "integration_kalem_icerik", "area", is_localizable=True)
    column(ours_id, "attributes__integration_kalem_icerik", market=None)

    report = reconcile_registry()
    assert report.single_language_columns == [
        ("attributes__integration_kalem_icerik", "description", None)
    ]
    assert report.counts["single_language_columns"] == 1


def test_a_localizable_column_in_a_market_with_no_chain_is_flagged() -> None:
    """UA is mapped by nobody today, so a UA column would be read in the
    default language — the same defect wearing a market code."""
    ours_id = ours("product_name")
    theirs(1900, "urun_adi_ua", is_localizable=True)
    column(ours_id, "attributes__urun_adi_ua", market="UA")

    report = reconcile_registry()
    assert [c for c, _, _ in report.single_language_columns] == ["attributes__urun_adi_ua"]


def test_a_non_localizable_column_is_never_flagged() -> None:
    """One value regardless of the header — there is no other language to miss."""
    ours_id = ours("composition")
    theirs(259, "integration_erp_icerik", is_localizable=False)
    column(ours_id, "attributes__integration_erp_icerik", market=None)

    report = reconcile_registry()
    assert report.single_language_columns == []


def test_unmapped_localizable_attributes_are_called_out_separately() -> None:
    """Per-language content nothing reads at all — a different finding from
    the ~130 unmapped keys it otherwise hides inside."""
    theirs(1, "allegro_name", is_localizable=True)
    theirs(2, "integration_beden", "dropdown", is_localizable=False)

    report = reconcile_registry()
    assert report.unmapped_localizable == [("allegro_name", "text")]
    assert {k for k, _, _ in report.unmapped_theirs} == {"allegro_name", "integration_beden"}
