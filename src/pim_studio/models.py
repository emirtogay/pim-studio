"""Database schema.

The design is settled in the vault (`pim-studio/`, gitignored):
`01 - Vision and Architecture` for the model, `Rules/` for liveness and the ban
list, `Findings/` for the measured facts each table answers to. The short
version:

The centre is not `products` — it's `attribute_values`: one row per
(entity, attribute, market) carrying the value, where it came from, and where
it is in its life (`proposed → approved → exported → live`, with `rejected`
and `stale` as side exits; `missing` and `n/a` are computed absences, never
stored). Attributes are registry rows, not schema columns, because masterdata
brings new fields every season and the Omnitron export is a 16-to-32-column
saved view of a larger set.

Grain: values enter at one of three grains and fan out downward —
base_code (masterdata content) → model_color (colour-specific) → sku
(price/stock, and the Omnitron upload key). The entry grain is recorded so
provenance survives the fan-out.

Market is part of the value key, never a column-name prefix. Omnitron's
market-suffixed columns (`urun_adi_pl`, `urun_detay_pol`, `kalem_icerik_cz`…)
are mapped to (attribute, market) explicitly in `attribute_columns` — the
suffixes are too inconsistent to parse (`_pl`/`_pol`, `_cz`/`_cze`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    or_,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


# Stored in `attribute_values.market` when a value is not market-keyed. See the
# note on `AttributeValue.market` for why this is a sentinel rather than NULL.
MARKET_AGNOSTIC = "*"


class Base(DeclarativeBase):
    pass


# ── Attribute registry ─────────────────────────────────────────────────────


class Attribute(Base):
    """One logical attribute — `product_name`, `fit`, `price`.

    `write_channel` decides which generator an approved value flows into and
    who owns the gap when one is missing:

    - `omnitron_import`: ours; goes into import files keyed on sku.
    - `product_card`:    the Turkey team's; reaches Omnitron via the
                         product-cards file. We propose and request, never
                         upload directly.
    - `read_only`:       derived or system-owned (`attributes_kwargs__*__label`,
                         `active_*`, discount fields). Never exported.

    `value_grain` is where a value for this attribute canonically lives —
    name/description at `model_color` (stored once, fanned out to SKUs on
    export, per Findings/Name vs detail and SKU grain: 18% of options disagree
    across sizes today and that's a defect, not a feature), price and stock at
    `sku`, masterdata-fed content at `base_code`.

    `per_market` says whether the value is keyed by market. Only price varies
    by market among the liveness inputs (confirmed empirically: 15.1% of
    multi-market SKUs differ on price, 0.016% on stock), but names and
    descriptions are per-market by definition.

    `applies_when_subcategory` is a comma-joined list of sub-categories the
    attribute applies to; NULL means it applies to everything. A wallet has no
    sleeve type — and gates must report that as "not applicable", never as
    "missing". (Plain string, not JSON: the predicate language we actually
    need today is a sub-category list, nothing more.)
    """

    __tablename__ = "attributes"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(128))
    data_type: Mapped[str] = mapped_column(String(16), default="text")  # text|html|number|bool
    write_channel: Mapped[str] = mapped_column(String(16))  # omnitron_import|product_card|read_only
    value_grain: Mapped[str] = mapped_column(String(12))  # base_code|model_color|sku
    per_market: Mapped[bool] = mapped_column(Boolean, default=False)
    long_text: Mapped[bool] = mapped_column(Boolean, default=False)
    gates_go_live: Mapped[bool] = mapped_column(Boolean, default=False)
    applies_when_subcategory: Mapped[str | None] = mapped_column(Text, default=None)
    display_group: Mapped[str] = mapped_column(String(64), default="Other")
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    columns: Mapped[list[AttributeColumn]] = relationship(back_populates="attribute")
    vocabulary: Mapped[list[VocabularyOption]] = relationship(back_populates="attribute")


class AttributeColumn(Base):
    """Explicit mapping from a source column to (attribute, market).

    One row per literal column string per source system — e.g.
    `attributes__urun_detay_pol` → (product_detail, PL) for source `omnitron`.
    The column string is the round-trip contract: import files must carry
    exactly these names, so they are stored verbatim and never derived.

    `market` NULL means the column is market-agnostic (global fields like
    `attributes__PL_sezon`, which despite the name is populated identically
    across all four markets — see Findings/Akinon EE products table).
    """

    __tablename__ = "attribute_columns"
    __table_args__ = (
        UniqueConstraint("source", "column_name", name="uq_attr_col_source_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))  # omnitron|product_card|masterdata_*
    column_name: Mapped[str] = mapped_column(String(128))
    market: Mapped[str | None] = mapped_column(String(4), default=None)  # PL|CZ|SK|HU|UA
    # Read from exports but never written into generated files (labels,
    # active_* derivatives).
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    # A push may write this column's Omnitron key for this column's market.
    # Per column, not per attribute, because `description` reads from the
    # shared `integration_kalem_icerik` under four locales *and* from the
    # market-suffixed siblings, and only the siblings — the keys the four
    # storefronts render, each naming its market — are safe to write. The set
    # is owned by `seed.API_WRITABLE_COLUMNS`; `sync/targets.py` is the only
    # reader, and nothing outside the flagged columns can reach a payload.
    api_writable: Mapped[bool] = mapped_column(Boolean, default=False)

    attribute: Mapped[Attribute] = relationship(back_populates="columns")


class VocabularyOption(Base):
    """One allowed value for a vocabulary-backed attribute.

    Seeded from the PIM's own attribute sheet where possible, then grown from
    data: a value seen in an export or masterdata file that isn't on the list
    lands as `candidate` for someone to promote or reject. This is what keeps
    `OUTLET`/`Outlet` from becoming two seasons — matching is done
    case-insensitively against this table, and the UI offers the list instead
    of a free text box.
    """

    __tablename__ = "vocabulary_options"
    __table_args__ = (
        UniqueConstraint("attribute_id", "value", name="uq_vocab_attr_value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    value: Mapped[str] = mapped_column(String(255))
    # approved|candidate|rejected
    status: Mapped[str] = mapped_column(String(12), default="candidate")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_from: Mapped[str | None] = mapped_column(String(128), default=None)

    attribute: Mapped[Attribute] = relationship(back_populates="vocabulary")


# ── Omnitron mirror ────────────────────────────────────────────────────────
#
# A faithful copy of Omnitron's own registry, kept separate from the curated
# tables above on purpose. Ours carry decisions Omnitron knows nothing about —
# `write_channel`, `value_grain`, `gates_go_live`, `applies_when_subcategory` —
# and a sync that merged into them would overwrite judgement with upstream
# facts. Reconciling the two is a report (and sometimes a conversation), not an
# UPDATE.
#
# Three rules hold across every table here:
#
# `pk` is Omnitron's own id, used as our primary key. A mirror has no identity
# of its own, and borrowing theirs makes the upsert a plain primary-key match.
#
# There are **no foreign keys between mirror tables**, deliberately. Their
# referential integrity is theirs to keep. If we declared it, syncing values
# before attributes would fail outright and a partially-completed sync would
# leave the mirror unwritable rather than merely incomplete. Inconsistencies
# are something the reconciliation report should surface, not something an
# insert should refuse.
#
# `synced_at` records when we last saw the row, which is how a stale mirror
# becomes visible rather than merely wrong.


class OmnitronAttribute(Base):
    """One row of `GET /api/v1/attributes/` — 149 of them on 2026-08-14.

    Against the 26 we hand-seeded. `erp_code` equalled `key` on all seven of
    the filter attributes checked, and is the join key our `attribute_columns`
    table has been approximating.

    `data_type` is stored as the raw string Omnitron sends. The values actually
    in use are `text` (91), `dropdown` (42), `area` (8), `bool` (7) and `image`
    (1) — note that `area` and `bool` are not what Akinon's documentation lists
    (`text_area`, `boolean`). Mapping onto our own `text|html|number|bool`
    vocabulary is the reconciliation report's job, and it has to work from what
    the tenant sends rather than what the docs promise.
    """

    __tablename__ = "omnitron_attributes"

    pk: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    data_type: Mapped[str] = mapped_column(String(32), index=True)
    erp_code: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    # An int in the live data, not a string — it references an attribute value
    # rather than holding one. Non-null on 6 of 149.
    default_value: Mapped[int | None] = mapped_column(Integer, default=None)

    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=False)
    is_searchable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_filterable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_variant: Mapped[bool] = mapped_column(Boolean, default=False)
    is_variant_listable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_form_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_form_field_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_offer: Mapped[bool] = mapped_column(Boolean, default=False)
    pre_attribute: Mapped[bool] = mapped_column(Boolean, default=False)
    # False on all seven filter attributes, true on the content ones. This is
    # half of the two-sources-for-one-fact problem: an attribute that is
    # localizable can hold per-language values *and* have market-suffixed
    # siblings like `urun_detay_pol`.
    is_localizable: Mapped[bool] = mapped_column(Boolean, default=False)

    entity_type: Mapped[int | None] = mapped_column(Integer, default=None)
    created_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    modified_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OmnitronAttributeValue(Base):
    """One allowed option for an Omnitron attribute — their vocabulary.

    20,382 rows on 2026-08-14, against the 38 in our `vocabulary_options`.

    `label` is the translatable display text and `value` is the underlying
    code; both are unique upstream. Their vocabulary is not filter-ready — 105
    values across the seven filter attributes collapse to 85 distinct labels,
    `desen` merges two numbering schemes, and the collar list contains a fit —
    which is what the vocabulary coverage report exists to surface.
    """

    __tablename__ = "omnitron_attribute_values"
    __table_args__ = (Index("ix_omni_value_attr_value", "attribute", "value"),)

    pk: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    # Omnitron's attribute pk. Intentionally not a ForeignKey — see the note at
    # the top of this section.
    attribute: Mapped[int] = mapped_column(Integer, index=True)
    value: Mapped[str | None] = mapped_column(String(255), default=None)
    label: Mapped[str | None] = mapped_column(Text, default=None)
    order: Mapped[int | None] = mapped_column(Integer, default=None)
    erp_code: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    created_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    modified_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OmnitronAttributeSet(Base):
    """A named group of attributes for a product family — 25 of them."""

    __tablename__ = "omnitron_attribute_sets"

    pk: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))
    attribute_set_type: Mapped[str | None] = mapped_column(String(32), default=None)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OmnitronAttributeConfig(Base):
    """The join between a set and an attribute, carrying per-set overrides.

    795 rows. This is the object that answers "is this attribute expected for
    this kind of product" — an attribute can be required in one set and
    optional in another. Our registry approximates it today with a
    hand-maintained `applies_when_subcategory` list, which is why syncing this
    matters more than its row count suggests: a wallet has no sleeve type, and
    a gate that reports one as missing buries the sweatshirts that genuinely
    are.

    The set endpoint embeds these under `attributeconfig_set`, so syncing sets
    yields configs for free; the standalone endpoint exists as well and is the
    simpler read.
    """

    __tablename__ = "omnitron_attribute_configs"
    __table_args__ = (Index("ix_omni_config_set_attr", "attribute_set", "attribute"),)

    pk: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    attribute: Mapped[int] = mapped_column(Integer, index=True)
    attribute_set: Mapped[int] = mapped_column(Integer, index=True)

    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=False)
    is_searchable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_filterable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_variant: Mapped[bool] = mapped_column(Boolean, default=False)
    is_variant_listable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_form_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_form_field_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_localizable: Mapped[bool] = mapped_column(Boolean, default=False)

    order: Mapped[int | None] = mapped_column(Integer, default=None)
    attribute_config_group: Mapped[int | None] = mapped_column(Integer, default=None)
    # A JSON list upstream (e.g. ["is_localizable", "is_offer"]). Stored as the
    # raw JSON string: nothing reads it yet, and inventing a normalised shape
    # for a field we do not use would be guessing at its meaning.
    default_fields: Mapped[str | None] = mapped_column(Text, default=None)

    created_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    modified_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ── Products ───────────────────────────────────────────────────────────────


#: Omnitron's `product_type` for a **product_meta** — the grouping object that
#: stands for a style on a listing page. There is exactly one per base code
#: (18,431 in the tenant on 2026-09-07), its `sku` is the bare base code, and
#: it is not a size anyone can buy.
#:
#: It is nevertheless a catalogue item, so `sync.catalogue` used to mint a
#: `Product` row for it like any other. The damage was not cosmetic: its
#: `integration_base_code_format` names one arbitrary colour and size
#: (`TH2312.166.T5`), so the row landed inside a real ModelColor as a duplicate
#: of a real size, and any attribute only *it* carried became that
#: ModelColor's. `PL_comment` is the one that mattered — 1,019 metas carry a
#: legacy `REMOVED` — and since `pl_comment` is registered at model_color grain
#: there was no disagreement for `sync.conflicts` to settle: the meta's value
#: was the only candidate and won unopposed. See
#: `Findings/Storefront reconciliation`.
PRODUCT_META = 1


def is_grouping_row(record: Mapping[str, Any] | None) -> bool:
    """Whether a product record Omnitron returned is a grouping row, not a size.

    **Compared as text, and that is the whole point.** `/api/v1/products/`
    returns `product_type` as the string `"1"` (verified live, 2026-09-09), and
    `PRODUCT_META` above is the integer `1`. `"1" == 1` is `False`, so a check
    written the obvious way rejects every grouping row in the tenant and reads,
    in every count it reports, exactly like a tenant that has none.

    That is not hypothetical. `sync/values` was fixed for it on 2026-09-09 and
    `sync/catalogue` was not, on the reasoning that catalogue compared "on a
    different endpoint" — it does not; `catalogue._resolve` reads
    `/api/v1/products/` too. The guard there had therefore never fired once:
    across every recorded `omnitron_catalogue` run the "product_meta is a
    grouping row" drop count is zero, in a tenant holding ~18,431 of them.

    Which is why this lives here, beside the constant, rather than being spelled
    out at each call site. The same argument `locks.PUBLISH` makes: two literals
    that agree today are a place for them to stop agreeing later, and this pair
    already did.

    `str()` on both sides rather than `int()` on one: `int(None)` raises and
    `int("")` raises, and a record missing the field entirely is the ordinary
    case for an endpoint that does not return it — which must read as "not a
    grouping row", not as an error.
    """
    if not isinstance(record, Mapping):
        return False
    return str(record.get("product_type")) == str(PRODUCT_META)


class Product(Base):
    """One SKU — the finest grain, and Omnitron's upload key.

    Identity only; everything descriptive lives in `attribute_values`.
    `model_color` and `base_code` are derived once at import (strip
    `.T<size>`; uppercase; the normaliser handles the dot/underscore/
    concatenated/two-column identifier variants) and stored so every rollup
    is a plain GROUP BY.

    Not every row is a size: see `PRODUCT_META` and `Product.is_size`. Anything
    that counts options, judges liveness or reads values must filter on
    `is_size()`; only the two stages that maintain identity — the product
    refresh and the deletion feed — read the table whole.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    base_code_format: Mapped[str] = mapped_column(String(32), index=True)  # MCS, dotted
    model_color: Mapped[str] = mapped_column(String(24), index=True)
    base_code: Mapped[str] = mapped_column(String(16), index=True)
    size: Mapped[str | None] = mapped_column(String(12), default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set when the API's deleted_items feed says the product is gone — never
    # from absence. Absence from an export is a saved view; absence from a
    # batched read can be a partial failure. `deleted_items` means deleted.
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # ── from the live API (NULL until the first product refresh) ──────────
    #
    # `omnitron_pk` is Omnitron's own product id, and the join key for every
    # pk-keyed resource: `deleted_items.object_id`, `catalog_items.product`,
    # `product_image.product`. Stored so later stages resolve pk-keyed rows
    # with a lookup instead of an API call per product.
    omnitron_pk: Mapped[int | None] = mapped_column(Integer, index=True, default=None)
    # Their global switch, not per-channel: false removes the product from
    # every channel at once. Recorded as seen; NULL means "not yet read".
    omnitron_is_active: Mapped[bool | None] = mapped_column(Boolean, default=None)
    # Their modification clock, kept so a later incremental refresh can ask
    # "changed since we last looked" per product rather than per run.
    omnitron_modified_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    #: `0` for a real product, `PRODUCT_META` for a grouping row. NULL means
    #: not yet read, which is why `is_size()` treats NULL as a size: a table
    #: that has never been refreshed must not empty the panel.
    omnitron_product_type: Mapped[int | None] = mapped_column(Integer, default=None)

    @classmethod
    def is_size(cls) -> ColumnElement[bool]:
        """The filter every reader of this table wants, in one place.

        A product we still hold and that is a size someone can buy. Spelling
        the two halves out at each call site is how the first half
        (`delisted_at`) ended up on five queries and off three, and the second
        half has a worse failure mode: a missing `delisted_at` shows a product
        that has gone, while a missing meta filter invents 4,449 options that
        never existed and lets one of them ban a real one.
        """
        return and_(
            cls.delisted_at.is_(None),
            or_(
                cls.omnitron_product_type.is_(None),
                cls.omnitron_product_type != PRODUCT_META,
            ),
        )


class CatalogueMembership(Base):
    """One product's presence in one market's catalogue.

    This is liveness rule 5 — "in the catalog" — which `rules/liveness.py`
    currently abstains on because no export ever showed it. It is also what
    now *defines* our product set: the union of these rows is the set of
    products we track, replacing a frozen Check_list export that held 26,813
    SKUs against PL's actual 51,914 (decided 2026-08-14).

    Keyed per SKU rather than per style, because membership genuinely varies
    within a base code: `sku=4677500` is absent from the PL and CZ catalogues
    while its sibling `sku=2001212` is in both.

    `left_at` records a product dropping out of a catalogue. That is a real
    commercial event — the product stopped being offered in that market — and
    is deliberately *not* the same as `Product.delisted_at`, which means
    Omnitron deleted the product outright. A product can leave PL, stay in CZ,
    and be deleted in neither.
    """

    __tablename__ = "catalogue_memberships"
    __table_args__ = (
        UniqueConstraint("product_id", "market", name="uq_membership_product_market"),
        Index("ix_membership_market", "market", "left_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    market: Mapped[str] = mapped_column(String(4))
    #: The Omnitron catalogue this membership was read from — recorded so a
    #: repointed catalogue is visible as data rather than inferred from the
    #: market code.
    catalog_pk: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class CatalogueTakedown(Base):
    """One "take this offline" somebody pressed: what, where, and how it ended.

    Taking a product off a market's website means deleting its `catalog_items`
    rows upstream — there is no per-market product flag, and `is_active` would
    remove it from every channel including Turkey's. Those rows are the only
    record that the product was ever in the catalogue, so once they are gone
    the information needed to put them back exists nowhere but here. That is
    what this table is for: it is the undo, not the log.

    **The grain is an option, and the blast radius is a group of markets.**
    A person acts on one `model_color`; the deletes land per SKU and per
    catalogue underneath. `markets` records which markets the group covered
    *as computed at the time* rather than being re-derived on read. The group
    was PL and CZ until HU and SK joined them on catalogue 601 (2026-09-16); a
    row written before that still says what that takedown actually reached.

    `status` is `planned|removing|done|partial|failed|restored`. `partial` is
    the one that matters: a takedown that deleted six of a product's eight
    sizes has left it half-selling, which is a worse state than either end and
    the reason the count columns are here rather than derived.
    """

    __tablename__ = "catalogue_takedowns"
    __table_args__ = (Index("ix_takedown_model_color", "model_color", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_color: Mapped[str] = mapped_column(String(24), index=True)
    #: Comma-joined market codes, in `scope.MARKET_CODES` order — "PL,CZ".
    markets: Mapped[str] = mapped_column(String(32))
    #: Comma-joined catalogue pks the takedown was allowed to touch.
    catalogs: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(10), default="planned", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: Set when every removal has been put back, so the panel can say "restored"
    #: rather than leaving a done takedown looking current.
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    restored_by: Mapped[str | None] = mapped_column(String(255), default=None)
    planned_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, default=None)


class CatalogueRemoval(Base):
    """One `catalog_items` row a takedown deleted — and how to put it back.

    `omnitron_product_pk` and `catalog_pk` are stored flat rather than reached
    through `product_id`, deliberately. A restore has to work after the product
    row has been delisted, renamed or re-imported, and the two numbers Omnitron
    needs are exactly these; making the undo depend on a join is making it
    depend on the part of our data most likely to have moved. `catalog_item_pk`
    is recorded too, though a restore cannot reuse it — Omnitron mints a new
    one — because "which row did we delete" is the question an upstream
    argument turns on.
    """

    __tablename__ = "catalogue_removals"
    __table_args__ = (
        Index("ix_removal_takedown", "takedown_id", "state"),
        Index("ix_removal_product", "omnitron_product_pk", "catalog_pk"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    takedown_id: Mapped[int] = mapped_column(ForeignKey("catalogue_takedowns.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), default=None)
    sku: Mapped[str] = mapped_column(String(32))
    omnitron_product_pk: Mapped[int] = mapped_column(Integer)
    catalog_pk: Mapped[int] = mapped_column(Integer)
    catalog_item_pk: Mapped[int | None] = mapped_column(Integer, default=None)
    #: planned|removed|failed|restored
    state: Mapped[str] = mapped_column(String(10), default="planned")
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class StorefrontListing(Base):
    """One option observed on sale on one market's public storefront.

    The only source in this system that is not a report about the shop — it is
    the shop. Every other answer to "is this live" is derived from Omnitron and
    can be wrong in ways nothing downstream would notice: on 2026-09-07 the
    panel and the Looker control table disagreed with the PL site by 661 and
    597 options respectively, and agreed with *each other*, because both read
    the catalogue of a channel that had been switched off. A third source with
    no plumbing in common is what broke that open.

    **Grain is ModelColor, and this is the one place liveness is not per SKU.**
    A listing page shows one card per option; it does not say which sizes are
    behind it. `Rules/Liveness` is right that liveness is a size-level fact —
    this table simply cannot answer at that grain, and pretending otherwise by
    fanning a listing out over an option's sizes would invent observations
    nobody made.

    **No foreign key to `products`, deliberately.** 57 options were selling in
    PL that our tables did not hold at all. A foreign key would have dropped
    exactly the rows worth seeing, and the drop would have looked like the
    storefront having fewer products rather than us having fewer.

    `left_at` is what makes the daily movement readable: 7 options left the PL
    listing in the two hours either side of 13:00 on 2026-09-07, which is the
    churn a merchandiser wants by name rather than as a net total.
    """

    __tablename__ = "storefront_listings"
    __table_args__ = (
        UniqueConstraint("model_color", "market", name="uq_listing_option_market"),
        Index("ix_listing_market", "market", "left_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    model_color: Mapped[str] = mapped_column(String(24), index=True)
    market: Mapped[str] = mapped_column(String(4))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: When the pull that last saw it ran. This is the `as of` on every number
    #: derived from this table — a live count with no time on it is a count
    #: nobody can check, which is how a figure 18 days old came to be read as
    #: today's.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# ── Attribute values ───────────────────────────────────────────────────────


class AttributeValue(Base):
    """One value of one attribute for one entity (at its entry grain) and,
    where applicable, one market. The centre of the system.

    Lifecycle (`state`):

        proposed → approved → exported → live
                ↘ rejected                ↘ stale

    `missing` and `not applicable` are computed, never stored — an absent row
    is missing (or n/a per the registry's applies_when), and storing them
    would make every import a full-matrix write.

    The `exported` → `live` hop exists because publishing happens by file
    upload with no acknowledgement: `exported` means "went out in a batch",
    `live` means "a later export confirmed it landed". Without the split, a
    rejected upload is indistinguishable from one that never happened and the
    same fix gets regenerated forever.

    Provenance: `source_kind` (omnitron_export|masterdata|brandlibrary|manual),
    `source_ref` (filename, run id, or user), and `entry_grain` — a value
    imported at base_code grain and fanned out still says so.

    `suspect_flag`/`suspect_reason` annotate a value that is present but
    defective (wrong language, off-vocabulary, out-of-range) without touching
    its lifecycle state: "live but in the wrong language" is both facts at
    once, and a not-null check must never launder it into "fine".
    """

    __tablename__ = "attribute_values"
    __table_args__ = (
        UniqueConstraint(
            "entity_kind", "entity_key", "attribute_id", "market", "state_lane",
            name="uq_value_entity_attr_market_lane",
        ),
        Index("ix_values_attr_state", "attribute_id", "state"),
        Index("ix_values_entity", "entity_kind", "entity_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(12))  # base_code|model_color|sku
    entity_key: Mapped[str] = mapped_column(String(32))
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    # `*` — not NULL — for a market-agnostic value. The unique constraint below
    # includes this column, and SQL treats NULLs as distinct, so a nullable
    # market would leave the one-row-per-key invariant unenforced for every
    # `per_market=False` attribute — 19 of the 26 in the registry. A partial
    # index restores the constraint but not `ON CONFLICT`, which still won't
    # match NULL rows, so the importer would need two write paths. The sentinel
    # keeps both the constraint and the upsert uniform.
    #
    # `attribute_columns.market` stays nullable: there, NULL means the source
    # column carries no market, which the importer resolves to `*`. The two
    # nullabilities are deliberate and not the same question.
    market: Mapped[str] = mapped_column(String(4), default=MARKET_AGNOSTIC)

    # Two lanes per key: `current` (what the upstream system has / what we
    # approved) and `proposal` (an unaccepted candidate). A proposal accepted
    # becomes the current lane's value; a proposal rejected stays on record in
    # its lane with state=rejected. This keeps "current vs proposed" a
    # two-row read instead of a history reconstruction.
    state_lane: Mapped[str] = mapped_column(String(8), default="current")  # current|proposal

    value: Mapped[str | None] = mapped_column(Text, default=None)
    # proposed|approved|exported|live|rejected|stale
    state: Mapped[str] = mapped_column(String(10), index=True)
    entry_grain: Mapped[str] = mapped_column(String(12))
    source_kind: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    suspect_flag: Mapped[str | None] = mapped_column(String(12), default=None)
    suspect_reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), default=None)


# ── Ban list ───────────────────────────────────────────────────────────────


class ValueRetirement(Base):
    """A value we held and upstream no longer does. Append-only.

    When a full pass proves Omnitron has dropped a price or a stock row, the
    `attribute_values` row is deleted — because the model's central invariant is
    that an absent row *is* the absence ("`missing` and `not applicable` are
    computed, never stored"). Keeping a retired row behind a `retired_at` flag
    would break that: every read everywhere would have to remember to filter it
    out, and the one that forgot would silently count sold-out products as live.
    That is the same shape as the departure-sweep bug, moved into the read path.

    So the value goes and the *event* is recorded here instead. Deleting without
    this table was the state until 2026-08-20, and it made a real question
    unanswerable: the run could say "retired 500 rows" and nothing could say
    which 500. A count is a number; this is a work queue.

    Append-only on purpose. A product whose stock returns and goes again is two
    events, not one row edited twice — "how often does this line sell out" is a
    question about the sequence.

    Not the Phase 9 snapshot, and not a substitute for it. This records only
    disappearance, and only for the attributes the liveness sync owns.
    """

    __tablename__ = "value_retirements"
    __table_args__ = (
        # "What went dark last night" is the query this table exists for.
        Index("ix_retirement_when", "retired_at"),
        # "Why is this product not live any more" — the per-product lookup the
        # panel does when someone opens one.
        Index("ix_retirement_entity", "entity_kind", "entity_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(12))
    entity_key: Mapped[str] = mapped_column(String(32))
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    market: Mapped[str] = mapped_column(String(4))
    #: What it held when we last saw it. The whole point — without it a
    #: retirement says something vanished but not what was lost, and "it had
    #: one unit left" and "it had two hundred" are different events.
    last_value: Mapped[str | None] = mapped_column(Text, default=None)
    retired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: Which run did it, so a suspicious night is traceable to its own log.
    sync_run_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    reason: Mapped[str] = mapped_column(String(64))


class ValueChange(Base):
    """A value we held that became a different value. Append-only.

    The sibling of `ValueRetirement`, and the two are only worth reading
    together: that one records a value *disappearing*, this one records it being
    *replaced*. Between them every way a value can stop being what it was is on
    the record, and neither can answer the other's question.

    Written by `upsert_attribute_values` at the moment it decides a row has
    changed — where the old value and the new one are both in hand and the old
    one was, until now, simply overwritten and lost. `state_lane` is
    `current|proposal` with no history lane, so before this table "what did this
    say last week" was unanswerable by anything in the system.

    ## What is deliberately not here

    **Insertions.** A value appearing for the first time has no "before", so it
    is not a change; and recording them would make the first sync against an
    empty database write 772,450 rows to say nothing happened. A product's first
    name arriving is a real event, but it is a different one, and putting it here
    would mean every reader had to filter on `old_value IS NOT NULL` to get the
    question they actually asked.

    **Everything except `value`.** `MATERIAL_FIELDS` also covers `state`,
    `entry_grain`, `source_kind` and the suspect flags. A lifecycle hop is
    already an audit-log event and belongs there; this table answers one
    question — what did this say, and what does it say now.

    **Refusals.** When upstream tries to overwrite an `approved` value the
    writer declines and records a `Divergence`. Nothing changed, so nothing is
    recorded here. That the attempt happened is worth knowing and is worth its
    own place; it is not history of the value.

    Append-only for the same reason `ValueRetirement` is: a value that goes
    A → B → A is three facts and a sequence, not one row edited twice.
    """

    __tablename__ = "value_changes"
    __table_args__ = (
        # "What changed last night" — the query across all products.
        Index("ix_change_when", "changed_at"),
        # "What changed on this product" — the per-product timeline, which is
        # the reason the table exists. Ordered by time within the entity so the
        # page's query is a range scan rather than a sort.
        Index("ix_change_entity", "entity_kind", "entity_key", "changed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(12))
    entity_key: Mapped[str] = mapped_column(String(32))
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    market: Mapped[str] = mapped_column(String(4))
    #: What it said before. Nullable because a row can hold an empty value and
    #: still be evidence the column was read — the distinction the coverage
    #: module turns on — so "was blank, now says X" is a change worth recording
    #: and must not be confused with an insertion.
    old_value: Mapped[str | None] = mapped_column(Text, default=None)
    new_value: Mapped[str | None] = mapped_column(Text, default=None)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    #: Which run did it, so a night that changed 40,000 values is traceable to
    #: its own log rather than merely alarming.
    sync_run_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    #: A person, when it was not a sync. `updated_by` on the value row is
    #: overwritten by the next write; this keeps who did what at the time.
    changed_by: Mapped[str | None] = mapped_column(String(255), default=None)


class OverviewSnapshot(Base):
    """The funnel's shape on one day, so the next week can be compared to it.

    The first piece of the Phase 9 snapshot `ValueRetirement` says it is not.
    Scoped deliberately narrow: the Overview page's own aggregates, not a
    general history of every value. "What changed since last week" is one
    question, and answering it does not require being able to answer every
    question about the past — a per-value history would be four orders of
    magnitude larger and is a different design problem.

    One row per date, upserted. The nightly build writes it, and a manual
    rebuild the same day refines that row rather than adding a second one:
    two snapshots of one day would make "a week ago" ambiguous, and the page
    would silently pick whichever the query happened to sort first.

    `payload` is JSON in a Text column rather than JSONB. The mirror runs on
    SQLite in development and Postgres in production, and this table is only
    ever read whole, by date — nothing queries into it, so JSONB would buy an
    index nobody uses at the cost of two code paths. Same call, same reason,
    as `ValueConflict`.

    `version` guards the comparison. When the funnel's definition changes, an
    old payload is not a baseline — it is a differently-shaped object whose
    numbers happen to have the same names. A version mismatch reads as "no
    comparable baseline" and the page says so, rather than reporting a delta
    that is really a definition change.
    """

    __tablename__ = "overview_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The day the snapshot describes, not the moment it was taken — the
    #: baseline lookup asks "a week ago", which is a date question.
    snapshot_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    payload: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BanComment(Base):
    """A `PL_comment` value that blocks a product (liveness rule 6).

    A stored list, not a keyword regex: the field has exactly ten distinct
    values today, `FR`/`TR` are market codes that must not ban, and a regex on
    "remov" would misfire on the first "do not remove before AW27". Matching
    is case-insensitive and trimmed. Comment values seen in data but on
    neither list are surfaced for classification — unknown means "someone
    decide", not "fine". See Rules/Ban list.
    """

    __tablename__ = "ban_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    comment: Mapped[str] = mapped_column(String(255), unique=True)
    bans: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ── Runs ───────────────────────────────────────────────────────────────────


class SyncRun(Base):
    """One ingest — an uploaded export, a masterdata import, later an API
    sync. Mirrors the price app's `scrape_runs` shape so the ops habits
    (status page, staleness alerts) carry over.

    `mode` is the difference between a run that is *entitled to conclude
    absence* and one that is not. A `full` pass reads every row upstream holds,
    so anything it did not see is genuinely gone and the reconciliation sweeps
    may run. An `incremental` pass reads only what changed since the watermark,
    so absence means nothing at all — a product removed from a catalogue and a
    product simply unmodified look identical. The sweeps are therefore refused
    in incremental mode rather than skipped quietly.

    Recording it matters after the fact, not just during: without this column
    an incremental run and a full reconciliation are both a green row on the
    runs page, and "the catalogue was last genuinely reconciled six weeks ago"
    becomes unanswerable.
    """

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)  # omnitron_export|masterdata|...
    source_file: Mapped[str | None] = mapped_column(String(255), default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # running|success|partial|failed
    status: Mapped[str] = mapped_column(String(10), default="running")
    # full|incremental — see the class docstring. Existing rows backfill to
    # `full`, which is what they were: nothing incremental existed before.
    mode: Mapped[str] = mapped_column(String(12), default="full")
    rows_seen: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_dropped: Mapped[int] = mapped_column(Integer, default=0)
    # Rows removed because a full pass proved upstream no longer holds them.
    # Separate from `rows_dropped`, which means "read but deliberately not
    # written" — conflating the two would hide a mass deletion inside a number
    # that routinely sits in the thousands for benign reasons.
    rows_retired: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, default=None)
    uploaded_by: Mapped[str | None] = mapped_column(String(255), default=None)


class VocabularyLabel(Base):
    """One dropdown value's label in one market's language.

    Omnitron localises vocabulary through the `Accept-Language` header — not a
    query parameter, which is silently ignored. `Kısa Kollu` comes back as
    `Krótki Rękaw` under `pl`, and the attribute's own display name localises
    too (`Kol Boyu` -> `Rękaw`).

    **`translated` is the point of this table.** When a term has no translation
    Omnitron returns the Turkish one, indistinguishable in shape from a real
    translation. Storing that as though it were Polish would record a
    wrong-language value as correct — the exact defect the EN? flag exists to
    catch — so each locale is compared against the baseline and only a genuine
    difference counts. Measured on the first pass: roughly a fifth of terms are
    untranslated, `fit` is absent in CZ and HU entirely, and `fabric_type`,
    `length` and `waist` have no translation in any locale.

    That gap is not cosmetic. Where Omnitron holds no Czech label, the Czech
    storefront shows Czech shoppers the Turkish one, so an untranslated row
    here is a customer-facing defect and reportable as such.

    Keyed on the **baseline label** rather than Omnitron's value pk, because
    that is what `attribute_values.value` stores for these attributes and it
    joins directly. Where one baseline label carries two different
    translations in a locale, the ambiguity is recorded rather than resolved —
    `ambiguous` marks it, in keeping with how this module treats every other
    disagreement.
    """

    __tablename__ = "vocabulary_labels"
    __table_args__ = (
        UniqueConstraint(
            "attribute_id", "baseline", "locale", name="uq_vocab_label_attr_base_locale"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    #: The label as the tenant's default locale renders it — Turkish here, and
    #: what `attribute_values.value` holds for dropdown-backed attributes.
    baseline: Mapped[str] = mapped_column(String(255), index=True)
    #: `pl` | `cs` | `sk` | `hu`. Omnitron's own codes, which are not the
    #: market codes: Czech is `cs` here and `CZ` as a market.
    locale: Mapped[str] = mapped_column(String(5))
    label: Mapped[str] = mapped_column(String(255))
    #: False when the locale returned the baseline unchanged, i.e. no
    #: translation exists. Never inferred at read time — the comparison is only
    #: possible while both responses are in hand.
    translated: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Two Omnitron values share this baseline label but disagree on the
    #: translation, so which one applies to a given product is unknowable from
    #: the label alone.
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ValueConflict(Base):
    """A ModelColor whose own SKUs disagreed — written anyway, and queued.

    The importer abstains when sizes of one product carry different values for
    one attribute, because a majority vote picks the un-updated generic value
    as often as the right one (`sync/conflicts.py` has the measurements). That
    decision is only defensible if the abstentions are *findable* — before this
    table they existed solely as log lines, so nobody could list them, count
    them per attribute, or work through them.

    A snapshot of the latest values sync, not a history: rows are replaced
    wholesale each run. A conflict that has been fixed upstream should simply
    stop appearing, and keeping the old row would turn the queue into an
    archive nobody trusts.
    """

    __tablename__ = "value_conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: What the importer wrote despite the disagreement, and the rule that
    #: picked it (`language` | `majority`). 2026-08-18: conflicts import a
    #: best value and flag rather than abstaining — an empty cell beside a
    #: full Omnitron column read as the panel being broken, not as caution.
    resolved_value: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_rule: Mapped[str | None] = mapped_column(String(16), default=None)
    entity_kind: Mapped[str] = mapped_column(String(16))
    entity_key: Mapped[str] = mapped_column(String(64), index=True)
    attribute_id: Mapped[int] = mapped_column(Integer, index=True)
    #: `*` for market-agnostic attributes, matching `attribute_values`.
    market: Mapped[str] = mapped_column(String(4))
    #: JSON object of {value: how many SKUs reported it}. Text rather than
    #: JSONB so SQLite dev and Postgres production stay on one code path.
    candidates: Mapped[str] = mapped_column(Text)
    #: How many SKUs took part, so a 2-of-5 near-tie reads differently from
    #: an 8-versus-1.
    sku_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Which source column(s) fed this. One column disagreeing with itself is
    #: a data defect; two columns is a registry defect.
    sources: Mapped[str] = mapped_column(String(255), default="")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    sync_run_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)


# ── BrandLibrary image publishing ──────────────────────────────────────────

# Products reach the sites without photos, and the photo often exists — in the
# brand's asset library, where the site cannot see it. This module finds those
# options, publishes the assets to our own folder on the photo server, and
# hands Turkey a bundle to upload, because our Omnitron connection is read-only
# and always will be.
#
# The three tables below are the three things a run produces that outlive it:
# what was published, what could not be, and what was handed over.


class BrandLibrarySession(Base):
    """The brand library cookie, and who last pasted it.

    In the database rather than the environment, which is unusual here and is
    forced by the library: it has no password flow and no API token — auth is a
    browser session cookie, and that cookie expires within hours. A value in
    the server's `.env` would be stale by the second week and every scheduled
    run would fail until somebody redeployed.

    So it lives here, the Images page updates it, and the weekly run reads
    whatever is current. That turns the cadence into "paste, then click"
    instead of "raise a ticket".

    One row, always. `id` is pinned to 1 rather than growing a history: a
    superseded cookie is a live credential for as long as it has left, and
    keeping a table of them would be a table of ways into the brand's library.
    `rotated_at` and `rotated_by` are the audit trail; the old value is not.
    """

    __tablename__ = "brandlibrary_session"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    cookie: Mapped[str] = mapped_column(Text)
    #: The session is tied to the browser that created it, so the user agent
    #: travels with the cookie. Taken from the same paste when it is a cURL.
    user_agent: Mapped[str | None] = mapped_column(Text, default=None)
    rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rotated_by: Mapped[str | None] = mapped_column(String(255), default=None)
    #: When the library last accepted it. What lets the page say "working an
    #: hour ago" rather than only "pasted this morning" — the two diverge, and
    #: the second one is the question people actually have.
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PublishedImage(Base):
    """One photo we put on the photo server. Replaces the manifest CSV.

    **The URL is the deliverable, not the upload.** An uploaded file nobody can
    reference is work that has to be done again — the first run learned that
    the expensive way — so every row records where the file landed publicly.

    `landed_at` is the `exported → live` hop the rest of the schema turns on,
    and it exists here for the same reason it exists there: the handoff is a
    file sent to another team with no acknowledgement. Set it when a later
    Omnitron image sync finds an image for this ModelColor. Without it, a
    bundle Turkey never uploaded is indistinguishable from one they did, and
    the same photos get republished forever.

    Note what this table is *not*: it is not `attribute_values` holding
    `image_0`. That attribute mirrors Omnitron's own URL, and writing our FTP
    address into it would make the mirror report a product as imaged while the
    site still shows nothing. The two are different claims and stay apart.
    """

    __tablename__ = "published_images"
    __table_args__ = (
        # The remote filename is the identity: it is what a second run would
        # overwrite. Unique so a re-run cannot record the same file twice.
        UniqueConstraint("remote_name", name="uq_published_remote_name"),
        Index("ix_published_model_color", "model_color"),
        Index("ix_published_landed", "landed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Dotted, as Omnitron spells it — `AF0276.J2G`. The join key to products.
    model_color: Mapped[str] = mapped_column(String(24), index=True)
    #: Underscored, as BrandLibrary spells it. Stored rather than derived
    #: because the two conventions are a fact of this business and the
    #: derivation is lossy in the direction that matters.
    brandlibrary_key: Mapped[str] = mapped_column(String(32))
    #: The asset's name in the library, which is what the filename comes from.
    asset_name: Mapped[str] = mapped_column(String(128))
    edition: Mapped[str] = mapped_column(String(16))  # original|zalando
    remote_name: Mapped[str] = mapped_column(String(160))
    public_url: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: Which run put it there. Null for the 1,603 files the original script
    #: published before this module existed, which are backfilled from its CSV.
    sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id"), index=True, default=None
    )
    bundle_id: Mapped[int | None] = mapped_column(
        ForeignKey("handoff_bundles.id"), index=True, default=None
    )
    #: Set when Omnitron is later seen to hold an image for this ModelColor.
    landed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class BrandLibraryAbsence(Base):
    """A ModelColor searched for in the library and genuinely not there.

    This is the row that turns a silent nothing into a piece of work. Without
    it the same forty options are re-searched every week, found missing every
    week, and nobody is ever told — the run reports success because publishing
    nothing is not a failure.

    `attempts` is the argument for asking the brand: "searched four times since
    12 August" is a sentence somebody can act on. Re-checking costs nothing —
    the catalogue is read in one pass whether these are in the search or not —
    so they stay in it, and an absence that resolves itself disappears from
    here the moment its photo is published.
    """

    __tablename__ = "brandlibrary_absences"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_color: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    first_missed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=1)


class HandoffBundle(Base):
    """A ZIP and an Excel handed to the Turkey team.

    The run is not finished when the upload is; it is finished when somebody
    else has something they can act on. We cannot write images into Omnitron —
    the connection is read-only by decision — so the bundle *is* the output and
    everything before it is preparation.

    The row outlives its files. Bundles are cleared from disk after
    `settings.bundles_kept`, and "what did we hand over in August" has to stay
    answerable after the ZIP is gone, so `zip_path` may point at nothing while
    the counts and `sent_at` remain true.
    """

    __tablename__ = "handoff_bundles"

    id: Mapped[int] = mapped_column(primary_key=True)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    built_by: Mapped[str | None] = mapped_column(String(255), default=None)
    option_count: Mapped[int] = mapped_column(Integer, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    zip_path: Mapped[str | None] = mapped_column(String(512), default=None)
    xlsx_path: Mapped[str | None] = mapped_column(String(512), default=None)
    #: building|ready|cleared — `cleared` is a bundle whose files were pruned,
    #: which is a normal end state and not a failure.
    status: Mapped[str] = mapped_column(String(10), default="building")
    #: When somebody recorded sending it on. Null is "built, not yet sent".
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# ── Pushes to Omnitron ─────────────────────────────────────────────────────


class PushBatch(Base):
    """One push somebody started: who, from where, and how it ended.

    `exported` on a value says it went out; it cannot say when, in which
    batch, for which sizes, whether the read-back confirmed it or why one size
    was skipped. This row and its items answer that. `halted` is its own end
    state — the read-back found a key changed that the push did not send, the
    run stopped before touching another product, and the row says so until
    somebody has looked (see `sync/push.py`).
    """

    __tablename__ = "push_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)
    #: ui|upload|bulk|cli — where the values it carries were set.
    source: Mapped[str] = mapped_column(String(12))
    #: pending|pushing|done|partial|failed|halted
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    note: Mapped[str | None] = mapped_column(String(255), default=None)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    sku_count: Mapped[int] = mapped_column(Integer, default=0)
    written_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    sync_run_id: Mapped[int | None] = mapped_column(Integer, default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error_summary: Mapped[str | None] = mapped_column(Text, default=None)


class PushItem(Base):
    """One (size, attribute, market) a batch carried — Omnitron's write grain.

    A value is stored once per option and written once per size, so one
    approved name becomes eight of these. `old_upstream` is what Omnitron held
    before the write, kept because "what did we overwrite" is the question a
    restore needs answered.
    """

    __tablename__ = "push_items"
    __table_args__ = (
        Index("ix_push_items_batch_status", "batch_id", "status"),
        Index("ix_push_items_model_color", "model_color"),
        Index("ix_push_items_unpublished", "market", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("push_batches.id"))
    model_color: Mapped[str] = mapped_column(String(24))
    sku: Mapped[str] = mapped_column(String(32))
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"))
    market: Mapped[str] = mapped_column(String(4))
    locale: Mapped[str | None] = mapped_column(String(8), default=None)
    #: The Omnitron attribute key the value was written to.
    key: Mapped[str] = mapped_column(String(128))
    old_upstream: Mapped[str | None] = mapped_column(Text, default=None)
    new_value: Mapped[str] = mapped_column(Text)
    #: pending|written|confirmed|failed|skipped
    status: Mapped[str] = mapped_column(String(10), default="pending")
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: When the market's own storefront was first seen serving this value.
    #:
    #: `confirmed_at` says Omnitron holds what we sent, seconds after the
    #: write. This says a customer can read it, which on the first real push
    #: was two hours later — and on another channel, 27 seconds. Null means
    #: not seen yet, never that it failed; the shop is asked again on the next
    #: pass. Set at option grain, so every size of one value publishes at once.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# ── Auth + audit (shape shared with the price app for optional SSO) ────────


class User(Base):
    """Admin-panel user. Same shape as the price app's `users` table so the
    two panels can share sign-on later if that decision lands — see
    04 - Open Questions in the vault.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))  # developer|planning|manager
    name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Deliberately absent: `last_seen_changelog_version`. It lives in
    # `module_user_state` because it is per-module — see that class.


class ModuleUserState(Base):
    """What *this module* remembers about a person.

    `users` is shared with the price-comparison app and carries identity only —
    email, password hash, name, active flag. Anything a module tracks about
    someone belongs to that module, and `last_seen_changelog_version` is the
    case that forced the rule: both modules have their own changelog, so a
    shared column means reading one app's release notes clears the other's
    unread flag. The bell would go dark on releases nobody saw, silently, and
    it would look like the feature simply not working.

    Keyed on `user_id` with no foreign key: the row it references lives in a
    table this module does not own and must not migrate.
    """

    __tablename__ = "module_user_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    last_seen_changelog_version: Mapped[str | None] = mapped_column(String(32), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuditLog(Base):
    """One row per auditable action.

    Same shape as the price app's `audit_log`, deliberately a *different*
    table. The two modules share a database and therefore share identity
    (`users`), but not per-module memory — the same line `module_user_state`
    draws for the changelog bell. A shared audit table would put price-app
    traffic in this module's Activity Log and vice versa, and would let a
    migration here alter a table the other app writes to.
    """

    __tablename__ = "pm_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default=utcnow
    )
    user_id: Mapped[int | None] = mapped_column(default=None, index=True)
    user_email: Mapped[str | None] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(64))
    method: Mapped[str | None] = mapped_column(String(8))
    path: Mapped[str | None] = mapped_column(String(255))
    status_code: Mapped[int | None] = mapped_column(default=None)
    ip: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, default=None)
