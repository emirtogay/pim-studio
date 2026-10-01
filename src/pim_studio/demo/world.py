"""The demo's upstream: one mutable state every mock reads and writes.

`catalog.py` says what the products are. This turns that into what each
external system would hold on a given day — PIM records, catalogue rows, price
and stock lists, the deletion feed, each shop's listing, the asset library —
and keeps it consistent while the panel writes to it. A push changes the PIM's
records and the shops start serving the new text; a takedown deletes catalogue
rows and the shops stop listing the option. That is the whole reason there is
one state rather than one fake per system.

**Writes survive the process.** The seed and the web server are different
processes, and a takedown the seed performed has to still be a takedown when
somebody opens the panel and presses "put back". So every write the PIM mock
accepts is appended to a journal under `<UPLOADS_ROOT>/mock-pim/`, and a fresh
world replays it. The journal is the only state; everything else regenerates
from the seed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import PRODUCT_META
from . import catalog as cat
from .catalog import EPOCH, LOCALE_OF, MARKETS, Catalog, Option

#: The shop's own spelling of each market's content keys, as the storefronts
#: render them and as `seed.COLUMNS` maps them.
NAME_KEY = {"PL": "urun_adi_pl", "CZ": "urun_adi_cz", "SK": "urun_adi_sk", "HU": "urun_adi_hu"}
DETAIL_KEY = {"PL": "urun_detay_pol", "CZ": "urun_detay_cze", "SK": "urun_detay_sk",
              "HU": "urun_detay_hu"}
DESCRIPTION_KEY = {"PL": "integration_kalem_icerik_pol", "CZ": "integration_kalem_icerik_cz",
                   "SK": "integration_kalem_icerik_sk", "HU": "integration_kalem_icerik_hu"}
SHARED_DESCRIPTION_KEY = "integration_kalem_icerik"

#: Merch comments the seeded ban list treats as "take it off the shop".
_BANNING = {"removed", "removed, wrong image"}

# ── the attribute registry ─────────────────────────────────────────────────

#: key, display name, data type, localizable.
_ATTRIBUTES: tuple[tuple[str, str, str, bool], ...] = (
    ("integration_base_code_format", "Article (model.colour.size)", "text", False),
    ("integration_model_renk", "Model and colour", "text", False),
    ("PL_sezon", "Season (PL team)", "text", False),
    ("PL_comment", "Comment (PL team)", "text", False),
    ("integration_yurt_disi_barkod", "Export barcode", "text", False),
    ("integration_erp_icerik", "Composition (ERP)", "text", False),
    ("integration_erp_sezon", "Season (ERP)", "text", False),
    ("integration_country_channel", "Country channel", "text", False),
    *((NAME_KEY[m], f"Product name {m}", "text", True) for m in MARKETS),
    *((DETAIL_KEY[m], f"Product detail {m}", "text", True) for m in MARKETS),
    (SHARED_DESCRIPTION_KEY, "Description (every language)", "area", True),
    *((DESCRIPTION_KEY[m], f"Description {m}", "area", True) for m in MARKETS),
    ("integration_renk", "Colour code", "dropdown", False),
    ("integration_beden", "Size", "dropdown", False),
    ("integration_cinsiyet", "Gender", "dropdown", False),
    ("integration_alt_grup", "Sub-category", "dropdown", False),
    ("integration_urun_ana_grup", "Main category", "dropdown", False),
    ("integration_renk_detay", "Colour detail", "dropdown", False),
    ("integration_renk_tonu", "Colour family", "dropdown", False),
    ("integration_yaka", "Collar", "dropdown", False),
    ("integration_kalip", "Fit", "dropdown", False),
    ("integration_kol_boyu", "Sleeve length", "dropdown", False),
    ("integration_desen", "Pattern", "dropdown", False),
    ("integration_kumas_tipi", "Fabric type", "dropdown", False),
    ("integration_boy", "Length", "dropdown", False),
    ("integration_bel", "Waist", "dropdown", False),
    # Attributes only the channels we ignore use — the registry report is
    # supposed to say so rather than present them as our gaps.
    ("integration_marketplace_title", "Marketplace title", "text", True),
    ("integration_gift_wrap", "Gift wrap available", "bool", False),
)

ATTRIBUTE_PK: dict[str, int] = {key: 1001 + i for i, (key, *_rest) in enumerate(_ATTRIBUTES)}
LOCALIZABLE: frozenset[str] = frozenset(k for k, _n, _t, loc in _ATTRIBUTES if loc)

#: Baseline label -> its translation in pl, cs, sk, hu. `None` is "never
#: translated": the PIM then answers with the baseline, which the labels stage
#: records as untranslated. Fit is absent in CZ and HU, and fabric, length and
#: waist have no translation anywhere — the same shape as the real registry.
_TRANSLATIONS: dict[str, tuple[str | None, str | None, str | None, str | None]] = {
    "SHORT-SLEEVES": ("Krótki rękaw", "Krátký rukáv", "Krátky rukáv", "Rövid ujjú"),
    "LONG SLEEVES": ("Długi rękaw", "Dlouhý rukáv", "Dlhý rukáv", "Hosszú ujjú"),
    "POLO COLLAR": ("Kołnierzyk polo", "Polo límeček", "Polo golier", "Pólógallér"),
    "BUTTON-DOWN": ("Kołnierzyk na guziki", "Propínací límeček", "Gombíkový golier",
                    "Gombos gallér"),
    "CLASSIC COLLAR": ("Klasyczny kołnierzyk", "Klasický límeček", "Klasický golier",
                       "Klasszikus gallér"),
    "CREW NECK": ("Okrągły dekolt", "Kulatý výstřih", "Okrúhly výstrih", "Kerek nyak"),
    "V-NECK": ("Dekolt w serek", "Výstřih do V", "Výstrih do V", "V-nyak"),
    "HALF ZIP": ("Zamek pod szyją", "Zip u krku", None, "Félcipzár"),
    "CLASSIC FIT": ("Klasyczny krój", None, "Klasický strih", None),
    "SLIM FIT": ("Dopasowany krój", None, "Úzky strih", None),
    "REGULAR FIT": ("Regularny krój", None, "Rovný strih", None),
    "LOOSE FIT": ("Luźny krój", None, "Voľný strih", None),
    "SOLID": ("Gładki", "Jednobarevný", "Jednofarebný", "Egyszínű"),
    "BLOCK COLOR": ("Kolorowe bloki", "Barevné bloky", "Farebné bloky", "Színblokkos"),
    "PRINTED": ("Z nadrukiem", "S potiskem", "S potlačou", "Mintás"),
    "CHECKED": ("W kratę", "Kostkovaný", "Kockovaný", "Kockás"),
    "STRIPED": ("W paski", "Pruhovaný", "Pásikavý", "Csíkos"),
    "KNIT EFFECT": ("Efekt dzianiny", "Pletený vzor", None, "Kötött hatás"),
    "MAN": ("Mężczyzna", "Muži", "Muži", "Férfi"),
    "WOMAN": ("Kobieta", "Ženy", "Ženy", "Női"),
    "Polo": ("Koszulki polo", "Polo trička", "Polo tričká", "Pólóingek"),
    "Shirt": ("Koszule", "Košile", "Košele", "Ingek"),
    "Sweater": ("Swetry", "Svetry", "Svetre", "Pulóverek"),
    "Trousers": ("Spodnie", "Kalhoty", "Nohavice", "Nadrágok"),
    "Sneakers": ("Sneakersy", "Tenisky", "Tenisky", "Sportcipők"),
    "Belt": ("Paski", "Opasky", "Opasky", "Övek"),
    "Cap": ("Czapki", "Kšiltovky", "Šiltovky", "Sapkák"),
    "Bag": ("Torby", "Tašky", "Tašky", "Táskák"),
    "Scarf": ("Szaliki", "Šály", "Šály", "Sálak"),
    "Textile": ("Odzież", "Oblečení", "Oblečenie", "Ruházat"),
    "Footwear": ("Obuwie", "Obuv", "Obuv", "Cipők"),
    "Leather Goods": ("Wyroby skórzane", "Kožené zboží", "Kožený tovar", "Bőráru"),
    "BLACK": ("Czarny", "Černá", "Čierna", "Fekete"),
    "WHITE": ("Biały", "Bílá", "Biela", "Fehér"),
    "BLUE": ("Niebieski", "Modrá", "Modrá", "Kék"),
    "BEIGE": ("Beżowy", "Béžová", "Béžová", "Bézs"),
    "RED": ("Czerwony", "Červená", "Červená", "Piros"),
    "GREEN": ("Zielony", "Zelená", "Zelená", "Zöld"),
    "GREY": ("Szary", "Šedá", "Sivá", "Szürke"),
    "BROWN": ("Brązowy", "Hnědá", "Hnedá", "Barna"),
    "PINK": ("Różowy", "Růžová", "Ružová", "Rózsaszín"),
    "NAVY": ("Granatowy", "Tmavě modrá", None, "Sötétkék"),
}
_LOCALE_INDEX = {"pl": 0, "cs": 1, "sk": 2, "hu": 3}


def _vocabulary(catalog: Catalog) -> dict[str, list[str]]:
    """Every dropdown attribute's labels, in first-seen order."""
    seen: dict[str, list[str]] = {}

    def add(key: str, label: str) -> None:
        bucket = seen.setdefault(key, [])
        if label not in bucket:
            bucket.append(label)

    for category in cat.CATEGORIES:
        for key, labels in category.filters.items():
            for label in labels:
                add(key, label)
        for size in category.sizes:
            add("integration_beden", size)
    for colour in cat.COLOURS:
        add("integration_renk", colour.code)
        add("integration_renk_detay", colour.detail)
        add("integration_renk_tonu", colour.family)
    for gender in cat.GENDERS:
        add("integration_cinsiyet", gender)
    for option in catalog.options:
        add("integration_alt_grup", option.sub_category)
        add("integration_urun_ana_grup", option.main_category)
    return seen


