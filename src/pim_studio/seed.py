"""Seed the attribute registry, column mappings, vocabularies and ban list.

Idempotent: safe to re-run after adding entries — existing rows are matched by
their natural keys and left alone, new ones are inserted. Run with:

    python -m pim_studio.seed

Everything here is traceable to a vault note. The Omnitron column names come
from the two exports profiled on 2026-08-13 (16-column April view, 32-column
August view) — an export is a saved column view, so this list grows additively
as wider exports appear and is never treated as complete. The product-card
columns come from the AW26 file and the SS26 Turkish template. The ban list is
Rules/Ban list. Vocabularies are seeded from observed values and marked
`approved` only where the user confirmed the set; the PIM attribute sheet can
extend them later.
"""

from __future__ import annotations

from .db import session_scope
from .logging import log
from .models import Attribute, AttributeColumn, BanComment, VocabularyOption
from .rules.ban import SEEDED_BAN_COMMENTS

# Ukraine is deliberately absent: UA content exists in Omnitron but is out of
# scope by decision (2026-08-13), so `urun_detay_ua` and `urun_adi_ua` are not
# mapped at all. The same goes for the `_kz` and `_ru` variants the live
# registry also carries — four channels only (2026-08-14).
MARKETS = ("PL", "CZ", "SK", "HU")

