"""Importing product attribute values — the stage that fills `attribute_values`.

The table has held zero rows since the schema was built. This is what fills it,
from the live API, for our own 26,813 SKUs.

## Two source shapes, one map

`attribute_columns` names the source path verbatim, and two prefixes are
understood:

    attributes__<key>                       the flat per-market attributes
    attributes_kwargs__<key>__label         a dropdown's human-readable label
    localized_attributes__<locale>__<key>   Omnitron's translation store
    market_attributes__<market>__<key>      one flat key, read per market locale

A dropdown arrives twice: `attributes.integration_alt_grup` is `"004"` and
`attributes_kwargs.integration_alt_grup.label` is the category name. Both map
to the same attribute, and the label takes precedence — see `load_mappings`.

Only `l_name` is read from the second, and deliberately so: the localised
*per-attribute copies* are structurally corrupt on this tenant (`urun_adi_pl`
filed under the `en` locale, `urun_adi_cz` under `hu`, `[cs].urun_adi_cz`
disagreeing outright with the flat value). `l_name` is different in kind — it
is Omnitron's core `name` field translated, not a copy of a custom attribute —
and it is the string the storefront most likely renders.

## Locale is a header, and the flat fields obey it

A flat column that is *localizable* holds one value per locale and Omnitron
serves exactly one of them per request — the one named by `Accept-Language`,
or the tenant default when the header is absent. That default view is where
the per-market content columns sit mostly empty, which is why this stage read
`urun_adi_*` as blank for years while the backoffice export (taken with a
locale selected) showed them filled. So the fetch now runs once per locale a
mapping needs, and each mapping resolves against its market's locale chain —
designated locale first, known misfiling locations next, the locale-less
baseline last, so the read is a strict superset of what it always was. See
`LOCALE_CHAINS`, `assign_locale_chains` and `_resolve`.

## What the shop shows, and the key it does not

Some content lives in one key for every market as well as in four suffixed
ones — `integration_kalem_icerik` holds a description in every language at
~100% fill where the `_pol/_cz/_sk/_hu` siblings hold one far less often. The
shops render only the sibling: where it is empty the page shows no description,
however good the shared key's text (example.com/.cz/.sk/.hu, 2026-09-16). So the
sibling alone is the description here, an empty one is a missing cell, and a
cell the sibling no longer fills is cleared rather than left holding whatever
an earlier reading put there.

The shared key is still read — `market_attributes__<market>__<key>`, once per
market locale — but only to be *offered*: where the shop's text is English or
Turkish, or absent, and the shared key holds the market's language, it becomes
a proposal (`proposals.offer_shared_key`). From 2026-08-25 to 0.47.0 it was
stored as the description instead, and the panel showed text no customer could
read — Turkish on 784RMA0950.2A9 while example.com showed English.

## Grain, and the disagreement it exposes

Products arrive at SKU grain. Most content attributes live at `model_color`,
so eight sizes of one polo carry eight copies of one name. Where they agree,
one value is written at model_color grain.

Where they disagree, **nothing is written**. The vault is explicit that the
majority value is usually the un-updated generic one, so picking it would be
worse than abstaining — and writing a value we had to guess at would make the
coverage report confidently wrong. The conflict is counted and logged with its
candidates instead. That is the honest state: not missing, not resolved,
visible.

## The grouping row, read apart

Omnitron keeps one `product_meta` per base code — the object behind a listing
card, `sku` equal to the bare base code, no size, nothing sellable. Until
2026-09-07 it sat inside a ModelColor as a ninth "size", and the conflict
resolver's language tier let its Hungarian outvote eight English sizes; when
`Product.is_size()` dropped it, 5H1299.64I's Hungarian detail became English
overnight with `sync` as the only author. The row was never what the shop
renders — card and page both show the size's text, six of six on example.com
(2026-09-09) — but it is where a translator most naturally typed, so it holds
local text the sizes lack.

So it is read again, on purpose and apart: one fetch per base code, resolved
through the same mappings and locale chains, written under
`entity_kind="base_code"` with the base code as the key. A row of its own can
never contend with a size, and nothing that counts sizes reads it. Its one
consumer is `proposals.GroupingRowSource`, which offers the text back for the
sizes — through a person, and then the push — which is where the shop reads.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..logging import log
from ..models import (
    Attribute,
    AttributeColumn,
    AttributeValue,
    OmnitronAttribute,
    Product,
    ValueChange,
    ValueConflict,
    is_grouping_row,
    utcnow,
)
from ..omnitron.client import OmnitronClient
from ..rules.language import FLAG_THRESHOLD, english_confidence
from . import conflicts, proposals
from .batching import fetch_by_sku_locales
from .runs import SyncRun
from .targets import WriteTargetError
from .writer import upsert_attribute_values

_FLAT = "attributes__"
_LOCALIZED = "localized_attributes__"
#: `attributes_kwargs__<key>__label` — the human label behind a dropdown
#: code. Omnitron sends both: `attributes.integration_alt_grup` is "004"
#: and `attributes_kwargs.integration_alt_grup.label` is the category name.
_KWARGS = "attributes_kwargs__"
_LABEL_SUFFIX = "__label"

#: `market_attributes__<market>__<key>` — the flat `<key>` read under
#: `<market>`'s locale. One upstream key serves all four markets this way, so
#: the market has to appear in the column name: `attribute_columns` is unique
#: on `(source, column_name)`. The market that *routes* the value is still the
#: column's own `market` field, exactly as for every other mapping.
#:
#: This exists for `integration_kalem_icerik`, which holds a full description
#: in every language at ~100% fill (799/800 measured) where the market-suffixed
#: `_pol/_cz/_sk/_hu` columns hold one at ~23% (177-193/800). No shop renders
#: it, so a mapping with this prefix is never written as a live value: what it
#: reads is offered as a proposal for the cell the suffixed column fills. See
#: the module docstring.
_MARKET = "market_attributes__"

#: How many products to read before writing. One batch of SKUs yields at most
#: this many products, and each can produce a value per mapped attribute.
_BATCH = 100

#: The Accept-Language chain each market's flat content is read under, in
#: trust order: the market's own Omnitron locale first, then any locale this
#: tenant demonstrably misfiled content into, then `None` — the locale-less
#: read that was the whole fetch until 2026-08-25, kept as the tail so this
#: stage can only ever see more than it used to, never less.
#:
#: Confirmed against the live tenant on 2026-08-25 (300 26AW products,
#: `scripts/probe_locale.py` + a full-locale scan): the market's own locale
#: holds the names at 96-100% (`urun_adi_pl` 300/300 under `pl`, `_cz` 292
#: under `cs`, `_sk` 289 under `sk`, `_hu` 289 under `hu`) against 41-71% in
#: the baseline view. Czech content also appears under `hu` (the CHANGELOG
#: 0.5.0 misfiling, still live), so `hu` stays in the CZ chain. `en` is
#: deliberately NOT a fallback anywhere: the `en` view holds the English
#: marketing copy, and short brand-heavy names ("Polo N.10.10 Classic Fit")
#: score under the language guard's flag threshold — a first run with `en`
#: chained wrote 124-129 English names into SK/HU before this was measured.
#:
#: The designated codes match `labels.LOCALES` and are not derivable from the
#: market codes (Czech is `cs` here, `CZ` to us, `cze` in the column names).
#: Every distinct named locale across the chains is one extra full fetch pass,
#: so the chains stay short.
LOCALE_CHAINS: dict[str, tuple[str | None, ...]] = {
    "PL": ("pl", None),
    "CZ": ("cs", "hu", None),
    "SK": ("sk", None),
    "HU": ("hu", None),
}

#: The read every mapping starts with: the locale-less baseline, alone. Only
#: `assign_locale_chains` upgrades a mapping beyond it.
_BASELINE_ONLY: tuple[str | None, ...] = (None,)

_HTML_TAG = re.compile(r"<[^>]+>")


@lru_cache(maxsize=8192)
def _english(value: str) -> float:
    """`english_confidence` over the text with its markup stripped.

    Stripped because `description` is HTML and lingua fed tag soup returns
    noise; cached because eight sizes of one polo carry eight copies of one
    name and the detector is the expensive part of this stage.
    """
    return english_confidence(_HTML_TAG.sub(" ", value))


class Mapping:
    """One source path resolved to (attribute, market, grain)."""

    __slots__ = (
        "attribute_id", "grain", "is_label", "is_shared_key", "key",
        "locale", "market", "our_key", "read_locales",
    )

    def __init__(
        self,
        attribute_id: int,
        our_key: str,
        grain: str,
        market: str | None,
        key: str,
        locale: str | None,
        is_label: bool = False,
        is_shared_key: bool = False,
    ) -> None:
        self.attribute_id = attribute_id
        self.our_key = our_key
        self.grain = grain
        self.market = market
        self.key = key
        self.locale = locale
        self.is_label = is_label
        #: One upstream key read per market via its locale. Offered as a
        #: proposal, never written as the live value — no shop renders it.
        self.is_shared_key = is_shared_key
        #: Which fetched views `_resolve` walks, in trust order. `(None,)`
        #: means the baseline view only — exactly the pre-locale behaviour.
        self.read_locales: tuple[str | None, ...] = _BASELINE_ONLY

    def read(self, product: dict[str, Any]) -> str | None:
        if self.is_label:
            entry = (product.get("attributes_kwargs") or {}).get(self.key)
            value = entry.get("label") if isinstance(entry, dict) else None
        elif self.locale is None:
            source = product.get("attributes") or {}
            value = source.get(self.key)
        else:
            source = (product.get("localized_attributes") or {}).get(self.locale) or {}
            value = source.get(self.key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None


def _grain_of(entity_kind: str) -> str:
    """What one written row stands for: a size read, or the style's grouping row."""
    return "base_code" if entity_kind == "base_code" else "sku"


