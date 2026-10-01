"""Our 26 curated attributes against Omnitron's 149.

The build log's next step 3: *reconcile rather than guess*. The registry was
hand-seeded from two exports before API access existed, so the question is not
"is it wrong" but "where does it differ, and which differences matter".

## How the two sides join

Not on `key`. Ours are semantic — `collar_type`, `composition`, `product_name` —
and Omnitron's are operational — `integration_yaka`, `urun_detay_pol`. Zero of
our 26 keys match an Omnitron key directly, and that is by design rather than
drift: our key names what the value *means*, theirs names where it *lives*.

The join runs through `attribute_columns`, which maps a source column string to
(attribute, market). An Omnitron column named `attributes__integration_yaka`
flattens the product object's `attributes.integration_yaka`, so stripping the
`attributes__` prefix yields the upstream key. That is the contract
`Findings/Omnitron column contract` deduced from the outside, and the mirror
now confirms it from the inside.

## What counts as unmapped, and what does not

Ten of the 28 omnitron-source columns resolve to no attribute, and every one is
correctly unresolvable rather than missing:

    active_price__price, active_price__retail_price   price list membership
    active_stock__stock                               stock list membership
    active_categories__category__name                 category assignment
    productimage_set__0__image                        image slot zero
    base_code, name                                   product fields
    attributes_kwargs__*__label  (three)              label projections

Prices, stock, categories and images are membership facts about a catalogue,
not attributes of a product, and the `_kwargs__*__label` columns are read-only
projections of an attribute we already map. Counting them as gaps would inflate
the number and bury the real ones, which is the same mistake `applies_when`
exists to prevent on the product side.

## What this report cannot tell you

Which of the 149 are used only by channels we ignore. That needs product data,
and we have not imported attribute values yet. Attribute-set membership is
reported instead — a real signal about where an attribute is expected, but not
the same question. Saying so is the point; a report that implied otherwise
would be worse than one that admits the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from ..db import session_scope
from ..models import (
    Attribute,
    AttributeColumn,
    OmnitronAttribute,
    OmnitronAttributeConfig,
    OmnitronAttributeValue,
    VocabularyOption,
)
from ..sync.values import LOCALE_CHAINS

#: Columns flattened from the product object's `attributes` map. Stripping this
#: prefix yields the Omnitron attribute key.
_ATTR_PREFIX = "attributes__"

#: `market_attributes__<market>__<key>` — the same flat map, read under one
#: market's locale. Stripping the prefix and the market yields the key.
_MARKET_PREFIX = "market_attributes__"

#: Columns that are legitimately not attributes. Held as an explicit list so an
#: unmapped column that is *not* on it stands out as a real finding rather than
#: disappearing into a count. Keyed by the reason, because the reason is what a
#: reader needs.
NOT_ATTRIBUTES = {
    "active_price__price": "price list membership, not an attribute",
    "active_price__retail_price": "price list membership, not an attribute",
    "active_stock__stock": "stock list membership, not an attribute",
    "active_categories__category__name": "category assignment, not an attribute",
    "productimage_set__0__image": "image slot zero, not an attribute",
    "base_code": "product field, not an attribute",
    "name": "product field, not an attribute",
}


#: The seven garment attributes the filter goal rests on, confirmed present on
#: the live instance on 2026-08-14. Called out separately because they are the
#: stated objective — customers filtering by collar, fit, sleeve and so on — and
#: burying them in a 131-row unmapped list would hide the finding that matters
#: most. Keys and glosses from `Findings/First connection`.
FILTER_ATTRIBUTES = {
    "integration_yaka": "collar",
    "integration_kalip": "fit",
    "integration_kol_boyu": "sleeve",
    "integration_desen": "pattern",
    "integration_kumas_tipi": "fabric",
    "integration_boy": "length",
    "integration_bel": "waist",
}


def _is_label_projection(column: str) -> bool:
    """`attributes_kwargs__<key>__label` is a read-only projection of <key>."""
    return column.startswith("attributes_kwargs__") and column.endswith("__label")


@dataclass
class MappedAttribute:
    """One of ours, resolved to an upstream attribute."""

    our_key: str
    write_channel: str
    omnitron_key: str
    omnitron_pk: int
    data_type: str
    is_localizable: bool
    markets: list[str] = field(default_factory=list)
    vocabulary_ours: int = 0
    vocabulary_theirs: int = 0
    set_count: int = 0


@dataclass
class ReconciliationReport:
    mapped: list[MappedAttribute] = field(default_factory=list)
    #: Ours with no upstream attribute behind any of their columns.
    unmapped_ours: list[tuple[str, str]] = field(default_factory=list)
    #: Their keys we map to nothing. The big one — most of the 149.
    unmapped_theirs: list[tuple[str, str, int]] = field(default_factory=list)
    #: Columns that resolve to no attribute, with why.
    non_attribute_columns: list[tuple[str, str]] = field(default_factory=list)
    #: Columns that resolve to nothing and have no accepted reason. Real gaps.
    unexplained_columns: list[str] = field(default_factory=list)
    #: The seven filter attributes: (key, gloss, present upstream, values, mapped).
    filter_attributes: list[tuple[str, str, bool, int, bool]] = field(default_factory=list)
    #: Flat columns on a localizable attribute that the values sync can only
    #: ever read in one language: (column, our attribute, market or None).
    single_language_columns: list[tuple[str, str, str | None]] = field(default_factory=list)
    #: Theirs, unmapped *and* localizable: (key, data_type). Per-language
    #: content we capture nothing of at all.
    unmapped_localizable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "mapped": len(self.mapped),
            "unmapped_ours": len(self.unmapped_ours),
            "unmapped_theirs": len(self.unmapped_theirs),
            "non_attribute_columns": len(self.non_attribute_columns),
            "unexplained_columns": len(self.unexplained_columns),
            "filter_attributes_unmapped": sum(
                1 for *_, mapped in self.filter_attributes if not mapped
            ),
            "single_language_columns": len(self.single_language_columns),
            "unmapped_localizable": len(self.unmapped_localizable),
        }


def reconcile_registry() -> ReconciliationReport:
    """Compare the curated registry against the mirror. Reads only."""
    report = ReconciliationReport()

    with session_scope() as db:
        ours = list(db.scalars(select(Attribute)))
        theirs = list(db.scalars(select(OmnitronAttribute)))
        columns = list(
            db.scalars(select(AttributeColumn).where(AttributeColumn.source == "omnitron"))
        )

        by_key = {a.key: a for a in theirs}
        by_erp = {a.erp_code: a for a in theirs if a.erp_code}
        ours_by_id = {a.id: a.key for a in ours}

        vocab_ours: dict[int, int] = {
            row[0]: row[1]
            for row in db.execute(
                select(VocabularyOption.attribute_id, func.count()).group_by(
                    VocabularyOption.attribute_id
                )
            )
        }
        vocab_theirs: dict[int, int] = {
            row[0]: row[1]
            for row in db.execute(
                select(OmnitronAttributeValue.attribute, func.count()).group_by(
                    OmnitronAttributeValue.attribute
                )
            )
        }
        set_counts: dict[int, int] = {
            row[0]: row[1]
            for row in db.execute(
                select(OmnitronAttributeConfig.attribute, func.count()).group_by(
                    OmnitronAttributeConfig.attribute
                )
            )
        }

        # Our attribute id -> upstream key -> (upstream attribute, markets).
        #
        # The market is carried per *column*, not per attribute. One of ours can
        # map to several upstream keys — `description` reaches
        # `integration_kalem_icerik` plus four market-suffixed siblings — and
        # each of those carries its own market. Collecting the union across the
        # attribute would report every mapping as covering all four markets,
        # which reads as "fully translated" when the truth may be one column per
        # market and gaps between them.
        resolved: dict[int, dict[str, tuple[OmnitronAttribute, set[str]]]] = {}
        for column in columns:
            name = column.column_name
            if name in NOT_ATTRIBUTES:
                report.non_attribute_columns.append((name, NOT_ATTRIBUTES[name]))
                continue
            if _is_label_projection(name):
                report.non_attribute_columns.append(
                    (name, "read-only label projection of the attribute it names")
                )
                continue
            # `market_attributes__<market>__<key>` reads one flat key under a
            # market's locale. Same upstream attribute as `attributes__<key>`,
            # reached through a name that carries the market so four markets
            # can share one key without colliding on the unique constraint.
            if name.startswith(_MARKET_PREFIX):
                _, _, upstream_key = name[len(_MARKET_PREFIX) :].partition("__")
                if not upstream_key:
                    report.unexplained_columns.append(name)
                    continue
            elif name.startswith(_ATTR_PREFIX):
                upstream_key = name[len(_ATTR_PREFIX) :]
            else:
                report.unexplained_columns.append(name)
                continue
            match = by_key.get(upstream_key) or by_erp.get(upstream_key)
            if match is None:
                report.unexplained_columns.append(name)
                continue

            # A localizable attribute holds a different value per language and
            # Omnitron serves one language per request, so a flat column on one
            # is only read in full if the values sync routes it through a
            # locale chain — and it does that per market. A localizable column
            # with no market, or a market with no chain, is therefore read in
            # the tenant default and one language of several is all we ever
            # store. That is how `integration_kalem_icerik` put Turkish
            # descriptions in a market-agnostic row for months without any
            # count looking wrong. Cheap to check, and it cannot regress
            # quietly: a new mapping that forgets the market lands here.
            if match.is_localizable and column.market not in LOCALE_CHAINS:
                report.single_language_columns.append(
                    (name, ours_by_id.get(column.attribute_id, "?"), column.market)
                )

            per_attribute = resolved.setdefault(column.attribute_id, {})
            _, markets = per_attribute.setdefault(match.key, (match, set()))
            if column.market:
                markets.add(column.market)

        matched_upstream_keys: set[str] = set()
        for attribute in sorted(ours, key=lambda a: a.key):
            hits = resolved.get(attribute.id)
            if not hits:
                report.unmapped_ours.append((attribute.key, attribute.write_channel))
                continue
            for upstream, markets in hits.values():
                matched_upstream_keys.add(upstream.key)
                report.mapped.append(
                    MappedAttribute(
                        our_key=attribute.key,
                        write_channel=attribute.write_channel,
                        omnitron_key=upstream.key,
                        omnitron_pk=upstream.pk,
                        data_type=upstream.data_type,
                        is_localizable=upstream.is_localizable,
                        markets=sorted(markets),
                        vocabulary_ours=vocab_ours.get(attribute.id, 0),
                        vocabulary_theirs=vocab_theirs.get(upstream.pk, 0),
                        set_count=set_counts.get(upstream.pk, 0),
                    )
                )

        for upstream in sorted(theirs, key=lambda a: a.key):
            if upstream.key not in matched_upstream_keys:
                report.unmapped_theirs.append(
                    (upstream.key, upstream.data_type, vocab_theirs.get(upstream.pk, 0))
                )
                # Called out separately from the ~130 unmapped keys: an
                # unmapped localizable attribute is per-language content the
                # markets may already be paying someone to write, and no
                # not-null check anywhere can miss it more completely than by
                # never reading the column at all.
                if upstream.is_localizable:
                    report.unmapped_localizable.append((upstream.key, upstream.data_type))
        # Meatiest first: an unmapped dropdown carrying 10,000 values is a very
        # different prospect from an unmapped text field carrying none, and
        # alphabetical order hides that completely.
        report.unmapped_theirs.sort(key=lambda row: (-row[2], row[0]))

        for key, gloss in FILTER_ATTRIBUTES.items():
            filter_attr = by_key.get(key)
            report.filter_attributes.append(
                (
                    key,
                    gloss,
                    filter_attr is not None,
                    vocab_theirs.get(filter_attr.pk, 0) if filter_attr else 0,
                    key in matched_upstream_keys,
                )
            )

    return report