# key, label, data_type, write_channel, value_grain, per_market, long_text,
# gates_go_live, applies_when (comma list or None), display_group, order
ATTRIBUTES = [
    # ── Identity / read-only spine ────────────────────────────────────────
    ("base_code_format", "Article (MCS)", "text", "omnitron_import", "sku",
     False, False, False, None, "Identity", 0),
    ("base_code", "Base code", "text", "read_only", "base_code",
     False, False, False, None, "Identity", 1),
    ("color_code", "Colour code", "text", "read_only", "model_color",
     False, False, False, None, "Identity", 2),
    ("ean", "EAN / barcode", "text", "read_only", "sku",
     False, False, False, None, "Identity", 3),

    # ── Per-market content (ours, uploadable) ─────────────────────────────
    ("product_name", "Product name", "text", "omnitron_import", "model_color",
     True, False, True, None, "Content", 10),
    ("product_detail", "Product detail", "text", "omnitron_import", "model_color",
     True, False, False, None, "Content", 11),
    # The storefront's own product name, and read-only for us: it is Omnitron's
    # core `name` field translated per locale, not a custom attribute, so it is
    # not something an import file can set (added 2026-08-14).
    #
    # It exists in the registry because it is almost certainly what the
    # customer actually sees — Akinon renders a product's own name by default,
    # and a custom attribute like `urun_adi_pl` only appears where a template
    # explicitly pulls it. Tracking both is what makes the divergence visible:
    # on one sampled polo, PL shows "Bawełniana Koszulka Polo Original N.10.10
    # Fine Piqué" here against "Koszulka Polo N.10.10 Classic Fit" in
    # `product_name`. Same product, two names, and only one of them is ours.
    ("storefront_name", "Storefront name", "text", "read_only", "model_color",
     True, False, False, None, "Content", 10),
    ("description", "Description", "html", "omnitron_import", "model_color",
     True, True, True, None, "Content", 12),

    # ── Commercial (read from Omnitron; price gates liveness) ─────────────
    ("price", "Price", "number", "read_only", "sku",
     True, False, True, None, "Commercial", 20),
    ("retail_price", "Retail price", "number", "read_only", "sku",
     True, False, False, None, "Commercial", 21),
    ("stock", "Stock", "number", "read_only", "sku",
     False, False, True, None, "Commercial", 22),
    ("is_discounted", "Is discounted", "bool", "read_only", "sku",
     True, False, False, None, "Commercial", 23),
    ("discount_pct", "Discount %", "number", "read_only", "sku",
     True, False, False, None, "Commercial", 24),

    # ── Our own writable fields ───────────────────────────────────────────
    ("pl_sezon", "Season (PL team)", "text", "omnitron_import", "model_color",
     False, False, False, None, "Season", 30),
    ("pl_comment", "Comment (PL team)", "text", "omnitron_import", "model_color",
     False, False, False, None, "Season", 31),
    ("erp_season", "ERP season", "text", "read_only", "model_color",
     False, False, False, None, "Season", 32),

    # ── Classification (partner-owned via product cards) ──────────────────
    ("main_category", "Main category", "text", "product_card", "model_color",
     False, False, True, None, "Classification", 40),
    ("sub_category", "Sub-category", "text", "product_card", "model_color",
     False, False, True, None, "Classification", 41),
    ("gender", "Gender", "text", "product_card", "model_color",
     False, False, True, None, "Classification", 42),
    ("composition", "Composition / material", "text", "product_card", "model_color",
     False, False, False, None, "Classification", 43),
    # Added 2026-08-18. All three exist in Omnitron with real vocabularies and
    # had no registry row, so nothing could hold their values — the last of the
    # seven garment attributes the product filters rest on.
    ("fabric_type", "Fabric type", "text", "product_card", "model_color",
     False, False, False, None, "Classification", 46),
    ("length", "Length", "text", "product_card", "model_color",
     False, False, False, None, "Classification", 47),
    ("waist", "Waist", "text", "product_card", "model_color",
     False, False, False, None, "Classification", 48),
    ("color_detail", "Colour detail", "text", "product_card", "model_color",
     False, False, False, None, "Classification", 44),
    ("color_filter", "Colour filter", "text", "product_card", "model_color",
     False, False, False, None, "Classification", 45),
    # Conditional on sub-category — the split measured in the AW26 product
    # cards file: sleeve/collar/fit/pattern apply to garment sub-categories
    # and are correctly blank on bags, wallets, caps, sneakers.
    ("sleeve_type", "Sleeve type", "text", "product_card", "model_color",
     False, False, False,
     "Polo,T-shirt,Shirt,Sweater,Sweatshirt,Jacket,Dress", "Classification", 46),
    ("collar_type", "Collar type", "text", "product_card", "model_color",
     False, False, False,
     "Polo,T-shirt,Shirt,Sweater,Sweatshirt,Jacket,Dress", "Classification", 47),
    ("fit", "Fit", "text", "product_card", "model_color",
     False, False, False,
     "Polo,T-shirt,Shirt,Sweater,Sweatshirt,Jacket,Dress,Trousers,Short,Skirts,Track trousers",
     "Classification", 48),
    ("pattern", "Pattern", "text", "product_card", "model_color",
     False, False, False,
     "Polo,T-shirt,Shirt,Sweater,Sweatshirt,Jacket,Dress,Trousers,Short,Skirts",
     "Classification", 49),

    # ── Images ────────────────────────────────────────────────────────────
    ("image_0", "First image", "text", "read_only", "model_color",
     False, False, True, None, "Images", 60),
]