# The grouping-row test used to live here, privately, with a docstring saying
# `sync/catalogue` compared the integer "on a different endpoint". That was
# wrong on the facts — `catalogue._resolve` reads `/api/v1/products/` too — and
# the mistake cost that stage its guard entirely. It is now one function beside
# the constant it interprets: `models.is_grouping_row`.


def load_mappings() -> list[Mapping]:
    """Resolve `attribute_columns` into readable source paths.

    Columns that name neither prefix are product fields or membership data
    (`base_code`, `active_price__price`, `productimage_set__0__image`) and are
    not attribute values — they are skipped here rather than treated as gaps.
    """
    mappings: list[Mapping] = []
    with session_scope() as db:
        rows = db.execute(
            select(AttributeColumn, Attribute)
            .join(Attribute, Attribute.id == AttributeColumn.attribute_id)
            .where(AttributeColumn.source == "omnitron")
        ).all()

        for column, attribute in rows:
            name = column.column_name
            if name.startswith(_FLAT):
                mappings.append(
                    Mapping(
                        attribute.id, attribute.key, attribute.value_grain,
                        column.market, name[len(_FLAT):], None,
                    )
                )
            elif name.startswith(_KWARGS) and name.endswith(_LABEL_SUFFIX):
                key = name[len(_KWARGS) : -len(_LABEL_SUFFIX)]
                mappings.append(
                    Mapping(
                        attribute.id, attribute.key, attribute.value_grain,
                        column.market, key, None, is_label=True,
                    )
                )
            elif name.startswith(_LOCALIZED):
                rest = name[len(_LOCALIZED):]
                locale, _, key = rest.partition("__")
                if not key:
                    continue
                mappings.append(
                    Mapping(
                        attribute.id, attribute.key, attribute.value_grain,
                        column.market, key, locale,
                    )
                )
            elif name.startswith(_MARKET):
                rest = name[len(_MARKET):]
                _market_in_name, _, key = rest.partition("__")
                if not key:
                    continue
                # The market comes from the column, not from the name: the name
                # carries it only to keep `(source, column_name)` unique across
                # the four markets that share one upstream key.
                mappings.append(
                    Mapping(
                        attribute.id, attribute.key, attribute.value_grain,
                        column.market, key, None, is_shared_key=True,
                    )
                )

    # Where a dropdown gives us both a code and its label for the same
    # (attribute, market), the **label wins and the code is dropped**.
    #
    # `integration_alt_grup` is the case: the flat attribute holds "004" and
    # `attributes_kwargs` holds the category name. Both are mapped to
    # `sub_category`, so without a precedence rule they contend for one key,
    # the collision guard fires, and *neither* is written — which is how the
    # panel ended up showing bare numeric codes for category and gender.
    #
    # The label is the right winner because these attributes exist to be read
    # by a human. The code is not lost: it stays in Omnitron and in the mirrored
    # vocabulary, where `omnitron_attribute_values` maps one to the other.
    labelled = {(m.attribute_id, m.market) for m in mappings if m.is_label}
    #
    # A shared key (`market_attributes__`) is kept beside the market-suffixed
    # sibling for the same pair, and the two never contend: the sibling is the
    # live value, the shared key only an offer. Until 0.47.0 the shared key won
    # and the sibling was its fallback, which stored text the shop never shows.
    return [
        m for m in mappings
        if m.is_label or m.is_shared_key or (m.attribute_id, m.market) not in labelled
    ]