@dataclass
class Product:
    """One PIM product record: a size, or a style's grouping row."""

    pk: int
    sku: str
    base_code: str
    product_type: str
    name: str
    #: View -> attributes. `None` is the tenant default; a locale holds only
    #: the localizable keys it has its own copy of.
    views: dict[str | None, dict[str, str]]
    kwargs: dict[str, dict[str, str]]
    localized: dict[str, dict[str, str]]
    images: list[str]
    modified: datetime
    option: Option | None = None

    def record(self, locale: str | None) -> dict[str, Any]:
        attributes = dict(self.views.get(None, {}))
        if locale is not None:
            attributes.update(self.views.get(locale, {}))
        return {
            "pk": self.pk,
            "sku": self.sku,
            "base_code": self.base_code,
            "name": self.name,
            "product_type": self.product_type,
            "is_active": True,
            "modified_date": _iso(self.modified),
            "attributes": attributes,
            "attributes_kwargs": self.kwargs,
            "localized_attributes": self.localized,
            "productimage_set": [
                {"image": url, "order": i} for i, url in enumerate(self.images)
            ],
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class World:
    """Everything upstream holds on one day, plus whatever the panel wrote."""

    catalog: Catalog
    day: int = cat.CURRENT_DAY
    journal: Path | None = None
    products: dict[int, Product] = field(default_factory=dict)
    by_sku: dict[str, int] = field(default_factory=dict)
    #: catalog_item pk -> (product pk, catalogue pk, modified).
    items: dict[int, tuple[int, int, datetime]] = field(default_factory=dict)
    prices: list[dict[str, Any]] = field(default_factory=list)
    stocks: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[dict[str, Any]] = field(default_factory=list)
    vocabulary: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._next_item = 90_000_001
        self.vocabulary = _vocabulary(self.catalog)
        self._build()
        self._replay()

    # ── building one day ───────────────────────────────────────────────────

    def _build(self) -> None:
        day = self.day
        # A deletion is news to whoever reads the feed next, so it is dated
        # when this world was made — after any earlier read, whose watermark
        # the products stage windows the feed by.
        deleted_at = _iso(datetime.now(UTC))
        styles_seen: set[str] = set()
        for option in self.catalog.options:
            if option.base_code not in styles_seen:
                styles_seen.add(option.base_code)
                self._add_grouping_row(option)
            catalogs = option.catalogs.get(day, frozenset())
            for sku in option.sizes:
                if sku.deleted_day2 and day >= 2:
                    self.deleted.append(
                        {
                            "pk": 70_000_000 + sku.pk,
                            "object_id": sku.pk,
                            "content_type_model": "product",
                            "deleted_date": deleted_at,
                        }
                    )
                    continue
                self._add_size(option, sku)
                for slot, catalog_pk in enumerate(sorted(catalogs)):
                    item_pk = sku.pk * 10 + slot
                    self.items[item_pk] = (sku.pk, catalog_pk, option.modified)
                self._add_liveness(option, sku)

    def _add_size(self, option: Option, sku: cat.Sku) -> None:
        day = self.day
        base: dict[str, str] = {
            "integration_base_code_format": sku.sku,
            "integration_model_renk": option.model_color,
            "PL_sezon": option.season,
            "integration_renk": option.colour.code,
            "integration_beden": sku.size,
            "integration_yurt_disi_barkod": sku.ean,
            "integration_erp_sezon": option.erp_season,
            # The tenant default copy of the shared key: English here.
            SHARED_DESCRIPTION_KEY: cat.description(option.category, "en", option.composition),
        }
        if option.comment:
            base["PL_comment"] = option.comment
        if option.composition:
            base["integration_erp_icerik"] = option.composition
        kwargs: dict[str, dict[str, str]] = {}

        def dropdown(key: str, label: str) -> None:
            labels = self.vocabulary[key]
            base[key] = f"{labels.index(label) + 1:03d}"
            kwargs[key] = {"value": base[key], "label": label}

        dropdown("integration_renk", option.colour.code)
        dropdown("integration_beden", sku.size)
        dropdown("integration_cinsiyet", option.gender)
        dropdown("integration_alt_grup", option.sub_category)
        dropdown("integration_urun_ana_grup", option.main_category)
        dropdown("integration_renk_detay", option.colour.detail)
        dropdown("integration_renk_tonu", option.colour.family)
        for key, label in option.filters.items():
            dropdown(key, label)

        views: dict[str | None, dict[str, str]] = {None: base}
        for market in MARKETS:
            locale = LOCALE_OF[market]
            view = views.setdefault(locale, {})
            name = sku.name_override.get(market) or (
                option.renamed_day2.get(market) if day >= 2 else None
            ) or option.names[market]
            if name:
                if market == "CZ" and option.misfiled_cz:
                    views.setdefault("hu", {})[NAME_KEY[market]] = name
                else:
                    view[NAME_KEY[market]] = name
            if option.details[market]:
                view[DETAIL_KEY[market]] = option.details[market] or ""
            if option.descriptions[market]:
                view[DESCRIPTION_KEY[market]] = option.descriptions[market] or ""
            view[SHARED_DESCRIPTION_KEY] = option.shared_descriptions[market]

        product = Product(
            pk=sku.pk,
            sku=sku.sku,
            base_code=option.base_code,
            product_type="0",
            name=option.style_name["en"],
            views=views,
            kwargs=kwargs,
            localized={
                LOCALE_OF[m]: {"l_name": option.storefront_names[m]} for m in MARKETS
            },
            images=[option.image_url] if option.image.get(day) else [],
            modified=option.modified,
            option=option,
        )
        self.products[product.pk] = product
        self.by_sku[product.sku] = product.pk

    def _add_grouping_row(self, option: Option) -> None:
        """The style's product_meta: sku is the bare base code, type 1."""
        siblings = [o for o in self.catalog.options if o.base_code == option.base_code]
        views: dict[str | None, dict[str, str]] = {
            None: {"integration_base_code_format": option.sizes[0].sku}
        }
        for market in MARKETS:
            locale = LOCALE_OF[market]
            if market == "CZ" and not any("CZ" in s.grouping_names for s in siblings):
                continue
            views.setdefault(locale, {})[NAME_KEY[market]] = option.style_name[locale]
        pk = self.catalog.grouping_pks[option.base_code]
        product = Product(
            pk=pk,
            sku=option.base_code,
            base_code=option.base_code,
            product_type=str(PRODUCT_META),
            name=option.style_name["en"],
            views=views,
            kwargs={},
            localized={},
            images=[],
            modified=option.modified,
        )
        self.products[pk] = product
        self.by_sku[product.sku] = pk

    def _add_liveness(self, option: Option, sku: cat.Sku) -> None:
        day = self.day
        for market in MARKETS:
            pair = sku.price.get(market)
            if pair and not (day >= 2 and market in sku.price_gone_day2):
                self.prices.append(
                    {
                        "pk": sku.pk * 10 + MARKETS.index(market),
                        "product": sku.pk,
                        "price_list": cat.PRICE_LIST[market],
                        "price": pair[0],
                        "retail_price": pair[1],
                        "currency_type": {"PL": "pln", "CZ": "czk", "SK": "eur",
                                          "HU": "huf"}[market],
                        "modified_date": _iso(option.modified),
                    }
                )
            units = sku.stock.get(market, 0)
            if day >= 2 and market in sku.sold_out_day2:
                units = 0
            self.stocks.append(
                {
                    "pk": sku.pk * 10 + MARKETS.index(market),
                    "product": sku.pk,
                    "stock_list": cat.STOCK_LIST[market],
                    "stock": units,
                    "modified_date": _iso(option.modified),
                }
            )

    # ── writes, and the journal that carries them across processes ────────

    def _replay(self) -> None:
        if self.journal is None or not self.journal.exists():
            return
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            if line.strip():
                self._apply(json.loads(line), record=False)

    def _apply(self, op: dict[str, Any], record: bool = True) -> dict[str, Any]:
        with self._lock:
            kind = op["op"]
            result: dict[str, Any] = {}
            if kind == "patch":
                product = self.products[op["pk"]]
                locale = op.get("locale")
                for key, value in op["attributes"].items():
                    if key in LOCALIZABLE and locale is not None:
                        product.views.setdefault(locale, {})[key] = value
                    else:
                        product.views[None][key] = value
                product.modified = _parse(op.get("at")) or product.modified
                result = {"pk": product.pk}
            elif kind == "delete_item":
                self.items.pop(op["pk"], None)
            elif kind == "create_item":
                at = _parse(op.get("at")) or EPOCH
                self.items[op["pk"]] = (op["product"], op["catalog"], at)
                self._next_item = max(self._next_item, op["pk"] + 1)
                result = {"pk": op["pk"], "product": op["product"], "catalog": op["catalog"]}
            if record and self.journal is not None:
                self.journal.parent.mkdir(parents=True, exist_ok=True)
                with self.journal.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(op, ensure_ascii=False) + "\n")
            return result

    def patch(self, pk: int, attributes: dict[str, str], locale: str | None) -> dict[str, Any]:
        return self._apply(
            {"op": "patch", "pk": pk, "locale": locale, "attributes": attributes,
             "at": _iso(datetime.now(UTC))}
        )

    def delete_item(self, pk: int) -> None:
        self._apply({"op": "delete_item", "pk": pk})

    def create_item(self, product: int, catalog: int) -> dict[str, Any]:
        with self._lock:
            pk = self._next_item
            return self._apply(
                {"op": "create_item", "pk": pk, "product": product, "catalog": catalog,
                 "at": _iso(datetime.now(UTC))}
            )

    # ── reads the mocks share ──────────────────────────────────────────────

    def listing(self, market: str) -> list[dict[str, Any]]:
        """What one shop is selling: a card per option, carrying its texts.

        A shop lists an option it holds in its catalogue, prices and stocks
        in its market, has a photo for, and merch has not removed — and then
        disagrees with that rule on a few options, as the real ones do.
        """
        locale = LOCALE_OF[market]
        price_lists = {r["product"] for r in self.prices
                       if r["price_list"] == cat.PRICE_LIST[market]}
        stocked = {r["product"] for r in self.stocks
                   if r["stock_list"] == cat.STOCK_LIST[market] and r["stock"] > 0}
        readers = {cat.OWN_CATALOG[market], cat.SHARED_CATALOG}
        rows: list[dict[str, Any]] = []
        with self._lock:
            members = {p for (p, c, _m) in self.items.values() if c in readers}
            for option in self.catalog.options:
                sizes = [self.products[self.by_sku[s.sku]] for s in option.sizes
                         if s.sku in self.by_sku]
                if not sizes or not {p.pk for p in sizes} & members:
                    continue
                pks = {p.pk for p in sizes}
                sellable = bool(
                    pks & price_lists and pks & stocked and sizes[0].images
                    and (option.comment or "").strip().lower() not in _BANNING
                )
                listed = option.shop_disagrees.get(market, sellable)
                if not listed:
                    continue
                view = sizes[0].record(locale)["attributes"]
                attributes = {
                    "integration_model_renk": option.model_color,
                    **{
                        key: view[key]
                        for key in (NAME_KEY[market], DETAIL_KEY[market],
                                    DESCRIPTION_KEY[market], "PL_sezon", "PL_comment")
                        if view.get(key)
                    },
                }
                rows.append(
                    {
                        "pk": sizes[0].pk,
                        "name": option.storefront_names[market],
                        "base_code": option.base_code,
                        "listing_code": option.colour.code,
                        "attributes": attributes,
                    }
                )
        return rows

    # ── the asset library ──────────────────────────────────────────────────

    def assets(self) -> list[dict[str, Any]]:
        """Every asset in the library, in the shape its API returns.

        Product photos carry the library's own product and colour ids, which
        is what `brandlibrary/catalogue.match` trusts. A few options also have
        a second record under the same filename, one with a marketplace edition
        — the duplicate `dedupe` exists for — and every option gets a colour
        swatch the publish must skip. Campaign images sit outside the
        e-commerce category the scan is filtered to.
        """
        domain = "https://assets.example.test"
        records: list[dict[str, Any]] = []
        for option in self.catalog.options:
            if not option.library_photos:
                continue
            for n in range(1, option.library_photos + 1):
                name = f"{option.library_key}_{n:02d}"
                asset_id = f"{option.index:04d}{n:02d}A"
                marketplace = option.library_marketplace and n == 1
                records.append(_asset(domain, asset_id, name, option, marketplace))
            if option.index % 17 == 0:
                name = f"{option.library_key}_01"
                records.append(_asset(domain, f"{option.index:04d}01B", name, option, True))
            records.append(
                _asset(domain, f"{option.index:04d}99S", f"swatch_{option.library_key}",
                       option, False)
            )
        for n in range(1, 13):
            records.append(
                {
                    "id": f"CAMPAIGN{n:04d}",
                    "name": f"lookbook_story_{n:02d}",
                    "extension": ["jpg"],
                    "type": "image",
                    "property_Year": ["2026"],
                    "property_2017_Category": ["CAMPAIGN"],
                    "original": f"{domain}/m/CAMPAIGN{n:04d}/original/lookbook_story_{n:02d}.jpg",
                    "thumbnails": {},
                }
            )
        return records