# source, column_name, attribute_key, market, read_only
# Omnitron columns are the literal export/import contract — stored verbatim.
COLUMNS = [
    # April + August exports, shared
    ("omnitron", "sku", None, None, True),  # identity, handled by importer, mapped for completeness
    ("omnitron", "attributes__integration_base_code_format", "base_code_format", None, False),
    ("omnitron", "base_code", "base_code", None, True),
    ("omnitron", "active_price__price", "price", "PL", True),
    ("omnitron", "active_price__retail_price", "retail_price", "PL", True),
    ("omnitron", "active_stock__stock", "stock", None, True),
    ("omnitron", "active_categories__category__name", "sub_category", None, True),
    ("omnitron", "productimage_set__0__image", "image_0", None, True),
    ("omnitron", "attributes__PL_sezon", "pl_sezon", None, False),
    ("omnitron", "attributes__PL_comment", "pl_comment", None, False),
    # April export only
    ("omnitron", "attributes__urun_adi_pl", "product_name", "PL", False),
    ("omnitron", "attributes__urun_detay_pol", "product_detail", "PL", False),
    ("omnitron", "attributes__integration_kalem_icerik_pol", "description", "PL", False),
    # `integration_model_renk` was mapped here too and should not have been.
    # It carries MODEL.COLOR (`L3539.132`) while `base_code_format` is always
    # MODEL.COLOR.SIZE (`L3539.132.T2`, confirmed 2026-08-14) — two different
    # values contending for one key, which is what made the first real run of
    # the value importer fail on the unique constraint. Removed rather than
    # repointed: the ModelColor is already derived onto `products.model_color`,
    # so nothing needs a second copy of it. Keeping it as an independent
    # cross-check of our own derivation would need its own attribute and its
    # own decision.
    # August export — the suffix spellings are inconsistent by design
    # (cz vs cze, pol vs pl); this explicit map is the only safe join.
    ("omnitron", "attributes__urun_detay_cze", "product_detail", "CZ", False),
    ("omnitron", "attributes__urun_detay_hu", "product_detail", "HU", False),
    ("omnitron", "attributes__urun_detay_sk", "product_detail", "SK", False),
    # Per-market names for the other three markets, added 2026-08-14 from the
    # live registry. The vault previously recorded that no `urun_adi_*`
    # variants existed for CZ/SK/HU — that came from the 50-column export,
    # which is a saved column view and simply did not include them. All three
    # attributes exist upstream (pks 1798-1800) and are populated, so the
    # earlier conclusion that the per-market name had moved into
    # `urun_detay_*` was wrong, and this registry's naming was right all along.
    #
    # Note the suffix flips again here: the name fields use `_cz` where the
    # detail fields use `_cze`. Nothing about these spellings is derivable.
    ("omnitron", "attributes__urun_adi_cz", "product_name", "CZ", False),
    ("omnitron", "attributes__urun_adi_hu", "product_name", "HU", False),
    ("omnitron", "attributes__urun_adi_sk", "product_name", "SK", False),
    # The storefront name, read from Omnitron's localisation store rather than
    # from a flat attribute — hence the `localized_attributes__<locale>__`
    # prefix, which the importer parses in place of `attributes__`.
    #
    # A third inconsistent code system: Omnitron's locales are `pl`, `cs`,
    # `sk`, `hu`, so Czech is **cs** here, `cze` in the detail field and `cz`
    # in the name field. Three spellings of Czech across three fields is why
    # this map is explicit rather than derived, and always will be.
    #
    # Read-only: `l_name` is a translation of Omnitron's core `name` field, so
    # no import file we generate can set it.
    ("omnitron", "localized_attributes__pl__l_name", "storefront_name", "PL", True),
    ("omnitron", "localized_attributes__cs__l_name", "storefront_name", "CZ", True),
    ("omnitron", "localized_attributes__sk__l_name", "storefront_name", "SK", True),
    ("omnitron", "localized_attributes__hu__l_name", "storefront_name", "HU", True),
    # The description as Omnitron's one localizable key holds it in every
    # language (2026-08-25). `integration_kalem_icerik` is filled on 799 of 800
    # products measured and returns a different text per locale; the
    # market-suffixed columns below carry one on 177-193 of the same 800.
    #
    # It is *not* the description the shops show. example.com/.cz/.sk/.hu render
    # the suffixed column and nothing else — an empty one means no description
    # on the page, however good the shared key's text (2026-09-16). So since
    # 0.47.0 these four are read only to offer proposals (`sync/values`,
    # `proposals.offer_shared_key`); the suffixed columns are the live value.
    # From 2026-08-25 until then the shared key was stored as the description.
    #
    # `market_attributes__<market>__` is the prefix for that: one upstream key
    # read four times, once per market locale. The market appears in the column
    # name only because `attribute_columns` is unique on (source, column_name);
    # the routing market is the field beside it, as everywhere else.
    #
    # Read-only: the shared key is Omnitron's own copy across every locale,
    # and no import file of ours addresses it per market.
    ("omnitron", "market_attributes__PL__integration_kalem_icerik", "description", "PL", True),
    ("omnitron", "market_attributes__CZ__integration_kalem_icerik", "description", "CZ", True),
    ("omnitron", "market_attributes__SK__integration_kalem_icerik", "description", "SK", True),
    ("omnitron", "market_attributes__HU__integration_kalem_icerik", "description", "HU", True),
    # The market-suffixed siblings: the description each shop renders, the key
    # a push writes, and the column name an import file carries. PL's is mapped
    # further up with the April export columns.
    ("omnitron", "attributes__integration_kalem_icerik_cz", "description", "CZ", False),
    ("omnitron", "attributes__integration_kalem_icerik_hu", "description", "HU", False),
    ("omnitron", "attributes__integration_kalem_icerik_sk", "description", "SK", False),
    ("omnitron", "attributes__integration_renk", "color_code", None, True),
    # size — importer identity field
    ("omnitron", "attributes__integration_beden", None, None, True),
    ("omnitron", "attributes__integration_cinsiyet", "gender", None, True),
    ("omnitron", "attributes__integration_alt_grup", "sub_category", None, True),
    # The seven garment attributes the filters depend on, wired 2026-08-18.
    # Every one is a dropdown, so the label is mapped rather than the code —
    # `integration_desen` is keyed by numeric codes and would otherwise render
    # as "003" instead of "Düz". The importer drops the code mapping wherever a
    # label mapping exists for the same attribute, so mapping both is not a
    # collision (see sync/values.load_mappings).
    ("omnitron", "attributes_kwargs__integration_yaka__label", "collar_type", None, True),
    ("omnitron", "attributes_kwargs__integration_kalip__label", "fit", None, True),
    ("omnitron", "attributes_kwargs__integration_kol_boyu__label", "sleeve_type", None, True),
    ("omnitron", "attributes_kwargs__integration_desen__label", "pattern", None, True),
    ("omnitron", "attributes_kwargs__integration_kumas_tipi__label", "fabric_type", None, True),
    ("omnitron", "attributes_kwargs__integration_boy__label", "length", None, True),
    ("omnitron", "attributes_kwargs__integration_bel__label", "waist", None, True),
    # Leftover integration artefact, confirmed 2026-08-13 — values look like
    # base-code fragments (0011, L131, TH67) and nothing maintains it. Recorded
    # so it isn't investigated again; never mapped to an attribute.
    ("omnitron", "attributes__integration_country_channel", None, None, True),
    # 65% synthetic — validate as 13-digit
    ("omnitron", "attributes__integration_yurt_disi_barkod", "ean", None, True),
    ("omnitron", "attributes_kwargs__integration_alt_grup__label", "sub_category", None, True),
    ("omnitron", "attributes_kwargs__integration_beden__label", None, None, True),
    ("omnitron", "attributes_kwargs__integration_cinsiyet__label", "gender", None, True),
    ("omnitron", "attributes_kwargs__integration_renk__label", "color_code", None, True),
    # The five curated attributes that had no source column at all, wired
    # 2026-08-18. Prompted by AH4463.132: ZAWARTOŚĆ visibly full in the
    # Omnitron check-list and empty in the panel, because nothing read it.
    # Fill rates measured against the live API over a 600-SKU random sample
    # before wiring, so none of these is a guess:
    #
    #   composition    integration_erp_icerik        49.0%  "%65 RAYON %35 NAYLON"
    #   color_detail   integration_renk_detay        99.8%  "AÇIK KAHVERENGİ"
    #   color_filter   integration_renk_tonu        100.0%  "Bej"
    #   main_category  integration_urun_ana_grup     99.0%  "Giyim"
    #   erp_season     integration_erp_sezon        100.0%  "24SS"
    #
    # Reading these is not gated by the ownership question in
    # [[04 - Open Questions]] — that asks who may *write* them, and the vault
    # is explicit that being readable in Omnitron means we can report on them
    # today. All five stay read-only here; `write_channel` on the attribute is
    # what decides who fills a gap.
    #
    # composition is a flat text attribute, the rest are dropdowns, so the
    # label is mapped for those — the raw values are codes ("001" for Giyim).
    ("omnitron", "attributes__integration_erp_icerik", "composition", None, True),
    ("omnitron", "attributes_kwargs__integration_renk_detay__label", "color_detail", None, True),
    ("omnitron", "attributes_kwargs__integration_renk_tonu__label", "color_filter", None, True),
    (
        "omnitron", "attributes_kwargs__integration_urun_ana_grup__label",
        "main_category", None, True,
    ),
    ("omnitron", "attributes__integration_erp_sezon", "erp_season", None, True),
    # Turkish master name. Deliberately inert: `name` is a bare product field,
    # not an `attributes__`/`attributes_kwargs__`/`localized_attributes__`
    # path, so `sync/values.load_mappings` never reads it — recorded here so
    # the column's existence is documented, like `integration_country_channel`
    # above. Wiring it would write the default-locale name at market `*` into
    # a per-market attribute, which is a semantics change nobody has asked for.
    ("omnitron", "name", "product_name", None, True),
    ("omnitron", "is_active", None, None, True),  # observed 100% empty — ignored
    ("omnitron", "data_source__name", None, None, True),

    # Product cards template (Turkey team) — Turkish headers, ModelColor grain
    ("product_card", "TEDARİKÇİ MODEL KODU", "base_code", None, False),
    ("product_card", "TEDARİKÇİ RENK KODU", "color_code", None, False),
    ("product_card", "ÜRÜN ADI (E-COM)", "product_name", None, False),
    ("product_card", "ÜRÜN AÇIKLAMASI (E-COM)", "description", None, False),
    ("product_card", "ÜRÜN DETAY (E-COM)", "product_detail", None, False),
    ("product_card", "KOL TİPİ", "sleeve_type", None, False),
    ("product_card", "DESEN", "pattern", None, False),
    ("product_card", "KALIP", "fit", None, False),
    ("product_card", "RENK DETAY", "color_detail", None, False),
    ("product_card", "RENK FİLTRESİ", "color_filter", None, False),
    ("product_card", "YAKA TİPİ", "collar_type", None, False),
    ("product_card", "ANA KATEGORİ (E-COM)", "main_category", None, False),
    # two spaces: verbatim from the template
    ("product_card", "ALT KATEGORİ  (E-COM)", "sub_category", None, False),
    ("product_card", "CİNSİYET (E-COM)", "gender", None, False),
    ("product_card", "COMPOSITION/MATERYAL", "composition", None, False),
]