def assign_locale_chains(mappings: list[Mapping]) -> tuple[str | None, ...]:
    """Give each market-routed flat mapping its locale chain; return the union.

    Which mappings need more than the baseline read is decided from data, not
    listed: a flat mapping routed to a market whose upstream attribute is
    localizable holds per-locale values, so it gets `LOCALE_CHAINS[market]`.
    The localizability boolean comes from the mirrored registry
    (`omnitron_attributes`, refreshed by the `registry` stage that the
    pipeline orders before this one). An attribute the mirror does not know is
    treated as localizable — the chain ends in the baseline read anyway, so
    the wrong default costs extra requests, never data.

    Labels, market-agnostic flats and `localized_attributes__` mappings stay
    on the baseline: a dropdown label is localised by the labels stage, and a
    value with no market has no locale to prefer.

    Returns every view the fetch must perform — the ordered union of the
    assigned chains, baseline first — which is what `fetch_by_sku_locales`
    takes as its `locales`.
    """
    with session_scope() as db:
        localizable = {
            key: flag
            for key, flag in db.execute(
                select(OmnitronAttribute.key, OmnitronAttribute.is_localizable)
            )
        }

    fetch_locales: list[str | None] = [None]
    for mapping in mappings:
        if mapping.is_label or mapping.locale is not None:
            continue
        chain = LOCALE_CHAINS.get(mapping.market or "")
        if chain is None:
            continue
        # A shared key is read per market *because* it is localizable — that
        # is the whole point of the mapping — so it is chained on the strength
        # of the declaration rather than waiting on the mirror to agree.
        if not mapping.is_shared_key and not localizable.get(mapping.key, True):
            continue
        if mapping.is_shared_key:
            # The market's own locale and nothing else.
            #
            # No baseline tail: for a market-suffixed column the locale-less
            # read still returns that market's own text, but for a key shared
            # across markets it returns the tenant default — Turkish — which is
            # how a Slovak description once read "1933'ten beri".
            #
            # And no misfiling fallback either. `hu` sits in the CZ chain
            # because Czech was once filed under `urun_adi_cz`'s `hu` view; in a
            # key shared across markets the `hu` view is simply the Hungarian
            # text, and offering it as Czech would be a confident lie.
            chain = chain[:1]
        mapping.read_locales = chain
        for locale in chain:
            if locale not in fetch_locales:
                fetch_locales.append(locale)
    return tuple(fetch_locales)