def _asset(domain: str, asset_id: str, name: str, option: Option, marketplace: bool
           ) -> dict[str, Any]:
    thumbnails = {"webimage": f"{domain}/m/{asset_id}/webimage/{name}.jpg"}
    if marketplace:
        thumbnails["MKP_ZAL"] = f"{domain}/m/{asset_id}/marketplace/{name}.jpg"
    return {
        "id": asset_id,
        "name": name,
        "extension": ["jpg"],
        "type": "image",
        "property_BI_PRODUCT_ID": [option.base_code],
        "property_BI_COLOR_ID": [option.colour.code],
        "property_Year": [option.library_year],
        "property_2017_Category": ["ECOMMERCE"],
        "original": f"{domain}/m/{asset_id}/original/{name}.jpg",
        "thumbnails": thumbnails,
    }


def registry() -> dict[str, list[dict[str, Any]]]:
    """The attribute registry endpoints, as records."""
    attributes = [
        {
            "pk": ATTRIBUTE_PK[key],
            "key": key,
            "name": name,
            "data_type": data_type,
            "erp_code": key,
            "is_localizable": localizable,
            "is_visible": True,
            "is_searchable": data_type != "area",
            "is_filterable": data_type == "dropdown",
            "is_variant": key == "integration_beden",
            "created_date": _iso(EPOCH - timedelta(days=400)),
            "modified_date": _iso(EPOCH - timedelta(days=30)),
        }
        for key, name, data_type, localizable in _ATTRIBUTES
    ]
    sets = [
        {"pk": 11, "name": "Apparel", "attribute_set_type": "simple"},
        {"pk": 12, "name": "Footwear", "attribute_set_type": "simple"},
        {"pk": 13, "name": "Accessories", "attribute_set_type": "simple"},
    ]
    garment = {"integration_yaka", "integration_kol_boyu", "integration_kalip",
               "integration_boy", "integration_bel", "integration_kumas_tipi"}
    configs = []
    for set_pk in (11, 12, 13):
        for key, _name, _data_type, localizable in _ATTRIBUTES:
            if set_pk != 11 and key in garment:
                continue
            configs.append(
                {
                    "pk": set_pk * 1000 + ATTRIBUTE_PK[key] % 1000,
                    "attribute": ATTRIBUTE_PK[key],
                    "attribute_set": set_pk,
                    "is_required": key in ("integration_base_code_format", NAME_KEY["PL"]),
                    "is_visible": True,
                    "is_localizable": localizable,
                    "order": ATTRIBUTE_PK[key] - 1000,
                    "default_fields": ["is_localizable"] if localizable else [],
                }
            )
    return {"attributes": attributes, "sets": sets, "configs": configs}