# The source columns a push may write, by literal column name. Every one names
# its market in the key (`urun_adi_pl`, `urun_detay_cze`,
# `integration_kalem_icerik_hu`) or is a global key only our team uses
# (`PL_sezon`, `PL_comment`); nothing shared across stores is in this set. The
# shared `integration_kalem_icerik` is deliberately absent: the storefronts
# render the suffixed siblings (measured on all four sites 2026-09-07), and a
# write to the shared key under the wrong locale lands in the Turkish store.
# The garment dropdowns are absent too — one tenant-wide value each, so a
# write would change Turkey along with us (decided 2026-09-07).
#
# `run()` re-applies this on every boot to every Omnitron column, so the flag
# has one owner; migration c7d2e91a4f38 seeds the same set for a database
# that is migrated but not yet re-seeded.
API_WRITABLE_COLUMNS = frozenset({
    "attributes__urun_adi_pl",
    "attributes__urun_adi_cz",
    "attributes__urun_adi_sk",
    "attributes__urun_adi_hu",
    "attributes__urun_detay_pol",
    "attributes__urun_detay_cze",
    "attributes__urun_detay_sk",
    "attributes__urun_detay_hu",
    "attributes__integration_kalem_icerik_pol",
    "attributes__integration_kalem_icerik_cz",
    "attributes__integration_kalem_icerik_sk",
    "attributes__integration_kalem_icerik_hu",
    "attributes__PL_sezon",
    "attributes__PL_comment",
})

