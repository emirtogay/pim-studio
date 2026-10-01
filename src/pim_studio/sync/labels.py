"""Localised vocabulary labels, and which terms have no translation.

Omnitron renders dropdown labels in the tenant default (Turkish) unless asked
otherwise, and the way to ask is the `Accept-Language` header — not a query
parameter. `?locale=pl`, `?language=pl` and `?lang=pl` are all accepted and
silently ignored, so a caller who uses one gets a plausible response in the
wrong language and no error to notice.

The important part of this stage is not the fetching, it is the comparison.
A term with no translation comes back as the Turkish one, identical in shape
to a real translation, so "is this Polish?" is only answerable while the
baseline and the locale response are both in hand. Recording that verdict is
what stops a Turkish label being stored as though it were Polish — the same
class of defect the EN? flag exists to catch, and this module's whole premise
is that filled is not the same as correct.

Measured on the first full pass, weighted by how many product rows actually
carry each term: PL 82%, CZ 76%, SK 88%, HU 76%. `fit` has no CZ or HU
translation at all (7,153 rows), and `fabric_type`, `length` and `waist` have
none in any locale (2,535 rows).

None of that is cosmetic. Where Omnitron holds no Czech label, the Czech
storefront shows Czech shoppers the Turkish one — so an untranslated row here
is a live customer-facing defect, and reporting it is the point.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from ..db import session_scope
from ..logging import log
from ..models import Attribute, AttributeColumn, VocabularyLabel
from ..omnitron.client import OmnitronClient
from .runs import SyncRun

#: Omnitron's locale codes for our four markets. Deliberately not the market
#: codes: Czech is `cs` to Omnitron and `CZ` to us, and Omnitron uses a third
#: spelling again in the content fields (`cze`). Nothing here is derivable.
LOCALES: dict[str, str] = {"PL": "pl", "CZ": "cs", "SK": "sk", "HU": "hu"}

#: Only dropdown-backed attributes have a vocabulary to localise, and this is
#: how the registry says so — a mapped column of the `__label` form. Derived
#: rather than listed, so wiring a new dropdown attribute brings its
#: translations along without touching this file.
_LABEL_SUFFIX = "__label"
_KWARGS_PREFIX = "attributes_kwargs__"

#: Identity attributes are excluded: a code has no language. `color_code` is
#: dropdown-backed and its "labels" are the codes themselves (`132`, `001`),
#: so it contributed 10,362 of 10,824 terms at 0% translated and made the
#: overall figure read 3% when the attributes people actually read sit near
#: 80%. An aggregate dominated by rows that could never be translated is worse
#: than no aggregate.
_SKIP_GROUPS = frozenset({"Identity"})


def _dropdown_attributes() -> dict[int, tuple[str, str]]:
    """Our attribute id -> (our key, Omnitron attribute name)."""
    out: dict[int, tuple[str, str]] = {}
    with session_scope() as db:
        rows = db.execute(
            select(Attribute.id, Attribute.key, AttributeColumn.column_name)
            .join(AttributeColumn, AttributeColumn.attribute_id == Attribute.id)
            .where(
                AttributeColumn.source == "omnitron",
                AttributeColumn.column_name.like(f"{_KWARGS_PREFIX}%{_LABEL_SUFFIX}"),
                Attribute.display_group.notin_(_SKIP_GROUPS),
            )
        )
        for attr_id, key, column in rows:
            name = column[len(_KWARGS_PREFIX) : -len(_LABEL_SUFFIX)]
            out[attr_id] = (key, name)
    return out


def _vocabulary(api: OmnitronClient, pk: int, locale: str | None) -> dict[int, str]:
    """Every value of one Omnitron attribute, keyed by its pk."""
    return {
        rec["pk"]: rec.get("label")
        for rec in api.iter_records(
            "/api/v1/attribute_value/", params={"attribute": pk}, page_size=200,
            locale=locale,
        )
    }


def sync_labels(api: OmnitronClient, run: SyncRun) -> dict[str, Any]:
    """Mirror each dropdown vocabulary in all four market languages."""
    ours = _dropdown_attributes()
    if not ours:
        log.warning("no_dropdown_attributes_mapped")
        return {}

    # Omnitron's own attribute pks, by name.
    remote: dict[str, int] = {}
    for rec in api.iter_records("/api/v1/attributes/", page_size=200):
        if rec.get("key"):
            remote[rec["key"]] = rec["pk"]

    records: list[dict[str, Any]] = []
    translated = defaultdict(int)
    total = defaultdict(int)
    ambiguous_terms = 0
    missing_upstream: list[str] = []

    for attr_id, (_key, name) in sorted(ours.items(), key=lambda kv: kv[1][0]):
        pk = remote.get(name)
        if pk is None:
            missing_upstream.append(name)
            continue
        base = _vocabulary(api, pk, None)
        if not base:
            continue
        run.seen()

        for market, locale in LOCALES.items():
            localised = _vocabulary(api, pk, locale)
            # One baseline label can belong to two Omnitron values — the
            # vocabulary report already counts 20 such duplicates. If they
            # disagree on the translation, which one applies to a product is
            # unknowable from the label alone, so it is recorded as ambiguous
            # rather than resolved by picking a side.
            by_baseline: dict[str, set[str]] = defaultdict(set)
            for value_pk, baseline in base.items():
                if not baseline:
                    continue
                by_baseline[baseline].add(localised.get(value_pk) or baseline)

            for baseline, labels in by_baseline.items():
                ambiguous = len(labels) > 1
                label = sorted(labels)[0] if ambiguous else next(iter(labels))
                is_translated = label != baseline
                total[market] += 1
                if is_translated:
                    translated[market] += 1
                if ambiguous:
                    ambiguous_terms += 1
                records.append(
                    {
                        "attribute_id": attr_id,
                        "baseline": baseline[:255],
                        "locale": locale,
                        "label": label[:255],
                        "translated": is_translated,
                        "ambiguous": ambiguous,
                    }
                )

    # Wholesale replace: this is the vocabulary as of now, and a stale row
    # would claim a translation that may since have been added or removed.
    with session_scope() as db:
        db.query(VocabularyLabel).delete()
        if records:
            db.bulk_insert_mappings(VocabularyLabel, records)

    run.imported(len(records))
    stats: dict[str, Any] = {
        "attributes": len(ours) - len(missing_upstream),
        "rows": len(records),
        "ambiguous": ambiguous_terms,
        **{
            f"translated_{m}": (
                round(100 * translated[m] / total[m]) if total[m] else 0
            )
            for m in LOCALES
        },
    }
    if missing_upstream:
        stats["not_in_omnitron"] = len(missing_upstream)
        log.warning("attributes_not_found_upstream", names=sorted(missing_upstream))
    log.info("labels_synced", **stats)
    return stats