def vocabulary_records(world: World, locale: str | None) -> list[dict[str, Any]]:
    """`attribute_value` rows; the label follows `Accept-Language`."""
    records: list[dict[str, Any]] = []
    for key, labels in world.vocabulary.items():
        attribute_pk = ATTRIBUTE_PK[key]
        for order, label in enumerate(labels, start=1):
            shown = label
            if locale in _LOCALE_INDEX:
                translated = _TRANSLATIONS.get(label, (None,) * 4)[_LOCALE_INDEX[locale]]
                shown = translated or label
            records.append(
                {
                    "pk": attribute_pk * 100 + order,
                    "attribute": attribute_pk,
                    "value": f"{order:03d}",
                    "label": shown,
                    "order": order,
                    "erp_code": f"{order:03d}",
                }
            )
    return records


# ── the process-wide world ─────────────────────────────────────────────────

_current: World | None = None
_current_key: tuple[Any, ...] | None = None
_override: World | None = None
_guard = threading.Lock()


def state_dir() -> Path:
    """Where the mocks keep what must outlive a process: `<UPLOADS_ROOT>/mock-pim`."""
    from ..config import settings

    return Path(settings.uploads_root) / "mock-pim"


def journal_path() -> Path:
    return state_dir() / "journal.jsonl"


