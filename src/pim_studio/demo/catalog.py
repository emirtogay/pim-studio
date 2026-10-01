"""The synthetic catalogue every demo mock is fed from.

One deterministic generator, so the PIM mock, the four storefronts, the asset
library and the photo server all describe the same products — a demo in which
the shop sells an option the PIM has never heard of, by accident rather than by
design, would be teaching the panel's reader to distrust the wrong thing.

Everything is invented: the brand is the fictional Northwind Apparel, the
product names are generic garment descriptions, and every code is generated.
The shape is not invented. Codes look like the real ones (`PO1234.410`, a
two-letter style prefix, four digits, a dot and a three-character colour), the
text is in the four market languages, and the gaps are the kinds this panel
exists to find:

- names and descriptions missing in one market or another;
- an English placeholder sitting in a Polish field;
- sizes of one option disagreeing about their own name (value conflicts);
- options with no photo, some of which the asset library holds;
- options not in every market's catalogue, not priced or not stocked
  somewhere, or listed by a shop that disagrees with the rules;
- merch "REMOVED" comments, and one nobody has classified.

**Two days.** `CURRENT_DAY` is the state the running app sees. Day 1 is the
state one sync earlier, which the seed reads first so the second read produces
real history — departures, sell-outs, renames, arrivals, deletions and photos
landing — through the sync's own code rather than through inserted rows.

`build()` is pure and cheap: same `size`, same catalogue, every time.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache

#: The seed every random choice here flows from. Changing it changes the demo.
SEED = 20260930

#: Options (model-colours) in the default catalogue.
DEFAULT_SIZE = 300

#: The day the running app sees. Day 1 is the read before it.
CURRENT_DAY = 2

MARKETS: tuple[str, ...] = ("PL", "CZ", "SK", "HU")

#: The PIM's locale per market — `cs`, not `cz`, as in `sync/labels.LOCALES`.
LOCALE_OF: dict[str, str] = {"PL": "pl", "CZ": "cs", "SK": "sk", "HU": "hu"}

#: Fixed reference instant for every date the catalogue carries, so the
#: generated records do not depend on when the seed ran.
EPOCH = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)

#: The catalogues `sync/scope.py` reads: each market's own, and the shared one.
OWN_CATALOG: dict[str, int] = {"PL": 4, "CZ": 40, "HU": 41, "SK": 172}
SHARED_CATALOG = 601

#: Price and stock lists per market, as `sync/scope.MARKETS` declares them.
PRICE_LIST: dict[str, int] = {"PL": 2, "CZ": 134, "HU": 135, "SK": 167}
STOCK_LIST: dict[str, int] = {"PL": 2, "CZ": 168, "HU": 167, "SK": 169}

#: Where the panel's own static mount serves the demo thumbnails from. The
#: seed renders one JPEG per imaged option into `static/demo/images/`.
IMAGE_URL_PREFIX = "/demo/images"


# ── reference data ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Colour:
    code: str
    rgb: tuple[int, int, int]
    #: `integration_renk_tonu` label — the storefront's colour filter.
    family: str
    #: `integration_renk_detay` label.
    detail: str
    #: Colour name per language: en, pl, cs, sk, hu.
    names: dict[str, str]


COLOURS: tuple[Colour, ...] = (
    Colour("001", (38, 38, 42), "BLACK", "DEEP BLACK",
           {"en": "black", "pl": "czarny", "cs": "černá", "sk": "čierna", "hu": "fekete"}),
    Colour("110", (236, 234, 228), "WHITE", "OFF WHITE",
           {"en": "white", "pl": "biały", "cs": "bílá", "sk": "biela", "hu": "fehér"}),
    Colour("410", (36, 52, 92), "BLUE", "NAVY",
           {"en": "navy", "pl": "granatowy", "cs": "tmavě modrá", "sk": "tmavomodrá",
            "hu": "sötétkék"}),
    Colour("420", (128, 168, 212), "BLUE", "LIGHT BLUE",
           {"en": "light blue", "pl": "jasnoniebieski", "cs": "světle modrá",
            "sk": "svetlomodrá", "hu": "világoskék"}),
    Colour("230", (214, 196, 160), "BEIGE", "SAND",
           {"en": "sand", "pl": "piaskowy", "cs": "písková", "sk": "piesková",
            "hu": "homokszín"}),
    Colour("620", (164, 40, 48), "RED", "BRICK RED",
           {"en": "brick red", "pl": "ceglasty", "cs": "cihlově červená",
            "sk": "tehlovo červená", "hu": "téglavörös"}),
    Colour("330", (62, 92, 70), "GREEN", "FOREST GREEN",
           {"en": "forest green", "pl": "butelkowa zieleń", "cs": "lesní zelená",
            "sk": "lesná zelená", "hu": "erdőzöld"}),
    Colour("093", (150, 150, 152), "GREY", "GREY MELANGE",
           {"en": "grey melange", "pl": "szary melanż", "cs": "šedý melír",
            "sk": "sivý melír", "hu": "szürke melír"}),
    Colour("250", (112, 78, 52), "BROWN", "CHESTNUT",
           {"en": "chestnut", "pl": "kasztanowy", "cs": "kaštanová", "sk": "gaštanová",
            "hu": "gesztenyebarna"}),
    Colour("A4B", (212, 166, 170), "PINK", "DUSTY ROSE",
           {"en": "dusty rose", "pl": "pudrowy róż", "cs": "pudrově růžová",
            "sk": "púdrovo ružová", "hu": "púderrózsaszín"}),
)

#: The four market languages plus English, in a fixed order.
LANGS = ("en", "pl", "cs", "sk", "hu")


def _t(en: str, pl: str, cs: str, sk: str, hu: str) -> dict[str, str]:
    return {"en": en, "pl": pl, "cs": cs, "sk": sk, "hu": hu}


@dataclass(frozen=True)
class Category:
    key: str
    #: Two-letter style prefix — the "AB" of `AB1234.123`.
    prefix: str
    #: `integration_alt_grup` label. Matches the sub-categories the registry's
    #: `applies_when_subcategory` lists, so not-applicable reads as such.
    sub_categories: tuple[str, ...]
    #: `integration_urun_ana_grup` label, one of the seeded vocabulary.
    main_category: str
    sizes: tuple[str, ...]
    #: Product names per language. A style takes one; all its colours share it.
    names: tuple[dict[str, str], ...]
    #: The long description, per language, before the bullet list.
    description: dict[str, str]
    compositions: tuple[str, ...]
    #: PIM dropdown key -> labels a product of this category may carry.
    #: Absent keys are absent on purpose: a belt has no sleeve.
    filters: dict[str, tuple[str, ...]]
    price_pl: tuple[int, int]
    weight: int


CATEGORIES: tuple[Category, ...] = (
    Category(
        "polos", "PO", ("Polo",), "Textile", ("XS", "S", "M", "L", "XL", "XXL"),
        (
            _t("Cotton piqué polo shirt", "Koszulka polo z bawełnianej piki",
               "Polo tričko z bavlněného piké", "Polo tričko z bavlneného piké",
               "Pamut piké pólóing"),
            _t("Slim fit polo shirt", "Koszulka polo o dopasowanym kroju",
               "Polo tričko v úzkém střihu", "Polo tričko v úzkom strihu",
               "Karcsúsított fazonú pólóing"),
            _t("Striped jersey polo shirt", "Koszulka polo w paski z dżerseju",
               "Pruhované polo tričko z žerzeje", "Pruhované polo tričko z džerseju",
               "Csíkos dzsörzé pólóing"),
            _t("Long-sleeve polo shirt", "Koszulka polo z długim rękawem",
               "Polo tričko s dlouhým rukávem", "Polo tričko s dlhým rukávom",
               "Hosszú ujjú pólóing"),
        ),
        _t("A classic piqué polo with a ribbed collar and a two-button placket.",
           "Klasyczna koszulka polo z dzianiny piki z prążkowanym kołnierzykiem "
           "i listwą na dwa guziki.",
           "Klasické polo tričko z piké s žebrovaným límečkem a légou na dva knoflíky.",
           "Klasické polo tričko z piké s rebrovaným golierom a légou na dva gombíky.",
           "Klasszikus piké pólóing bordázott gallérral és kétgombos patentpánttal."),
        ("100% COTTON", "95% COTTON 5% ELASTANE"),
        {
            "integration_yaka": ("POLO COLLAR",),
            "integration_kalip": ("CLASSIC FIT", "SLIM FIT", "REGULAR FIT"),
            "integration_kol_boyu": ("SHORT-SLEEVES",),
            "integration_desen": ("SOLID", "BLOCK COLOR", "PRINTED"),
            "integration_kumas_tipi": ("PIQUE", "JERSEY"),
            "integration_boy": ("REGULAR LENGTH",),
        },
        (199, 349), 26,
    ),
    Category(
        "shirts", "SH", ("Shirt",), "Textile", ("S", "M", "L", "XL", "XXL"),
        (
            _t("Oxford button-down shirt", "Koszula oxford z kołnierzykiem na guziki",
               "Košile oxford s propínacím límečkem", "Košeľa oxford s gombíkovým golierom",
               "Oxford ing gombos gallérral"),
            _t("Linen shirt", "Lniana koszula", "Lněná košile", "Ľanová košeľa",
               "Lenvászon ing"),
            _t("Checked flannel shirt", "Flanelowa koszula w kratę",
               "Flanelová košile s kostkovaným vzorem", "Flanelová kockovaná košeľa",
               "Kockás flanelling"),
            _t("Regular fit poplin shirt", "Koszula z popeliny o regularnym kroju",
               "Popelínová košile v rovném střihu", "Popelínová košeľa v rovnom strihu",
               "Normál fazonú poplin ing"),
        ),
        _t("A long-sleeved shirt with a buttoned front and a chest pocket.",
           "Koszula z długim rękawem, zapinana na guziki, z kieszonką na piersi.",
           "Košile s dlouhým rukávem, zapínáním na knoflíky a náprsní kapsou.",
           "Košeľa s dlhým rukávom, zapínaním na gombíky a náprsným vreckom.",
           "Hosszú ujjú, gombos ing mellzsebbel."),
        ("100% COTTON", "100% LINEN", "60% COTTON 40% POLYESTER"),
        {
            "integration_yaka": ("BUTTON-DOWN", "CLASSIC COLLAR"),
            "integration_kalip": ("REGULAR FIT", "SLIM FIT"),
            "integration_kol_boyu": ("LONG SLEEVES",),
            "integration_desen": ("SOLID", "CHECKED", "STRIPED"),
            "integration_kumas_tipi": ("OXFORD", "LINEN", "FLANNEL", "POPLIN"),
            "integration_boy": ("REGULAR LENGTH",),
        },
        (249, 449), 22,
    ),
    Category(
        "knitwear", "KW", ("Sweater",), "Textile", ("S", "M", "L", "XL"),
        (
            _t("Lambswool crew neck sweater", "Sweter z wełny jagnięcej z okrągłym dekoltem",
               "Svetr z jehněčí vlny s kulatým výstřihem",
               "Sveter z jahňacej vlny s okrúhlym výstrihom",
               "Bárányszőr kerek nyakú pulóver"),
            _t("Cotton cable-knit sweater", "Bawełniany sweter w warkocze",
               "Bavlněný svetr s copánkovým vzorem", "Bavlnený sveter s vrkočovým vzorom",
               "Csavart mintás pamut pulóver"),
            _t("Half-zip knitted sweater", "Sweter z zamkiem pod szyją",
               "Pletený svetr se zipem u krku", "Pletený sveter so zipsom pri krku",
               "Félcipzáras kötött pulóver"),
            _t("Merino V-neck sweater", "Sweter z wełny merino w serek",
               "Svetr z merino vlny s výstřihem do V",
               "Sveter z merino vlny s výstrihom do V", "Merinó V-nyakú pulóver"),
        ),
        _t("A soft knitted sweater in a regular fit with ribbed cuffs and hem.",
           "Miękki sweter o regularnym kroju ze ściągaczami przy mankietach i na dole.",
           "Měkký pletený svetr v rovném střihu s žebrovanými manžetami a lemem.",
           "Mäkký pletený sveter v rovnom strihu s rebrovanými manžetami a lemom.",
           "Puha, normál fazonú kötött pulóver bordázott mandzsettával és szegéllyel."),
        ("100% LAMBSWOOL", "100% COTTON", "100% MERINO WOOL", "70% COTTON 30% WOOL"),
        {
            "integration_yaka": ("CREW NECK", "V-NECK", "HALF ZIP"),
            "integration_kalip": ("REGULAR FIT",),
            "integration_kol_boyu": ("LONG SLEEVES",),
            "integration_desen": ("SOLID", "KNIT EFFECT"),
            "integration_kumas_tipi": ("KNIT",),
        },
        (299, 599), 20,
    ),
    Category(
        "trousers", "TR", ("Trousers",), "Textile", ("30", "32", "34", "36", "38"),
        (
            _t("Cotton chinos", "Bawełniane spodnie chino", "Bavlněné chino kalhoty",
               "Bavlnené chino nohavice", "Pamut chino nadrág"),
            _t("Stretch slim jeans", "Elastyczne jeansy o wąskim kroju",
               "Strečové džíny v úzkém střihu", "Strečové džínsy v úzkom strihu",
               "Sztreccs farmernadrág karcsúsított fazonnal"),
            _t("Wool blend trousers", "Spodnie z domieszką wełny",
               "Kalhoty s příměsí vlny", "Nohavice s prímesou vlny",
               "Gyapjúkeverék nadrág"),
            _t("Drawstring jogger trousers", "Spodnie dresowe ze ściągaczem",
               "Teplákové kalhoty se stahovací šňůrkou",
               "Teplákové nohavice so sťahovacou šnúrkou",
               "Húzózsinóros szabadidőnadrág"),
        ),
        _t("Five-pocket trousers with a zip fly and a button fastening.",
           "Spodnie z pięcioma kieszeniami, zapinane na zamek i guzik.",
           "Kalhoty s pěti kapsami, zapínáním na zip a knoflík.",
           "Nohavice s piatimi vreckami, zapínaním na zips a gombík.",
           "Ötzsebes nadrág cipzárral és gombbal."),
        ("98% COTTON 2% ELASTANE", "100% COTTON", "60% WOOL 40% POLYESTER"),
        {
            "integration_kalip": ("SLIM FIT", "REGULAR FIT", "LOOSE FIT"),
            "integration_desen": ("SOLID",),
            "integration_kumas_tipi": ("TWILL", "DENIM", "WOOL BLEND", "SWEAT"),
            "integration_boy": ("FULL LENGTH",),
            "integration_bel": ("MID RISE", "HIGH RISE"),
        },
        (249, 499), 16,
    ),
    Category(
        "footwear", "FW", ("Sneakers",), "Footwear", ("40", "41", "42", "43", "44", "45"),
        (
            _t("Leather low-top sneakers", "Skórzane sneakersy", "Kožené nízké tenisky",
               "Kožené nízke tenisky", "Bőr sportcipő"),
            _t("Suede loafers", "Zamszowe mokasyny", "Semišové mokasíny",
               "Semišové mokasíny", "Velúr mokaszin"),
            _t("Canvas sneakers", "Płócienne trampki", "Plátěné tenisky",
               "Plátenné tenisky", "Vászon tornacipő"),
        ),
        _t("Comfortable lace-up shoes on a rubber sole.",
           "Wygodne, sznurowane buty na gumowej podeszwie.",
           "Pohodlné šněrovací boty na gumové podrážce.",
           "Pohodlná šnurovacia obuv na gumenej podrážke.",
           "Kényelmes, fűzős cipő gumitalppal."),
        ("UPPER: LEATHER, SOLE: RUBBER", "UPPER: SUEDE, SOLE: RUBBER",
         "UPPER: COTTON, SOLE: RUBBER"),
        {"integration_desen": ("SOLID",)},
        (349, 699), 8,
    ),
    Category(
        "accessories", "AC", ("Belt", "Cap", "Bag", "Scarf"), "Leather Goods", ("OS",),
        (
            _t("Leather belt", "Skórzany pasek", "Kožený opasek", "Kožený opasok",
               "Bőröv"),
            _t("Cotton twill cap", "Bawełniana czapka z daszkiem", "Bavlněná kšiltovka",
               "Bavlnená šiltovka", "Pamut baseballsapka"),
            _t("Canvas tote bag", "Płócienna torba na zakupy", "Plátěná nákupní taška",
               "Plátenná nákupná taška", "Vászon bevásárlótáska"),
            _t("Knitted wool scarf", "Dzianinowy szalik z wełny", "Pletená vlněná šála",
               "Pletený vlnený šál", "Kötött gyapjúsál"),
        ),
        _t("An everyday accessory made from hard-wearing materials.",
           "Codzienny dodatek wykonany z trwałych materiałów.",
           "Každodenní doplněk z odolných materiálů.",
           "Každodenný doplnok z odolných materiálov.",
           "Strapabíró anyagokból készült, mindennapi kiegészítő."),
        ("100% LEATHER", "100% COTTON", "100% WOOL"),
        {"integration_desen": ("SOLID", "PRINTED")},
        (99, 249), 8,
    ),
)

#: The accessory sub-category follows the name the style took.
_ACCESSORY_SUB = ("Belt", "Cap", "Bag", "Scarf")
_ACCESSORY_MAIN = ("Leather Goods", "Textile", "Textile", "Textile")

GENDERS = ("MAN", "WOMAN", "UNISEX")

#: Seasons as the PL team writes them — `Outlet` in two casings on purpose, as
#: the season facet folds case and nothing else.
SEASONS = ("26AW", "26AW", "26SS", "26SS", "25AW", "25SS", "CONT", "OUTLET", "Outlet")
ERP_SEASON = {"26AW": "26AW", "26SS": "26SS", "25AW": "25AW", "25SS": "25SS",
              "CONT": "24AW", "OUTLET": "24SS", "Outlet": "24SS"}
LIBRARY_YEAR = {"26AW": "2026", "26SS": "2026", "25AW": "2025", "25SS": "2025",
                "CONT": "2024", "OUTLET": "2024", "Outlet": "2024"}

#: Bullet points appended to every description, per language.
_CARE = _t("Machine wash at 30°C", "Pranie w pralce w 30°C", "Praní v pračce na 30 °C",
           "Pranie v práčke pri 30 °C", "Gépben mosható 30 °C-on")
_COMPOSITION_WORD = _t("Composition", "Skład", "Složení", "Zloženie", "Összetétel")


# ── the catalogue ──────────────────────────────────────────────────────────


@dataclass
class Sku:
    sku: str
    size: str
    pk: int
    ean: str
    #: market -> (price, retail price) as decimal strings; absent = not priced.
    price: dict[str, tuple[str, str]]
    #: market -> units on the market's stock list.
    stock: dict[str, int]
    #: Day-2 changes: markets whose price row is withdrawn, and where it sells out.
    price_gone_day2: frozenset[str] = frozenset()
    sold_out_day2: frozenset[str] = frozenset()
    #: The PIM deletes this product before day 2 (`deleted_items`).
    deleted_day2: bool = False
    #: market -> a name this one size carries instead of the option's own.
    name_override: dict[str, str] = field(default_factory=dict)


@dataclass
class Option:
    index: int
    model_color: str
    base_code: str
    colour: Colour
    category: Category
    sub_category: str
    main_category: str
    gender: str
    style_name: dict[str, str]
    #: urun_adi_* per market; None is a missing name.
    names: dict[str, str | None]
    #: urun_detay_* per market — usually the name again (a second title field).
    details: dict[str, str | None]
    #: The market-suffixed description each shop renders; None is missing.
    descriptions: dict[str, str | None]
    #: `integration_kalem_icerik` under each market locale — always filled.
    shared_descriptions: dict[str, str]
    storefront_names: dict[str, str]
    season: str
    erp_season: str
    comment: str | None
    composition: str | None
    #: PIM dropdown key -> label.
    filters: dict[str, str]
    sizes: list[Sku]
    catalogs: dict[int, frozenset[int]]  # day -> catalogue pks
    image: dict[int, bool]  # day -> has a PIM image
    #: Asset library: how many product photos it holds (0 = absent), whether
    #: they carry a marketplace edition, and which year they are filed under.
    library_photos: int
    library_marketplace: bool
    library_year: str
    #: CZ name filed under the `hu` locale instead of `cs`.
    misfiled_cz: bool = False
    #: Text the style's grouping row holds for markets its sizes leave empty.
    grouping_names: dict[str, str] = field(default_factory=dict)
    #: Day-2 renames: market -> the new name.
    renamed_day2: dict[str, str] = field(default_factory=dict)
    #: The shop lists the option despite the rules (True) or omits it although
    #: it qualifies (False), per market. Absent = the shop follows the rules.
    shop_disagrees: dict[str, bool] = field(default_factory=dict)
    modified: datetime = EPOCH

    @property
    def library_key(self) -> str:
        """The asset library's spelling — underscored, as `brandlibrary_key`."""
        return self.model_color.replace(".", "_")

    @property
    def image_url(self) -> str:
        return f"{IMAGE_URL_PREFIX}/{self.library_key}.jpg"


