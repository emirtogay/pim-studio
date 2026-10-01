"""Auditing the filter vocabulary.

Pinned behaviours: casing duplicates and code-scheme duplicates both collapse
to one label, contamination is reported without accusing, and — the rule that
was nearly wrong — a text-keyed attribute whose labels equal its values is
healthy, not unlabelled.
"""

from __future__ import annotations

import pytest

from pim_studio.db import session_scope
from pim_studio.models import OmnitronAttribute, OmnitronAttributeValue
from pim_studio.reports.vocabulary import audit_vocabulary

pytestmark = pytest.mark.usefixtures("temp_db")

_next_pk = iter(range(1, 100000))


def attribute(pk: int, key: str) -> None:
    with session_scope() as db:
        db.add(
            OmnitronAttribute(
                pk=pk, key=key, name=key, data_type="dropdown", erp_code=key
            )
        )


def option(attribute_pk: int, value: str, label: str | None = None) -> None:
    with session_scope() as db:
        db.add(
            OmnitronAttributeValue(
                pk=next(_next_pk),
                attribute=attribute_pk,
                value=value,
                label=value if label is None else label,
            )
        )


SCOPE = {"integration_yaka": "collar", "integration_desen": "pattern"}


# ── redundancy ─────────────────────────────────────────────────────────────


def test_casing_variants_collapse_to_one_label() -> None:
    """`BOĞAZLI YAKA` and `Boğazlı Yaka` are the same option twice — even when
    the *labels* differ only by Turkish casing.

    This is the case plain `casefold()` gets wrong: default Unicode folding
    sends `I` to dotted `i` but leaves dotless `ı` alone, so the same Turkish
    word in two casings folds to two different strings and reads as two
    distinct labels. The live duplicates happened to share identical labels,
    which would have hidden this until the first pair that did not."""
    attribute(253, "integration_yaka")
    option(253, "BOĞAZLI YAKA")
    option(253, "Boğazlı Yaka")

    report = audit_vocabulary({"integration_yaka": "collar"})
    assert report.summaries[0].values == 2
    assert report.summaries[0].distinct_labels == 1
    assert report.summaries[0].redundant == 1
    assert len(report.redundant) == 1
    assert sorted(report.redundant[0].values) == ["BOĞAZLI YAKA", "Boğazlı Yaka"]


def test_same_label_under_two_values_collapses() -> None:
    """The shape the live duplicates actually take: two values, one label."""
    attribute(253, "integration_yaka")
    option(253, "POLO YAKA", "Polo Yaka")
    option(253, "Polo", "Polo Yaka")

    report = audit_vocabulary({"integration_yaka": "collar"})
    assert report.summaries[0].redundant == 1
    assert sorted(report.redundant[0].values) == ["POLO YAKA", "Polo"]


def test_two_code_schemes_collapse_to_one_label() -> None:
    """The desen case: values 003 and 026 both label `Düz` — two numbering
    schemes merged without reconciliation."""
    attribute(243, "integration_desen")
    option(243, "003", "Düz")
    option(243, "026", "Düz")

    report = audit_vocabulary({"integration_desen": "pattern"})
    assert report.redundant[0].label == "düz"
    assert report.redundant[0].values == ["003", "026"]


def test_distinct_labels_are_not_redundant() -> None:
    attribute(244, "integration_kol_boyu")
    option(244, "Kısa Kollu")
    option(244, "Uzun Kollu")

    report = audit_vocabulary({"integration_kol_boyu": "sleeve"})
    assert report.redundant == []
    assert report.summaries[0].redundant == 0


# ── contamination ──────────────────────────────────────────────────────────


def test_a_label_under_two_attributes_is_reported() -> None:
    """`Regular` sits in both waist and length. Reported, not accused — denim
    really is both a pattern and a fabric."""
    attribute(1633, "integration_boy")
    attribute(1634, "integration_bel")
    option(1633, "Regular")
    option(1634, "Regular")

    report = audit_vocabulary({"integration_boy": "length", "integration_bel": "waist"})
    assert len(report.contamination) == 1
    assert report.contamination[0].label == "regular"
    assert report.contamination[0].attributes == ["integration_bel", "integration_boy"]


def test_no_contamination_within_one_attribute() -> None:
    attribute(253, "integration_yaka")
    option(253, "POLO YAKA")
    option(253, "Polo", "POLO YAKA")

    report = audit_vocabulary({"integration_yaka": "collar"})
    assert report.contamination == []


# ── defective values ───────────────────────────────────────────────────────


def test_a_placeholder_dash_is_defective() -> None:
    attribute(253, "integration_yaka")
    option(253, "-")

    report = audit_vocabulary({"integration_yaka": "collar"})
    assert any(d.reason.startswith("placeholder") for d in report.defective)


def test_an_unlabelled_code_in_a_labelled_attribute_is_defective() -> None:
    """desen `000` -> `000`: present, non-null, meaningless — 165 rows of the
    polo export carry it. The class a not-null check launders into 'fine'."""
    attribute(243, "integration_desen")
    for value, label in [("003", "Düz"), ("026", "Düz"), ("012", "Baskılı"), ("000", "000")]:
        option(243, value, label)

    report = audit_vocabulary({"integration_desen": "pattern"})
    reasons = {(d.value, d.reason) for d in report.defective}
    assert ("000", "code repeated as its own label") in reasons


def test_a_text_keyed_attribute_is_not_flagged_as_unlabelled() -> None:
    """The rule that was nearly shipped wrong. Six of yaka's values have label
    equal to value because the attribute is text-keyed — that is its healthy
    shape, not a missing label. A rule that fires on healthy data trains
    people to ignore the report."""
    attribute(253, "integration_yaka")
    for value in ["Kayık Yaka", "Kapüşonlu", "Polo Yaka"]:
        option(253, value)  # label == value, and that is fine

    report = audit_vocabulary({"integration_yaka": "collar"})
    assert report.defective == []


# ── scope and absence ──────────────────────────────────────────────────────


def test_an_attribute_missing_from_the_mirror_is_said_not_skipped() -> None:
    report = audit_vocabulary(SCOPE)
    assert set(report.missing_attributes) == set(SCOPE)


def test_totals_add_up() -> None:
    attribute(253, "integration_yaka")
    option(253, "SPOR")
    option(253, "SPORT", "SPOR")
    option(253, "Kayık Yaka")
    attribute(243, "integration_desen")
    option(243, "003", "Düz")

    report = audit_vocabulary(SCOPE)
    totals = report.totals
    assert totals["values"] == 4
    assert totals["distinct_labels"] == 3
    assert totals["redundant"] == 1