@dataclass(frozen=True)
class Resolved:
    """One mapping's answer for one SKU, and where it came from."""

    value: str
    locale: str | None          # the view it was read from; None = baseline
    designated: bool            # found under the chain's first choice?
    suspect_flag: str | None = None
    suspect_reason: str | None = None


class _LocaleTally:
    """Per (attribute, market) counts of how resolution went.

    This is the stage's answer to the defect that hid the bug for years: a
    value withheld for a *reason* must be countable apart from a value that
    was never there, or the next locale drift reads as the Turkey team not
    doing their job.
    """

    __slots__ = ("counts", "fallback_locales")

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.fallback_locales: Counter[str] = Counter()


def _resolve(
    mapping: Mapping,
    records_by_locale: dict[str | None, dict | None],
    tallies: dict[tuple[str, str], _LocaleTally],
) -> Resolved | None:
    """Pick the one value this mapping holds for this SKU, or say why not.

    Walks the mapping's locale chain in trust order and returns the first
    value that survives two guards, so the conflict resolver downstream never
    sees the same SKU twice for one mapping:

    - **Baseline equality** (named locales only): an untranslated localizable
      value does not come back empty, it comes back as the baseline text —
      `labels.py` semantics. Crediting that echo to the locale would claim a
      translation that does not exist, so the walk moves on; the chain's
      `None` tail still writes the baseline itself, exactly as the pre-locale
      fetch did, and the write is counted `equals_baseline` — filled, and
      known untranslated — rather than blended into `found_baseline`.
    - **Language** (chained mappings only): a value reading as English at
      flag confidence is written-but-flagged when it sits where it belongs
      (the designated locale, or the baseline tail), because filled-and-
      flagged beats hidden. Found while *hunting* under a fallback locale it
      is skipped instead — weak provenance and wrong language together are
      not evidence, they are two defects compounding.

    Mappings still on the baseline-only read return exactly what they always
    did, untallied.
    """
    chain = mapping.read_locales
    if chain == _BASELINE_ONLY:
        value = mapping.read(records_by_locale.get(None) or {})
        return None if value is None else Resolved(value, None, True)

    # A shared key tallies under its own label: it and the sibling both answer
    # for one (attribute, market), and summing them would report more values
    # than there are products.
    label = f"{mapping.our_key}·shared" if mapping.is_shared_key else mapping.our_key
    tally = tallies.setdefault((label, mapping.market or "*"), _LocaleTally())
    baseline = mapping.read(records_by_locale.get(None) or {})
    withheld: str | None = None
    for locale in chain:
        record = records_by_locale.get(locale)
        if record is None:
            continue
        value = mapping.read(record)
        if value is None:
            continue
        if locale is not None and baseline is not None and value == baseline:
            withheld = withheld or "equals_baseline"
            continue
        designated = locale == chain[0]
        confidence = _english(value)
        suspect = confidence >= FLAG_THRESHOLD
        if suspect and not designated and locale is not None:
            withheld = withheld or "suspect_skipped"
            continue
        if designated:
            tally.counts["found_designated"] += 1
        elif locale is None:
            bucket = (
                "equals_baseline" if withheld == "equals_baseline" else "found_baseline"
            )
            tally.counts[bucket] += 1
        else:
            tally.counts["found_fallback"] += 1
            tally.fallback_locales[locale] += 1
        if not suspect:
            return Resolved(value, locale, designated)
        tally.counts["suspect_written"] += 1
        return Resolved(
            value, locale, designated,
            suspect_flag="EN?",
            suspect_reason=(
                f"reads as English ({confidence:.2f}) in a {mapping.market} "
                f"field, found under {locale or 'the locale-less baseline'}"
            ),
        )

    tally.counts[withheld or "empty"] += 1
    return None


def offer_text(by_value: dict[str, list[str]], market: str) -> str | None:
    """The one text a shared key holds for an option, or None if it has several.

    Sizes that agree, or disagree only cosmetically or by language, give an
    answer. A majority vote does not: a live value has to be *something* and
    is flagged for review, but a proposal is optional, and offering the larger
    of two rival texts would put a coin-flip in front of a reviewer as if it
    were the catalogue's answer.
    """
    if len(by_value) == 1:
        return next(iter(by_value))
    settled = conflicts.settle(by_value, market)
    if settled is None or settled.rule == conflicts.MAJORITY:
        return None
    return settled.value