@dataclass
class Catalog:
    options: list[Option]
    #: One grouping row per style: base code -> pk.
    grouping_pks: dict[str, int]

    def by_model_color(self) -> dict[str, Option]:
        return {o.model_color: o for o in self.options}


def _ean(rng: random.Random) -> str:
    """A 13-digit code in the in-store range (prefix 2), with a valid check digit."""
    body = "20" + "".join(str(rng.randrange(10)) for _ in range(10))
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def _money(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}"


def _prices(rng: random.Random, low: int, high: int) -> dict[str, tuple[str, str]]:
    """A price per market, derived from one PL list price, sometimes discounted."""
    base = rng.randrange(low, high + 1, 10) - 0.01
    discounted = rng.random() < 0.22
    factor = rng.choice((0.7, 0.8)) if discounted else 1.0

    def pair(list_price: float, decimals: int) -> tuple[str, str]:
        sale = list_price * factor
        if decimals == 0:
            sale = round(sale / 10) * 10 - 1
        return _money(sale, decimals), _money(list_price, decimals)

    return {
        "PL": pair(base, 2),
        "CZ": pair(round(base * 5.8 / 10) * 10 - 1, 0),
        "SK": pair(round(base * 0.24) - 0.05, 2),
        "HU": pair(round(base * 92 / 100) * 100 - 10, 0),
    }