# attribute_key -> (status, values). Approved only where the set is confirmed;
# everything else can arrive as candidates from data.
VOCABULARIES = {
    "sleeve_type": ("approved", ["LONG SLEEVES", "SHORT-SLEEVES", "SLEEVELESS"]),
    "gender": ("approved", ["MAN", "WOMAN", "UNISEX"]),
    "main_category": ("approved", ["Textile", "Leather Goods", "Underwear", "Footwear"]),
    "fit": (
        "candidate",
        ["CLASSIC FIT", "SLIM FIT", "REGULAR FIT", "LOOSE FIT", "OVERSIZE FIT", "FLARE FIT"],
    ),
    "color_filter": (
        "candidate",
        ["BLACK", "BLUE", "WHITE", "GREEN", "BEIGE", "RED", "BROWN", "GREY", "PINK"],
    ),
    "pattern": ("candidate", ["SOLID", "PRINTED", "BLOCK COLOR", "KNIT EFFECT"]),
    "pl_sezon": (
        "candidate",
        ["26SS", "26AW", "25SS", "25AW", "OUTLET", "OUTLETSS", "OUTLETAW", "REMOVED", "CONT"],
    ),
}

# Rules/Ban list — the confirmed classification of every PL_comment value
# observed in the April export. Owned by `rules.ban` so the classification has
# one home: the rule module needs it to work without a database, this needs it
# to fill the table, and a third copy in the demo generator is how the three
# drift apart. Matching is case-insensitive + trimmed.
BAN_COMMENTS = SEEDED_BAN_COMMENTS