def clear_unrendered(
    db: Session,
    *,
    attribute_ids: set[int],
    routed: set[tuple[int, str]],
    read: set[tuple[str, str]],
    held: set[tuple[str, str, int, str]],
    sync_run_id: int | None = None,
) -> int:
    """Delete live values the key the shop renders does not hold.

    The importer only ever upserts, which is right for a value that is merely
    unchanged and wrong for one that is gone: until 0.47.0 the description came
    from the shared key, and every option whose own market key is empty would
    otherwise keep that text for ever — the very value the shop does not show.
    And the same holds on any night somebody empties the key in Omnitron.

    So, for these attributes only, a `live` row is deleted when this run read
    its entity (`read`) and found nothing for its key (`held` is every key any
    size or grouping row offered a candidate for, conflicts included). A row a
    person owns (`approved`, `exported`) is never touched. Each deletion is
    written to `value_changes` with the text that went — the precedent
    `imports_api.discard` set — so the product timeline shows it.
    """
    if not attribute_ids or not read:
        return 0
    rows = db.scalars(
        select(AttributeValue).where(
            AttributeValue.state_lane == "current",
            AttributeValue.state == "live",
            AttributeValue.attribute_id.in_(attribute_ids),
            AttributeValue.entity_kind.in_(("model_color", "base_code")),
        )
    ).all()
    now = utcnow()
    gone = 0
    for row in rows:
        if (row.entity_kind, row.entity_key) not in read:
            continue
        if (row.attribute_id, row.market) not in routed:
            continue
        if (row.entity_kind, row.entity_key, row.attribute_id, row.market) in held:
            continue
        db.add(
            ValueChange(
                entity_kind=row.entity_kind,
                entity_key=row.entity_key,
                attribute_id=row.attribute_id,
                market=row.market,
                old_value=row.value,
                new_value=None,
                changed_at=now,
                sync_run_id=sync_run_id,
            )
        )
        db.delete(row)
        gone += 1
    if gone:
        db.flush()
        log.info("unrendered_values_cleared", count=gone)
    return gone


def confirm_exported(db: Session, records: list[dict[str, Any]]) -> int:
    """Promote `exported` values that Omnitron now holds as we sent them.

    The push reads a size back the moment it writes, but a value it could not
    confirm then — a size that answered late, a batch that halted after the
    write — stays `exported`. This is the standing confirmation: each night's
    read is compared against every `exported` row, and a match is the
    `exported → live` hop the lifecycle was built around. A mismatch is left
    alone; the writer has already reported it as a divergence.
    """
    upstream = {
        (r["entity_kind"], r["entity_key"], r["attribute_id"], r["market"]): r["value"]
        for r in records
    }
    rows = db.scalars(
        select(AttributeValue)
        .where(AttributeValue.state_lane == "current")
        .where(AttributeValue.state == "exported")
    ).all()
    promoted: list[dict[str, Any]] = []
    for row in rows:
        seen = upstream.get((row.entity_kind, row.entity_key, row.attribute_id, row.market))
        if seen is None or str(seen).strip() != (row.value or "").strip():
            continue
        promoted.append(
            {
                "entity_kind": row.entity_kind,
                "entity_key": row.entity_key,
                "attribute_id": row.attribute_id,
                "market": row.market,
                "state_lane": row.state_lane,
                "value": row.value,
                "state": "live",
                "entry_grain": row.entry_grain,
                "source_kind": row.source_kind,
                "suspect_flag": row.suspect_flag,
                "suspect_reason": row.suspect_reason,
            }
        )
    if promoted:
        # `protect=False`: the whole point is to move a protected row on.
        upsert_attribute_values(db, promoted, protect=False)
        log.info("exported_values_confirmed", count=len(promoted))
    return len(promoted)