def description(category: Category, lang: str, composition: str | None) -> str:
    """The HTML description for one language: a sentence, then the bullets."""
    bullets = [f"<li>{_CARE[lang]}</li>"]
    if composition:
        bullets.insert(0, f"<li>{_COMPOSITION_WORD[lang]}: {composition.lower()}</li>")
    return f"<p>{category.description[lang]}</p><ul>{''.join(bullets)}</ul>"


@lru_cache(maxsize=4)
def build(size: int = DEFAULT_SIZE, seed: int = SEED) -> Catalog:
    """The whole catalogue, deterministically. Cached: it is read many times."""
    rng = random.Random(seed)
    weights = [c.weight for c in CATEGORIES]
    options: list[Option] = []
    used_codes: set[str] = set()
    grouping_pks: dict[str, int] = {}
    next_pk = 100_001

    while len(options) < size:
        category = rng.choices(CATEGORIES, weights=weights)[0]
        while True:
            base_code = f"{category.prefix}{rng.randrange(1000, 10000)}"
            if base_code not in used_codes:
                used_codes.add(base_code)
                break
        name_index = rng.randrange(len(category.names))
        style_name = category.names[name_index]
        if category.key == "accessories":
            sub_category = _ACCESSORY_SUB[name_index]
            main_category = _ACCESSORY_MAIN[name_index]
        else:
            sub_category = category.sub_categories[0]
            main_category = category.main_category
        gender = rng.choice(GENDERS) if category.key in ("accessories", "footwear") else (
            rng.choice(("MAN", "MAN", "WOMAN"))
        )
        season = rng.choice(SEASONS)
        composition = rng.choice(category.compositions) if rng.random() > 0.08 else None
        filters = {key: rng.choice(labels) for key, labels in category.filters.items()}
        # Waist and length are the sparsest of the seven filter attributes in
        # the real registry; leave some unset rather than fill everything.
        for sparse in ("integration_bel", "integration_boy"):
            if sparse in filters and rng.random() < 0.3:
                del filters[sparse]

        colour_count = min(rng.choice((1, 2, 2, 3)), size - len(options))
        colours = rng.sample(COLOURS, colour_count)
        grouping_pks[base_code] = 500_000 + len(grouping_pks) + 1

        for colour in colours:
            index = len(options)
            model_color = f"{base_code}.{colour.code}"
            names: dict[str, str | None] = {
                m: style_name[LOCALE_OF[m]] for m in MARKETS
            }
            descriptions: dict[str, str | None] = {
                m: description(category, LOCALE_OF[m], composition) for m in MARKETS
            }
            shared = {
                m: description(category, LOCALE_OF[m], composition) for m in MARKETS
            }
            storefront_names = {
                m: f"{style_name[LOCALE_OF[m]]} · {colour.names[LOCALE_OF[m]]}"
                for m in MARKETS
            }

            sizes: list[Sku] = []
            prices = _prices(rng, *category.price_pl)
            for label in category.sizes:
                stock = {
                    m: (0 if rng.random() < 0.12 else rng.randrange(1, 40)) for m in MARKETS
                }
                sizes.append(
                    Sku(
                        sku=f"{model_color}.{label}",
                        size=label,
                        pk=next_pk,
                        ean=_ean(rng),
                        price=dict(prices),
                        stock=stock,
                    )
                )
                next_pk += 1

            day1 = _catalogues(rng)
            options.append(
                Option(
                    index=index,
                    model_color=model_color,
                    base_code=base_code,
                    colour=colour,
                    category=category,
                    sub_category=sub_category,
                    main_category=main_category,
                    gender=gender,
                    style_name=style_name,
                    names=names,
                    details=dict(names),
                    descriptions=descriptions,
                    shared_descriptions=shared,
                    storefront_names=storefront_names,
                    season=season,
                    erp_season=ERP_SEASON[season],
                    comment=None,
                    composition=composition,
                    filters=dict(filters),
                    sizes=sizes,
                    catalogs={1: day1, 2: day1},
                    image={1: True, 2: True},
                    library_photos=rng.choice((2, 3, 3, 4)),
                    library_marketplace=rng.random() < 0.5,
                    library_year=LIBRARY_YEAR[season],
                    modified=EPOCH - timedelta(days=rng.randrange(1, 180), minutes=index),
                )
            )

    _apply_gaps(rng, options)
    return Catalog(options=options, grouping_pks=grouping_pks)