def run() -> None:
    with session_scope() as db:
        attrs: dict[str, Attribute] = {a.key: a for a in db.query(Attribute).all()}
        added_a = added_c = added_v = added_b = 0

        for (
            key, label, dtype, channel, grain, per_mkt, longt, gates, applies, group, order,
        ) in ATTRIBUTES:
            if key in attrs:
                continue
            a = Attribute(
                key=key, label=label, data_type=dtype, write_channel=channel,
                value_grain=grain, per_market=per_mkt, long_text=longt,
                gates_go_live=gates, applies_when_subcategory=applies,
                display_group=group, display_order=order,
            )
            db.add(a)
            attrs[key] = a
            added_a += 1
        db.flush()

        existing_cols = {
            (c.source, c.column_name) for c in db.query(AttributeColumn).all()
        }
        for source, col, attr_key, market, ro in COLUMNS:
            if (source, col) in existing_cols or attr_key is None:
                continue
            db.add(AttributeColumn(
                attribute_id=attrs[attr_key].id, source=source,
                column_name=col, market=market, read_only=ro,
            ))
            added_c += 1

        # The writable set is re-applied every run rather than only on insert,
        # so a column that changes sides moves with the next boot and a flag
        # set by hand in the database does not survive it.
        flagged = 0
        for c in db.query(AttributeColumn).filter_by(source="omnitron").all():
            want = c.column_name in API_WRITABLE_COLUMNS
            if c.api_writable != want:
                c.api_writable = want
                flagged += 1
        missing = API_WRITABLE_COLUMNS - {col for (_s, col, _a, _m, _r) in COLUMNS}
        if missing:
            raise RuntimeError(
                f"API_WRITABLE_COLUMNS names columns COLUMNS does not: {sorted(missing)}"
            )

        existing_vocab = {
            (v.attribute_id, v.value.lower()) for v in db.query(VocabularyOption).all()
        }
        for attr_key, (status, values) in VOCABULARIES.items():
            aid = attrs[attr_key].id
            for v in values:
                if (aid, v.lower()) in existing_vocab:
                    continue
                db.add(VocabularyOption(
                    attribute_id=aid, value=v, status=status, created_from="seed",
                ))
                added_v += 1

        existing_bans = {b.comment.lower() for b in db.query(BanComment).all()}
        for comment, bans, note in BAN_COMMENTS:
            if comment.lower() in existing_bans:
                continue
            db.add(BanComment(comment=comment, bans=bans, note=note))
            added_b += 1

    log.info(
        "seed_complete",
        attributes=added_a, columns=added_c, vocabulary=added_v, ban_comments=added_b,
        writable_flags_changed=flagged,
    )


if __name__ == "__main__":
    run()