def import_values(api: OmnitronClient, run: SyncRun) -> dict[str, Any]:
    """Read our products and write their attribute values.

    Values land in the `current` lane at state `live` — `live` because this is
    what Omnitron actually holds right now, which is precisely what that state
    means. Rows we own at `approved` or `exported` are protected by the writer
    and reported as divergence rather than overwritten.
    """
    mappings = load_mappings()
    if not mappings:
        log.warning("no_mappings")
        return {}
    fetch_locales = assign_locale_chains(mappings)

    with session_scope() as db:
        skus = list(
            db.scalars(
                select(Product.sku)
                .where(Product.is_size())
                .order_by(Product.sku)
            )
        )
        sku_to_mc: dict[str, str] = {
            row[0]: row[1]
            for row in db.execute(
                select(Product.sku, Product.model_color).where(Product.is_size())
            )
        }
        # The grouping rows, one per style. Keyed by base code rather than
        # taken from `products`: the catalogue stage stopped minting them on
        # 2026-09-07, so the table only holds the ones from before, and a
        # style opened since would have no row to read.
        base_codes = sorted(
            {
                code
                for code in db.scalars(
                    select(Product.base_code).where(Product.is_size()).distinct()
                )
                if code
            }
        )

    source_ref = f"sync_run:{run.id}"
    inserted = updated = unchanged = protected = 0
    grain_conflicts = mapping_conflicts = 0

    # (entity_kind, entity_key, attribute_id, market) -> {value: [source, …]}
    #
    # Everything is accumulated before anything is written, and the key here is
    # exactly the table's unique key. That is what makes a collision visible
    # instead of fatal — two rows contending for one key is a question about
    # the data, and answering it with an INSERT is how the importer crashed the
    # first time it ran for real.
    pending: dict[tuple[str, str, int, str], dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    #: How resolution went, per (attribute key, market) — the counters that
    #: make a withheld value distinguishable from a missing one.
    tallies: dict[tuple[str, str], _LocaleTally] = {}
    #: Annotations for the eventual rows, keyed by (pending key, value) so a
    #: ModelColor assembled from eight SKUs keeps one verdict per candidate.
    suspects: dict[tuple[tuple[str, str, int, str], str], tuple[str, str | None]] = {}
    fallback_provenance: dict[tuple[tuple[str, str, int, str], str], str] = {}

    # What the shared keys hold, per ModelColor, kept out of `pending` so it can
    # never become a live value — the shops do not render it. Offered as
    # proposals once the live values are written; see `proposals.offer_shared_key`.
    offers: dict[tuple[str, str, int, str], dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    #: Every (entity_kind, entity_key) this run actually read. A value can only
    #: be cleared for being absent upstream on an entity somebody looked at.
    read: set[tuple[str, str]] = set()

    def absorb(
        sku: str,
        records_by_locale: dict[str | None, dict | None],
        entity: tuple[str, str] | None,
    ) -> None:
        """Resolve every mapping for one fetched record into `pending`.

        `entity` is the (kind, key) a ModelColor-grain value lands under — a
        size's ModelColor, or a grouping row's base code. SKU-grain values
        land under the SKU itself, and a grouping row has none to offer:
        stock, price and size belong to things that can be bought.
        """
        if entity is not None:
            read.add(entity)
        for mapping in mappings:
            resolved = _resolve(mapping, records_by_locale, tallies)
            if resolved is None:
                continue
            market = mapping.market or "*"
            if mapping.grain == "sku":
                if entity is not None and entity[0] == "base_code":
                    continue
                key = ("sku", sku, mapping.attribute_id, market)
            elif entity:
                key = (entity[0], entity[1], mapping.attribute_id, market)
            else:
                continue
            if mapping.is_shared_key:
                # Offered for an option only, read from its own sizes. A
                # proposal is made for an option, not a style, and the grouping
                # row's text already reaches the sizes through GroupingRowSource.
                if key[0] == "model_color":
                    offers[key][resolved.value].append(f"{mapping.key}@{sku}")
                continue
            pending[key][resolved.value].append(
                f"{mapping.key}@{sku}@{resolved.locale or 'base'}"
            )
            if resolved.suspect_flag:
                suspects[(key, resolved.value)] = (
                    resolved.suspect_flag, resolved.suspect_reason,
                )
            if not resolved.designated and resolved.locale is not None:
                fallback_provenance[(key, resolved.value)] = resolved.locale

    for views in fetch_by_sku_locales(api, skus, fetch_locales, batch_size=_BATCH):
        base = views[None]
        run.seen(base.requested)
        for sku in base.found:
            records_by_locale = {loc: view.found.get(sku) for loc, view in views.items()}
            model_color = sku_to_mc.get(sku)
            absorb(
                sku, records_by_locale,
                ("model_color", model_color) if model_color else None,
            )
        if base.missing:
            run.dropped(len(base.missing), reason="sku not found in Omnitron")

    # The grouping rows, after the sizes and apart from them — see the module
    # docstring. Not counted as SKUs read and not dropped when absent: a style
    # without one is a style without one, not a row that went missing.
    grouping_found = 0
    grouping_missing: list[str] = []
    for views in fetch_by_sku_locales(api, base_codes, fetch_locales, batch_size=_BATCH):
        base = views[None]
        for code in base.found:
            record = base.found.get(code)
            if not is_grouping_row(record):
                # Something answered to the base code that is not the grouping
                # row. Left unread rather than filed under the style: a size's
                # text under a base-code key would be a size in disguise again.
                grouping_missing.append(code)
                continue
            grouping_found += 1
            records_by_locale = {loc: view.found.get(code) for loc, view in views.items()}
            absorb(code, records_by_locale, ("base_code", code))
        grouping_missing.extend(base.missing)
    if grouping_missing:
        log.info(
            "grouping_rows_missing",
            requested=len(base_codes), found=grouping_found,
            missing=len(grouping_missing), sample=grouping_missing[:5],
        )

    # Nothing is written until every SKU has been read. A ModelColor whose T4
    # and T7 disagree is only detectable once both are in hand, and they can
    # land in different batches.
    records: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    resolved_by_rule: Counter[str] = Counter()
    for (entity_kind, entity_key, attribute_id, market), by_value in pending.items():
        if len(by_value) > 1:
            # Two distinct sources feeding one key is a registry defect; one
            # source disagreeing with itself across sizes is a data defect.
            # Different causes, different owners, so they are counted apart.
            sources = {s.split("@")[0] for group in by_value.values() for s in group}
            if len(sources) > 1:
                mapping_conflicts += 1
                reason = "two source columns map to one attribute with different values"
                event = "mapping_collision"
            else:
                grain_conflicts += 1
                reason = "model_color disagrees across its own SKUs"
                event = "model_color_value_conflict"
            # A disagreement imports a value anyway, and the judgement calls
            # are flagged for review (2026-08-18, reversing pure abstention:
            # an empty cell beside a full Omnitron column reads as the panel
            # being broken, not as caution). Cosmetic differences resolve
            # silently; the language tier disqualifies English placeholders
            # in local-market fields; majority is the last resort and the
            # tier most worth a human look. Mapping collisions still abstain
            # — two source columns feeding one attribute is a registry bug,
            # and writing through it would mask the bug.
            settled = (
                conflicts.settle(by_value, market)
                if event == "model_color_value_conflict"
                else None
            )
            if settled is not None:
                grain_conflicts -= 1
                resolved_by_rule[settled.rule] += 1
                log.info(
                    "model_color_value_conflict_resolved",
                    entity_kind=entity_kind, entity_key=entity_key,
                    attribute_id=attribute_id, market=market,
                    rule=settled.rule, candidates=settled.candidates,
                )
                key = (entity_kind, entity_key, attribute_id, market)
                flag, flag_reason = suspects.get((key, settled.value), (None, None))
                records.append(
                    {
                        "entity_kind": entity_kind,
                        "entity_key": entity_key,
                        "attribute_id": attribute_id,
                        "market": market,
                        "value": settled.value,
                        "state": "live",
                        "entry_grain": _grain_of(entity_kind),
                        "source_kind": "omnitron_api",
                        "source_ref": f"{source_ref}:{settled.rule}",
                        "suspect_flag": flag,
                        "suspect_reason": flag_reason,
                    }
                )
                if settled.rule in conflicts.JUDGEMENT_RULES:
                    unresolved.append(
                        {
                            "entity_kind": entity_kind,
                            "entity_key": entity_key,
                            "attribute_id": attribute_id,
                            "market": market,
                            "candidates": json.dumps(
                                {v: len(s) for v, s in by_value.items()},
                                ensure_ascii=False,
                            ),
                            "sku_count": sum(len(s) for s in by_value.values()),
                            "sources": ",".join(sorted(sources))[:255],
                            "sync_run_id": run.id,
                            "resolved_value": settled.value,
                            "resolved_rule": settled.rule,
                        }
                    )
                continue

            run.dropped(reason=reason)
            # Recorded, not just logged. An abstention nobody can list is
            # indistinguishable from missing data to anyone reading the panel.
            unresolved.append(
                {
                    "entity_kind": entity_kind,
                    "entity_key": entity_key,
                    "attribute_id": attribute_id,
                    "market": market,
                    "candidates": json.dumps(
                        {v: len(s) for v, s in by_value.items()}, ensure_ascii=False
                    ),
                    "sku_count": sum(len(s) for s in by_value.values()),
                    "sources": ",".join(sorted(sources))[:255],
                    "sync_run_id": run.id,
                }
            )
            log.info(
                event,
                entity_kind=entity_kind, entity_key=entity_key,
                attribute_id=attribute_id, market=market,
                sources=sorted(sources),
                candidates={v: len(s) for v, s in by_value.items()},
            )
            continue
        value = next(iter(by_value))
        key = (entity_kind, entity_key, attribute_id, market)
        flag, flag_reason = suspects.get((key, value), (None, None))
        # A value found under a fallback locale carries that locale in its
        # provenance — `writer` excludes `source_ref` from the change test, so
        # the annotation can never churn a row, only explain one.
        locale = fallback_provenance.get((key, value))
        records.append(
            {
                "entity_kind": entity_kind,
                "entity_key": entity_key,
                "attribute_id": attribute_id,
                "market": market,
                "value": value,
                "state": "live",
                "entry_grain": _grain_of(entity_kind),
                "source_kind": "omnitron_api",
                "source_ref": f"{source_ref}:{locale}" if locale else source_ref,
                "suspect_flag": flag,
                "suspect_reason": flag_reason,
            }
        )

    # Wholesale replace: these are the conflicts as of this run. A row kept
    # from a previous sync would claim a disagreement that may already be
    # fixed upstream, and a queue with stale entries stops being worked.
    #
    # Except where a person has already ruled. An `approved` value is a human
    # decision the writer refuses to overwrite; re-queueing the same
    # disagreement every night would ask them to make it again forever.
    with session_scope() as db:
        decided = {
            (r[0], r[1], r[2], r[3])
            for r in db.execute(
                select(
                    AttributeValue.entity_kind, AttributeValue.entity_key,
                    AttributeValue.attribute_id, AttributeValue.market,
                ).where(
                    AttributeValue.state_lane == "current",
                    AttributeValue.state.in_(("approved", "exported")),
                )
            )
        }
        unresolved = [
            u for u in unresolved
            if (u["entity_kind"], u["entity_key"], u["attribute_id"], u["market"])
            not in decided
        ]
        db.query(ValueConflict).delete()
        if unresolved:
            db.bulk_insert_mappings(ValueConflict, unresolved)

    for start in range(0, len(records), 2000):
        with session_scope() as db:
            result = upsert_attribute_values(db, records[start : start + 2000])
        inserted += result.inserted
        updated += result.updated
        unchanged += result.unchanged
        protected += len(result.protected)
        for divergence in result.protected:
            log.warning("value_diverged_from_ours", detail=str(divergence))

    run.imported(inserted + updated)

    # The read-back the push could not do for itself: an `exported` value
    # that this run read from Omnitron unchanged has landed, and goes `live`.
    with session_scope() as db:
        confirmed = confirm_exported(db, records)

    # For the attributes a shared key is read for, the live value is the
    # rendered key and nothing else — so a value that key no longer holds goes,
    # and the shared key's text is offered where the shop's is wrong or absent.
    offered_attributes = {m.attribute_id for m in mappings if m.is_shared_key}
    with session_scope() as db:
        cleared = clear_unrendered(
            db,
            attribute_ids=offered_attributes,
            routed={
                (m.attribute_id, m.market or "*")
                for m in mappings
                if m.attribute_id in offered_attributes
            },
            read=read,
            held=set(pending),
            sync_run_id=run.id,
        )
    try:
        with session_scope() as db:
            offered = proposals.offer_shared_key(
                db,
                {
                    (entity_key, attribute_id, market): text
                    for (_kind, entity_key, attribute_id, market), by_value in offers.items()
                    if (text := offer_text(by_value, market)) is not None
                },
                offered_attributes,
                sync_run_id=run.id,
            )
    except WriteTargetError as exc:
        # A seed mistake in what may be pushed. The `proposals` stage raises on
        # the same check, loudly; failing *this* stage for it would stop `all`
        # after the live values are already written, over an optional offer.
        log.error("shared_key_offer_skipped", reason=str(exc))
        offered = {}

    # The per-locale accounting, one row per chained (attribute, market).
    # Logged row by row as well as returned: `equals_baseline` suddenly rising
    # in the nightly log is what locale drift looks like from here.
    locale_totals: Counter[str] = Counter()
    locale_coverage: list[dict[str, Any]] = []
    for (our_key, market), tally in sorted(tallies.items()):
        row: dict[str, Any] = {
            "attribute": our_key,
            "market": market,
            "designated": tally.counts["found_designated"],
            "fallback": tally.counts["found_fallback"],
            "baseline": tally.counts["found_baseline"],
            "empty": tally.counts["empty"],
            "equals_baseline": tally.counts["equals_baseline"],
            "suspect_written": tally.counts["suspect_written"],
            "suspect_skipped": tally.counts["suspect_skipped"],
            "fallback_locales": dict(tally.fallback_locales),
        }
        locale_coverage.append(row)
        log.info("locale_value_coverage", **row)
        locale_totals["found_designated"] += tally.counts["found_designated"]
        locale_totals["found_fallback"] += tally.counts["found_fallback"]
        locale_totals["found_baseline"] += tally.counts["found_baseline"]
        locale_totals["empty"] += tally.counts["empty"]
        locale_totals["equals_baseline"] += tally.counts["equals_baseline"]
        locale_totals["suspect_written"] += tally.counts["suspect_written"]
        locale_totals["suspect_skipped"] += tally.counts["suspect_skipped"]

    stats = {
        "mappings": len(mappings),
        "skus": len(skus),
        "fetch_locales": len(fetch_locales),
        "grouping_rows": len(base_codes),
        "grouping_rows_found": grouping_found,
        "grouping_rows_missing": len(grouping_missing),
        "cleared_unrendered": cleared,
        "shared_key_proposed": offered.get("proposed", 0),
        "shared_key_withdrawn": offered.get("withdrawn", 0),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "conflicts": grain_conflicts,
        "mapping_collisions": mapping_conflicts,
        "protected": protected,
        "confirmed": confirmed,
        "resolved_cosmetic": sum(resolved_by_rule.values()),
        **{f"resolved_{rule}": n for rule, n in sorted(resolved_by_rule.items())},
        "locale_found_designated": locale_totals["found_designated"],
        "locale_found_fallback": locale_totals["found_fallback"],
        "locale_found_baseline": locale_totals["found_baseline"],
        "locale_empty": locale_totals["empty"],
        "locale_equals_baseline": locale_totals["equals_baseline"],
        "locale_suspect_written": locale_totals["suspect_written"],
        "locale_suspect_skipped": locale_totals["suspect_skipped"],
    }
    log.info("values_imported", **stats)
    stats["locale_coverage"] = locale_coverage
    return stats