def _size_marker() -> Path:
    return state_dir() / "catalog.json"


def record_size(size: int) -> None:
    """Note which catalogue the seed used, so the web server builds the same one."""
    _size_marker().parent.mkdir(parents=True, exist_ok=True)
    _size_marker().write_text(json.dumps({"size": size, "seed": cat.SEED}), encoding="utf-8")


def recorded_size() -> int:
    """The catalogue size the seed used, or the default when it never ran."""
    try:
        return int(json.loads(_size_marker().read_text(encoding="utf-8"))["size"])
    except (OSError, ValueError, KeyError, TypeError):
        return cat.DEFAULT_SIZE


def current() -> World:
    """The world every mock in this process reads, unless `use()` swapped it."""
    global _current, _current_key
    if _override is not None:
        return _override
    size = recorded_size()
    key = (size, str(journal_path()))
    with _guard:
        if _current is None or _current_key != key:
            _current = World(cat.build(size), cat.CURRENT_DAY, journal=journal_path())
            _current_key = key
        return _current


@contextmanager
def use(world: World) -> Iterator[World]:
    """Make `world` the one every mock sees, for the length of the block."""
    global _override
    previous = _override
    _override = world
    try:
        yield world
    finally:
        _override = previous


def reset() -> None:
    """Forget the cached world. The next `current()` rebuilds and replays."""
    global _current, _current_key
    with _guard:
        _current = None
        _current_key = None