def _catalogues(rng: random.Random) -> frozenset[int]:
    """Which catalogues hold an option: mostly the shared one, sometimes not."""
    roll = rng.random()
    if roll < 0.74:
        return frozenset({SHARED_CATALOG})
    if roll < 0.84:
        return frozenset({SHARED_CATALOG, OWN_CATALOG["PL"]})
    if roll < 0.92:
        return frozenset({OWN_CATALOG["PL"], OWN_CATALOG["CZ"]})
    if roll < 0.97:
        return frozenset({OWN_CATALOG["HU"], OWN_CATALOG["SK"]})
    return frozenset({OWN_CATALOG["PL"]})


def english_placeholder(option: Option) -> str:
    """English marketing copy pasted into a local field — long enough for the
    language rule to be sure of it, as real placeholders usually are."""
    return f"The {option.style_name['en'].lower()} you will wear every day"


def _pick(rng: random.Random, pool: list[Option], share: float) -> list[Option]:
    count = max(1, round(len(pool) * share)) if pool else 0
    return rng.sample(pool, min(count, len(pool)))


def _apply_gaps(rng: random.Random, options: list[Option]) -> None:
    """Spread the defects the panel's pages exist to show. Order matters."""
    by_style: dict[str, list[Option]] = {}
    for option in options:
        by_style.setdefault(option.base_code, []).append(option)

    # Missing names and descriptions in one market or another.
    for option in _pick(rng, options, 0.12):
        option.names["CZ"] = None
        option.details["CZ"] = None
    for option in _pick(rng, options, 0.08):
        option.names["SK"] = None
    for option in _pick(rng, options, 0.10):
        option.details["HU"] = None
    for option in _pick(rng, options, 0.30):
        # The shop renders the suffixed key and nothing else; the shared key
        # still holds the text, which `values` offers as a proposal.
        option.descriptions[rng.choice(MARKETS)] = None
    for option in _pick(rng, options, 0.06):
        option.descriptions["HU"] = None
        option.descriptions["SK"] = None

    # The second title field drifting from the first — "name ≠ detail".
    for option in _pick(rng, options, 0.10):
        market = rng.choice(("PL", "CZ"))
        detail = option.details[market]
        if detail:
            option.details[market] = f"{detail} {option.colour.names[LOCALE_OF[market]]}"

    # Czech text filed under the Hungarian locale, as the tenant once did.
    for option in _pick(rng, options, 0.04):
        if option.names["CZ"]:
            option.misfiled_cz = True

    # English placeholders in a Polish field, on styles with a sibling colour
    # that carries the Polish name — so the sibling can propose the fix.
    multi = [o for o in options if len(by_style[o.base_code]) > 1]
    placeholders = _pick(rng, multi, 0.05)
    for option in placeholders:
        option.names["PL"] = english_placeholder(option)

    # Sizes disagreeing about their own name. One English size is settled by
    # the language rule; two sizes carrying another style's name are settled
    # by majority — the tier the conflicts page exists for, since the majority
    # is the right answer here only because the data says three against two.
    eligible = [o for o in options if len(o.sizes) >= 4 and o not in placeholders]
    for n, option in enumerate(_pick(rng, eligible, 0.06)):
        names = option.category.names
        other = names[(names.index(option.style_name) + 1) % len(names)]
        if n % 3 == 0:
            option.sizes[0].name_override["PL"] = english_placeholder(option)
        elif n % 3 == 1:
            for sku in option.sizes[:2]:
                sku.name_override["PL"] = other["pl"]
        elif option.names["CZ"]:
            for sku in option.sizes[:2]:
                sku.name_override["CZ"] = other["cs"]

    # Grouping rows that hold the CZ name the sizes lack.
    for option in options:
        if option.names["CZ"] is None and rng.random() < 0.5:
            option.grouping_names["CZ"] = option.style_name["cs"]

    # Merch comments: a ban, a non-ban, and one nobody has classified.
    for option in _pick(rng, options, 0.03):
        option.comment = "REMOVED"
    for option in _pick(rng, options, 0.01):
        option.comment = "Removed, wrong image"
    for option in _pick(rng, options, 0.01):
        option.comment = "FR"
    unclassified = _pick(rng, options, 0.01)
    for option in unclassified:
        option.comment = "check photos before AW"

    # Photos: about a fifth have none in the PIM. The library holds most of
    # those; the rest are genuinely absent.
    imageless = _pick(rng, options, 0.20)
    for option in imageless:
        option.image = {1: False, 2: False}
        if rng.random() < 0.3:
            option.library_photos = 0
    # Half of the publishable ones land in the PIM by day 2 — the partner
    # uploaded the bundle — which is what moves `landed_at`.
    publishable = [o for o in imageless if o.library_photos]
    for option in publishable[: len(publishable) // 2]:
        option.image[2] = True
    # The library also holds assets for options it knows under no metadata
    # (see `brandlibrary/catalogue.candidate_keys`) — not modelled; every
    # demo asset carries its product and colour ids.

    # Prices and stock with holes in them.
    for option in _pick(rng, options, 0.04):
        for sku in option.sizes:
            sku.price.pop("HU", None)
    for option in _pick(rng, options, 0.03):
        for sku in option.sizes:
            sku.stock = {m: 0 for m in MARKETS}

    # ── what changes between day 1 and day 2 ──────────────────────────────
    stable = [o for o in options if o.catalogs[1] and o.image[1]]
    departed = _pick(rng, stable, 0.015)
    for option in departed:
        option.catalogs[2] = frozenset()
    for option in _pick(rng, [o for o in stable if o not in departed], 0.04):
        sku = rng.choice(option.sizes)
        sku.sold_out_day2 = frozenset({"PL"})
    for option in _pick(rng, [o for o in stable if o not in departed], 0.012):
        for sku in option.sizes:
            sku.price_gone_day2 = frozenset({"HU"})
    for option in _pick(rng, [o for o in stable if o not in departed], 0.03):
        option.renamed_day2["PL"] = f"{option.style_name['pl']} premium"
    deletions = _pick(rng, [o for o in stable if o not in departed and len(o.sizes) > 1], 0.008)
    for option in deletions:
        option.sizes[-1].deleted_day2 = True
    # New arrivals: in no catalogue on day 1.
    arrivals = _pick(rng, [o for o in stable if o not in departed], 0.02)
    for option in arrivals:
        option.catalogs[1] = frozenset()

    # Shops that disagree with the rules: lagging behind, or ahead of the data.
    for option in options:
        for market in MARKETS:
            roll = rng.random()
            if roll < 0.03:
                option.shop_disagrees[market] = False
            elif roll < 0.04:
                option.shop_disagrees[market] = True


def channels() -> list[dict[str, object]]:
    """The tenant's sales channels: our four and a few that are none of ours."""
    ours = [
        {"pk": 68, "name": "Northwind PL web shop", "catalog": 4, "is_active": False},
        {"pk": 101, "name": "Northwind CZ web shop", "catalog": 40, "is_active": True},
        {"pk": 102, "name": "Northwind HU web shop", "catalog": 41, "is_active": True},
        {"pk": 167, "name": "Northwind SK web shop", "catalog": 172, "is_active": True},
    ]
    # Channel ids `scope.KNOWN_FOREIGN_CHANNELS` classifies, with invented names:
    # the drift check reads pks, not names.
    foreign = [
        {"pk": 1, "name": "Home market web shop", "catalog": 1, "is_active": True},
        {"pk": 3, "name": "Marketplace A", "catalog": 5, "is_active": True},
        {"pk": 134, "name": "Marketplace B", "catalog": 7, "is_active": True},
        {"pk": 103, "name": "Retired market web shop", "catalog": 9, "is_active": False},
    ]
    return ours + foreign
