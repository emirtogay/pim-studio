"""The values stage — mapping parsing, locale routing, and resolution.

First tests this module has ever had, written alongside the locale fix. The
behaviours pinned are the ones that hid the `urun_adi_*` defect for months: a
mapping silently reading the wrong view, an empty value indistinguishable from
a withheld one, and a locale echo credited as a translation.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeColumn,
    AttributeValue,
    OmnitronAttribute,
    Product,
    StorefrontListing,
    ValueChange,
    ValueConflict,
)
from pim_studio.omnitron import OmnitronClient
from pim_studio.sync import values
from pim_studio.sync.runs import SyncRun
from pim_studio.sync.values import (
    LOCALE_CHAINS,
    Mapping,
    _resolve,
    assign_locale_chains,
    import_values,
    load_mappings,
)

pytestmark = pytest.mark.usefixtures("temp_db")

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}


def an_attribute(key: str, grain: str = "model_color", per_market: bool = True) -> int:
    with session_scope() as db:
        a = Attribute(
            key=key, label=key, write_channel="omnitron_import",
            value_grain=grain, per_market=per_market,
        )
        db.add(a)
        db.flush()
        return a.id


def a_column(
    attr_id: int, column_name: str, market: str | None = None, writable: bool = False
) -> None:
    with session_scope() as db:
        db.add(
            AttributeColumn(
                attribute_id=attr_id, source="omnitron",
                column_name=column_name, market=market, api_writable=writable,
            )
        )


def mirror(key: str, is_localizable: bool, pk: int) -> None:
    with session_scope() as db:
        db.add(
            OmnitronAttribute(
                pk=pk, key=key, name=key, data_type="text",
                is_localizable=is_localizable,
            )
        )


def our_product(sku: str, model_color: str = "L3558.132") -> None:
    with session_scope() as db:
        db.add(
            Product(
                sku=sku,
                base_code_format=f"{model_color}.T5",
                model_color=model_color,
                base_code=model_color.split(".")[0],
                size="T5",
            )
        )


def a_run() -> SyncRun:
    return SyncRun(row_id=1, kind="omnitron_values")


def calm_detector(monkeypatch: pytest.MonkeyPatch, confidence: float = 0.0) -> None:
    """Pin the language verdict so resolution tests are about routing."""
    monkeypatch.setattr(values, "_english", lambda _: confidence)


# ── load_mappings ──────────────────────────────────────────────────────────


def test_all_three_prefixes_parse_into_their_shapes() -> None:
    name = an_attribute("product_name")
    store = an_attribute("storefront_name")
    sub = an_attribute("sub_category", per_market=False)
    a_column(name, "attributes__urun_adi_pl", "PL")
    a_column(store, "localized_attributes__cs__l_name", "CZ")
    a_column(sub, "attributes_kwargs__integration_alt_grup__label")

    by_key = {m.key: m for m in load_mappings()}
    flat = by_key["urun_adi_pl"]
    assert (flat.market, flat.locale, flat.is_label) == ("PL", None, False)
    localized = by_key["l_name"]
    assert (localized.market, localized.locale) == ("CZ", "cs")
    label = by_key["integration_alt_grup"]
    assert (label.is_label, label.locale) == (True, None)


def test_the_label_wins_over_the_code_for_one_attribute_market() -> None:
    sub = an_attribute("sub_category", per_market=False)
    a_column(sub, "attributes__integration_alt_grup")
    a_column(sub, "attributes_kwargs__integration_alt_grup__label")

    mappings = load_mappings()
    assert len(mappings) == 1
    assert mappings[0].is_label


def test_bare_product_fields_are_skipped_not_mapped() -> None:
    """`name`, `sku`, `base_code` are product fields, not attribute paths —
    including the seeded-but-inert `("omnitron", "name", product_name)` row."""
    name = an_attribute("product_name")
    a_column(name, "name")
    a_column(name, "sku")
    a_column(name, "base_code")
    assert load_mappings() == []


# ── Mapping.read ───────────────────────────────────────────────────────────


def flat_mapping(key: str = "urun_adi_pl", market: str = "PL") -> Mapping:
    return Mapping(1, "product_name", "model_color", market, key, None)


def test_read_flat_kwargs_and_localized_paths() -> None:
    product = {
        "attributes": {"urun_adi_pl": "Koszulka"},
        "attributes_kwargs": {"integration_alt_grup": {"label": "T-Shirt"}},
        "localized_attributes": {"cs": {"l_name": "Polokošile"}},
    }
    assert flat_mapping().read(product) == "Koszulka"
    label = Mapping(1, "sub_category", "model_color", None, "integration_alt_grup",
                    None, is_label=True)
    assert label.read(product) == "T-Shirt"
    localized = Mapping(1, "storefront_name", "model_color", "CZ", "l_name", "cs")
    assert localized.read(product) == "Polokošile"


def test_read_coalesces_empty_and_whitespace_to_none() -> None:
    assert flat_mapping().read({"attributes": {"urun_adi_pl": ""}}) is None
    assert flat_mapping().read({"attributes": {"urun_adi_pl": "   "}}) is None
    assert flat_mapping().read({"attributes": {}}) is None
    assert flat_mapping().read({}) is None


# ── assign_locale_chains ───────────────────────────────────────────────────


def test_a_localizable_market_column_gets_its_chain() -> None:
    name = an_attribute("product_name")
    a_column(name, "attributes__urun_adi_pl", "PL")
    mirror("urun_adi_pl", is_localizable=True, pk=1798)

    mappings = load_mappings()
    fetch = assign_locale_chains(mappings)
    assert mappings[0].read_locales == LOCALE_CHAINS["PL"]
    assert fetch == (None, *[loc for loc in LOCALE_CHAINS["PL"] if loc is not None])


def test_a_non_localizable_column_stays_on_the_baseline() -> None:
    """One value regardless of header — extra passes would read it N times."""
    name = an_attribute("product_name")
    a_column(name, "attributes__urun_adi_pl", "PL")
    mirror("urun_adi_pl", is_localizable=False, pk=1798)

    mappings = load_mappings()
    assert assign_locale_chains(mappings) == (None,)
    assert mappings[0].read_locales == (None,)


def test_an_attribute_the_mirror_does_not_know_defaults_to_the_chain() -> None:
    """The chain ends in the baseline read, so the wrong default costs extra
    requests, never data — fail toward reading more."""
    name = an_attribute("product_name")
    a_column(name, "attributes__urun_adi_hu", "HU")

    mappings = load_mappings()
    assign_locale_chains(mappings)
    assert mappings[0].read_locales == LOCALE_CHAINS["HU"]


def test_labels_localized_and_agnostic_mappings_stay_on_the_baseline() -> None:
    sub = an_attribute("sub_category", per_market=False)
    store = an_attribute("storefront_name")
    season = an_attribute("erp_season", per_market=False)
    a_column(sub, "attributes_kwargs__integration_alt_grup__label", "PL")
    a_column(store, "localized_attributes__pl__l_name", "PL")
    a_column(season, "attributes__integration_erp_sezon")
    mirror("integration_alt_grup", is_localizable=True, pk=1)
    mirror("l_name", is_localizable=True, pk=2)
    mirror("integration_erp_sezon", is_localizable=True, pk=3)

    mappings = load_mappings()
    assert assign_locale_chains(mappings) == (None,)
    assert all(m.read_locales == (None,) for m in mappings)


# ── _resolve ───────────────────────────────────────────────────────────────


def chained(
    market: str = "PL",
    key: str = "urun_adi_pl",
    chain: tuple[str | None, ...] = ("pl", "en", None),
) -> Mapping:
    """A mapping with an explicit chain — these tests pin the walk's
    mechanics, not the tenant policy in `LOCALE_CHAINS`."""
    m = flat_mapping(key, market)
    m.read_locales = chain
    return m


def views(**by_locale: dict | None) -> dict[str | None, dict | None]:
    """Keyword-friendly records_by_locale; `base` stands for the None view."""
    return {
        (None if loc == "base" else loc): record
        for loc, record in by_locale.items()
    }


def attrs(**kv: str) -> dict:
    return {"attributes": dict(kv)}


def test_the_designated_locale_wins(monkeypatch) -> None:
    calm_detector(monkeypatch)
    tallies: dict = {}
    r = _resolve(
        chained(),
        views(base=attrs(), pl=attrs(urun_adi_pl="Koszulka Polo"), en=attrs()),
        tallies,
    )
    assert (r.value, r.locale, r.designated) == ("Koszulka Polo", "pl", True)
    assert r.suspect_flag is None
    assert tallies[("product_name", "PL")].counts["found_designated"] == 1


def test_a_fallback_locale_answers_when_the_designated_is_empty(monkeypatch) -> None:
    calm_detector(monkeypatch)
    tallies: dict = {}
    r = _resolve(
        chained(),
        views(base=attrs(), pl=attrs(), en=attrs(urun_adi_pl="Koszulka Polo")),
        tallies,
    )
    assert (r.value, r.locale, r.designated) == ("Koszulka Polo", "en", False)
    tally = tallies[("product_name", "PL")]
    assert tally.counts["found_fallback"] == 1
    assert dict(tally.fallback_locales) == {"en": 1}


def test_a_baseline_echo_is_written_but_counted_untranslated(monkeypatch) -> None:
    """The locale answered with the default-locale text — `labels.py`
    semantics say that is no translation. The value still lands (the
    locale-less fetch always wrote it) but under `equals_baseline`, so the
    untranslated population stays countable."""
    calm_detector(monkeypatch)
    tallies: dict = {}
    r = _resolve(
        chained(),
        views(
            base=attrs(urun_adi_pl="Pamuklu Polo"),
            pl=attrs(urun_adi_pl="Pamuklu Polo"),
            en=attrs(),
        ),
        tallies,
    )
    assert (r.value, r.locale, r.designated) == ("Pamuklu Polo", None, False)
    tally = tallies[("product_name", "PL")]
    assert tally.counts["equals_baseline"] == 1
    assert tally.counts["found_designated"] == 0
    assert tally.counts["found_baseline"] == 0


def test_a_baseline_only_value_counts_as_baseline_not_echo(monkeypatch) -> None:
    calm_detector(monkeypatch)
    tallies: dict = {}
    r = _resolve(
        chained(),
        views(base=attrs(urun_adi_pl="Koszulka Polo"), pl=attrs(), en=attrs()),
        tallies,
    )
    assert (r.value, r.locale) == ("Koszulka Polo", None)
    assert tallies[("product_name", "PL")].counts["found_baseline"] == 1


def test_english_under_the_designated_locale_is_written_flagged(monkeypatch) -> None:
    calm_detector(monkeypatch, confidence=0.95)
    tallies: dict = {}
    r = _resolve(
        chained(),
        views(base=attrs(), pl=attrs(urun_adi_pl="Cotton Polo Shirt"), en=attrs()),
        tallies,
    )
    assert r.value == "Cotton Polo Shirt"
    assert r.suspect_flag == "EN?"
    assert "0.95" in r.suspect_reason and "PL" in r.suspect_reason
    tally = tallies[("product_name", "PL")]
    assert tally.counts["found_designated"] == 1
    assert tally.counts["suspect_written"] == 1


def test_english_under_a_fallback_locale_is_skipped(monkeypatch) -> None:
    """Weak provenance and wrong language together are two defects
    compounding, not evidence."""
    calm_detector(monkeypatch, confidence=0.95)
    tallies: dict = {}
    r = _resolve(
        chained(),
        views(base=attrs(), pl=attrs(), en=attrs(urun_adi_pl="Cotton Polo Shirt")),
        tallies,
    )
    assert r is None
    assert tallies[("product_name", "PL")].counts["suspect_skipped"] == 1


def test_empty_under_every_locale_is_counted_empty(monkeypatch) -> None:
    calm_detector(monkeypatch)
    tallies: dict = {}
    assert _resolve(chained(), views(base=attrs(), pl=attrs(), en=attrs()), tallies) is None
    assert tallies[("product_name", "PL")].counts["empty"] == 1


def test_a_missing_locale_view_is_walked_past(monkeypatch) -> None:
    calm_detector(monkeypatch)
    tallies: dict = {}
    r = _resolve(chained(), views(base=attrs(urun_adi_pl="Koszulka"), pl=None), tallies)
    assert (r.value, r.locale) == ("Koszulka", None)


def test_a_baseline_only_mapping_reads_exactly_as_before(monkeypatch) -> None:
    """The pre-locale behaviour, byte for byte: baseline view, no tally."""
    calm_detector(monkeypatch)
    tallies: dict = {}
    mapping = flat_mapping("integration_erp_sezon", None)
    r = _resolve(mapping, views(base=attrs(integration_erp_sezon="24SS")), tallies)
    assert (r.value, r.locale, r.designated) == ("24SS", None, True)
    assert _resolve(mapping, views(base=attrs()), tallies) is None
    assert tallies == {}


def test_html_is_stripped_before_the_detector_judges() -> None:
    """`description` is HTML; lingua fed tag soup returns noise."""
    values._english.cache_clear()
    text = "<p>The classic long sleeve shirt is made of the finest cotton</p>"
    assert values._english(text) >= 0.90


# ── import_values, end to end ──────────────────────────────────────────────


def localised_api(
    per_locale: dict[str | None, dict[str, dict[str, str]]],
    requests_seen: list[tuple[str | None, str]] | None = None,
    metas: frozenset[str] = frozenset(),
) -> OmnitronClient:
    """Serves `sku__in` queries with attributes that depend on Accept-Language,
    which is exactly the Omnitron behaviour the whole fix is about."""

    def handler(request: httpx.Request) -> httpx.Response:
        locale = request.headers.get("accept-language")
        if requests_seen is not None:
            requests_seen.append((locale, request.url.params.get("sku__in", "")))
        by_sku = per_locale.get(locale, {})
        asked = [s for s in request.url.params.get("sku__in", "").split(",") if s]
        results = [
            {
                "sku": sku,
                "attributes": by_sku.get(sku, {}),
                # A grouping row answers to its bare base code and says so.
                # As a *string*, which is what the live endpoint sends — an
                # int here would let a `!= PRODUCT_META` guard pass the tests
                # and reject every real row.
                "product_type": "1" if sku in metas else "0",
            }
            for sku in asked
            if sku in per_locale.get(None, {})
        ]
        return httpx.Response(
            200, json={"count": len(results), "next": None, "results": results}
        )

    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    return c


def seed_two_markets() -> None:
    name = an_attribute("product_name")
    a_column(name, "attributes__urun_adi_pl", "PL")
    a_column(name, "attributes__urun_adi_cz", "CZ")
    mirror("urun_adi_pl", is_localizable=True, pk=1798)
    mirror("urun_adi_cz", is_localizable=True, pk=1799)
    our_product("100")
    our_product("101")


def stored_values() -> dict[tuple[str, str], AttributeValue]:
    with session_scope() as db:
        rows = list(db.scalars(select(AttributeValue)))
        return {(r.entity_key, r.market): r for r in rows}


def live_values() -> dict[tuple[str, str], AttributeValue]:
    """The `current` lane only — what the panel shows as the value."""
    with session_scope() as db:
        rows = db.scalars(select(AttributeValue).where(AttributeValue.state_lane == "current"))
        return {(r.entity_key, r.market): r for r in rows}


def waiting_proposals() -> dict[tuple[str, str], AttributeValue]:
    with session_scope() as db:
        rows = db.scalars(select(AttributeValue).where(AttributeValue.state_lane == "proposal"))
        return {(r.entity_key, r.market): r for r in rows}


def test_names_arrive_per_locale_and_land_per_market() -> None:
    """The headline case: Polish under `pl`, Czech misfiled under `hu`, both
    invisible to the locale-less read — and both now land, the misfiled one
    with its provenance on the row."""
    seed_two_markets()
    seen: list[tuple[str | None, str]] = []
    api = localised_api(
        {
            None: {"100": {}, "101": {}},
            "pl": {
                "100": {"urun_adi_pl": "Koszulka Polo z bawełny"},
                "101": {"urun_adi_pl": "Koszulka Polo z bawełny"},
            },
            "hu": {
                "100": {"urun_adi_cz": "Bavlněné polo tričko"},
                "101": {"urun_adi_cz": "Bavlněné polo tričko"},
            },
        },
        seen,
    )
    stats = import_values(api, a_run())

    rows = stored_values()
    assert rows[("L3558.132", "PL")].value == "Koszulka Polo z bawełny"
    assert rows[("L3558.132", "CZ")].value == "Bavlněné polo tričko"
    # Fallback provenance rides on source_ref; the writer keeps it immaterial.
    assert rows[("L3558.132", "CZ")].source_ref.endswith(":hu")
    assert not rows[("L3558.132", "PL")].source_ref.endswith(":pl")

    # One request per locale view per chunk: baseline + pl (PL chain) +
    # cs/hu (CZ chain) = 4, and the union is fetched exactly once for the
    # sizes — and once more, under the same four views, for the grouping row.
    sizes_asked = [loc for loc, skus in seen if "100" in skus.split(",")]
    assert sorted(sizes_asked, key=str) == sorted([None, "cs", "hu", "pl"], key=str)
    grouping_asked = [loc for loc, skus in seen if skus == "L3558"]
    assert sorted(grouping_asked, key=str) == sorted([None, "cs", "hu", "pl"], key=str)

    assert stats["fetch_locales"] == 4
    assert stats["locale_found_designated"] == 2   # PL, both SKUs
    assert stats["locale_found_fallback"] == 2     # CZ via hu, both SKUs
    assert stats["locale_empty"] == 0
    coverage = {(r["attribute"], r["market"]): r for r in stats["locale_coverage"]}
    assert coverage[("product_name", "CZ")]["fallback_locales"] == {"hu": 2}

    # Cross-locale duplicates never reach the conflict machinery.
    with session_scope() as db:
        assert db.query(ValueConflict).count() == 0


def test_agreeing_sizes_still_collapse_to_one_model_color_row() -> None:
    seed_two_markets()
    api = localised_api(
        {
            None: {"100": {}, "101": {}},
            "pl": {
                "100": {"urun_adi_pl": "Koszulka Polo"},
                "101": {"urun_adi_pl": "Koszulka Polo"},
            },
        }
    )
    import_values(api, a_run())
    rows = stored_values()
    assert set(rows) == {("L3558.132", "PL")}
    assert rows[("L3558.132", "PL")].entity_kind == "model_color"


def test_a_locale_suppressed_name_is_counted_not_silently_skipped() -> None:
    """The defect that hid the bug: nothing anywhere counted a value that came
    back empty. Now the tally says empty, per attribute and market."""
    seed_two_markets()
    api = localised_api({None: {"100": {}, "101": {}}})
    stats = import_values(api, a_run())
    assert stored_values() == {}
    coverage = {(r["attribute"], r["market"]): r for r in stats["locale_coverage"]}
    assert coverage[("product_name", "PL")]["empty"] == 2
    assert coverage[("product_name", "CZ")]["empty"] == 2


def test_suspect_verdicts_persist_on_the_row(monkeypatch) -> None:
    calm_detector(monkeypatch, confidence=0.95)
    seed_two_markets()
    api = localised_api(
        {
            None: {"100": {}, "101": {}},
            "pl": {
                "100": {"urun_adi_pl": "Cotton Polo Shirt"},
                "101": {"urun_adi_pl": "Cotton Polo Shirt"},
            },
        }
    )
    stats = import_values(api, a_run())
    row = stored_values()[("L3558.132", "PL")]
    assert row.suspect_flag == "EN?"
    assert "English" in row.suspect_reason
    assert stats["locale_suspect_written"] == 2


def test_a_non_localizable_registry_keeps_todays_single_fetch() -> None:
    """With nothing chained the stage makes exactly one pass — the regression
    guard for every mapping the locale work must not touch."""
    season = an_attribute("erp_season", per_market=False)
    a_column(season, "attributes__integration_erp_sezon")
    mirror("integration_erp_sezon", is_localizable=False, pk=10)
    our_product("100")
    seen: list[tuple[str | None, str]] = []
    api = localised_api(
        {None: {"100": {"integration_erp_sezon": "24SS"}}}, seen
    )
    stats = import_values(api, a_run())

    # One baseline view for the sizes and one for the grouping row; no locale
    # view anywhere, which is the guard.
    assert [loc for loc, _ in seen] == [None, None]
    assert stats["fetch_locales"] == 1
    assert stats["locale_coverage"] == []
    row = stored_values()[("L3558.132", "*")]
    assert (row.value, row.source_ref) == ("24SS", "sync_run:1")
    assert (row.suspect_flag, row.suspect_reason) == (None, None)


# ── one shared key, read per market, never shown ───────────────────────────


def test_a_market_attributes_column_parses_to_the_columns_market() -> None:
    """The market in the name only keeps `(source, column_name)` unique; the
    routing market is the column's own field."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__PL__integration_kalem_icerik", "PL")

    mapping = load_mappings()[0]
    assert (mapping.key, mapping.market, mapping.is_shared_key) == (
        "integration_kalem_icerik", "PL", True,
    )
    assert mapping.locale is None


def test_a_shared_key_sits_beside_its_sibling_and_demotes_nothing() -> None:
    """Both mappings stay — the sibling is the live value and its column name
    the export contract, the shared key is an offer — and neither outranks the
    other, because they no longer answer the same question."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__CZ__integration_kalem_icerik", "CZ")
    a_column(desc, "attributes__integration_kalem_icerik_cz", "CZ")

    by_key = {m.key: m for m in load_mappings()}
    assert len(by_key) == 2
    assert by_key["integration_kalem_icerik"].is_shared_key is True
    assert by_key["integration_kalem_icerik_cz"].is_shared_key is False
    assert not hasattr(by_key["integration_kalem_icerik_cz"], "is_fallback")


def test_a_shared_key_is_chained_even_when_the_mirror_is_silent() -> None:
    """It is read per market because it is localizable — that is the point of
    the mapping — so it does not wait on the mirror to say so."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__SK__integration_kalem_icerik", "SK")

    mappings = load_mappings()
    assert assign_locale_chains(mappings) == (None, "sk")
    assert mappings[0].read_locales == ("sk",)


def test_a_shared_key_reads_only_its_market_s_own_locale() -> None:
    """`hu` sits in the CZ chain for a misfiling in `urun_adi_cz`. In a key
    shared across markets the `hu` view is simply the Hungarian text, and
    offering it as Czech would be fluent, confident and wrong."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__CZ__integration_kalem_icerik", "CZ")
    assign_locale_chains(mappings := load_mappings())
    assert mappings[0].read_locales == ("cs",)

    got = _resolve(
        mappings[0],
        views(
            base=attrs(integration_kalem_icerik="Klasszikus pamut póló"),
            cs=attrs(),
            hu=attrs(integration_kalem_icerik="Klasszikus férfi pamut póló rövid ujjal"),
        ),
        {},
    )
    assert got is None


def test_a_shared_key_never_falls_back_to_the_locale_less_read() -> None:
    """For a key shared across markets the baseline is the tenant default —
    Turkish — so the tail every other mapping keeps would offer "1933'ten
    beri" for a Slovak cell. Measured live before this was pinned: 78 of 300
    SK descriptions came through as the Turkish master text."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__SK__integration_kalem_icerik", "SK")
    assign_locale_chains(mappings := load_mappings())
    assert None not in mappings[0].read_locales

    tallies: dict = {}
    turkish = "1933'ten beri zarif spor giyimin"
    got = _resolve(
        mappings[0],
        views(base=attrs(integration_kalem_icerik=turkish),
              sk=attrs(integration_kalem_icerik=turkish)),
        tallies,
    )
    assert got is None
    # Tallied apart from the sibling that answers for the same cell.
    assert tallies[("description·shared", "SK")].counts["equals_baseline"] == 1


def test_the_shared_key_never_becomes_the_live_value() -> None:
    """The shop renders the sibling. With both filled the sibling is written;
    with only the shared key filled the cell is missing, because that is what
    example.com shows: no description (NH5744FG.000, 2026-09-16)."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__PL__integration_kalem_icerik", "PL")
    a_column(desc, "attributes__integration_kalem_icerik_pol", "PL")
    mirror("integration_kalem_icerik", is_localizable=True, pk=232)
    mirror("integration_kalem_icerik_pol", is_localizable=False, pk=1732)
    our_product("100", model_color="AAA.111")
    our_product("200", model_color="BBB.222")

    api = localised_api(
        {
            None: {
                "100": {"integration_kalem_icerik_pol": "Kurtka kurierska"},
                "200": {},
            },
            "pl": {
                "100": {"integration_kalem_icerik": "Kurtka z kolekcji Northwind"},
                "200": {"integration_kalem_icerik": "Bluza z kapturem"},
            },
        }
    )
    import_values(api, a_run())

    rows = live_values()
    assert rows[("AAA.111", "PL")].value == "Kurtka kurierska"
    assert ("BBB.222", "PL") not in rows


def test_the_shared_key_on_one_size_never_contends_with_the_sibling_on_another() -> None:
    """They never share a pool, so a size carrying only the shared key cannot
    turn the option into a conflict."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__PL__integration_kalem_icerik", "PL")
    a_column(desc, "attributes__integration_kalem_icerik_pol", "PL")
    mirror("integration_kalem_icerik", is_localizable=True, pk=232)
    our_product("100")
    our_product("101")

    api = localised_api(
        {
            None: {"100": {"integration_kalem_icerik_pol": "Tekst kuratorski"}, "101": {}},
            "pl": {"100": {}, "101": {"integration_kalem_icerik": "Tekst wspólny"}},
        }
    )
    import_values(api, a_run())

    assert live_values()[("L3558.132", "PL")].value == "Tekst kuratorski"
    with session_scope() as db:
        assert db.query(ValueConflict).count() == 0


# ── confirming what a push sent ────────────────────────────────────────────


def test_an_exported_value_omnitron_now_holds_goes_live_and_a_differing_one_stays() -> None:
    """The nightly read is the standing read-back for a push: a row a batch
    wrote and could not confirm goes `live` the first night Omnitron reads it
    back unchanged, and one Omnitron reads differently is left as it is."""
    seed_two_markets()
    with session_scope() as db:
        name = db.scalar(select(Attribute).where(Attribute.key == "product_name"))
        assert name is not None
        db.add(AttributeValue(
            entity_kind="model_color", entity_key="L3558.132", attribute_id=name.id,
            market="PL", state="exported", entry_grain="model_color",
            source_kind="human", value="Koszulka polo", updated_by="p@example.com",
        ))
        db.add(AttributeValue(
            entity_kind="model_color", entity_key="L3558.132", attribute_id=name.id,
            market="CZ", state="exported", entry_grain="model_color",
            source_kind="human", value="Polokošile", updated_by="p@example.com",
        ))
    api = localised_api(
        {
            None: {"100": {}, "101": {}},
            "pl": {
                "100": {"urun_adi_pl": "Koszulka polo"},
                "101": {"urun_adi_pl": "Koszulka polo"},
            },
            "cs": {
                "100": {"urun_adi_cz": "Něco jiného"},
                "101": {"urun_adi_cz": "Něco jiného"},
            },
        },
        [],
    )
    stats = import_values(api, a_run())
    assert stats["confirmed"] == 1
    rows = stored_values()
    assert rows[("L3558.132", "PL")].state == "live"
    assert rows[("L3558.132", "PL")].value == "Koszulka polo"
    assert rows[("L3558.132", "CZ")].state == "exported"
    assert rows[("L3558.132", "CZ")].value == "Polokošile"
    assert stats["protected"] == 1


# ── what the shop shows, and what the shared key offers ────────────────────

POLISH = (
    "Damskie skórzane sneakersy Rallycourt z gładkiej skóry licowej na grubej "
    "podeszwie"
)
ENGLISH = (
    "Women's Rallycourt Leather Sneakers in smooth leather with a chunky rubber "
    "outsole"
)
TURKISH = "Erkek Kısa Kollu Klasik Kesim Polo Yaka Tişört pamuklu kumaş"


def _description_from_both_keys() -> int:
    """A description the PL shop renders from `_pol`, with the shared key
    beside it. `_pol` is pushable, as it is in the seed."""
    desc = an_attribute("description")
    a_column(desc, "market_attributes__PL__integration_kalem_icerik", "PL")
    a_column(desc, "attributes__integration_kalem_icerik_pol", "PL", writable=True)
    mirror("integration_kalem_icerik", is_localizable=True, pk=232)
    mirror("integration_kalem_icerik_pol", is_localizable=False, pk=1732)
    return desc


def listed(model_color: str, market: str = "PL") -> None:
    with session_scope() as db:
        db.add(StorefrontListing(model_color=model_color, market=market))


def both_keys(sibling: dict[str, str], shared: dict[str, str]) -> OmnitronClient:
    """Sizes -> `_pol` text in the baseline view, sizes -> shared text under `pl`."""
    return localised_api(
        {
            None: {
                sku: ({"integration_kalem_icerik_pol": text} if text else {})
                for sku, text in sibling.items()
            },
            "pl": {
                sku: ({"integration_kalem_icerik": text} if text else {})
                for sku, text in shared.items()
            },
        }
    )


def test_the_panel_shows_the_shop_s_english_and_offers_the_shared_key_s_polish() -> None:
    """784RMA0950.2A9, the report that started this: example.com shows English
    from `_pol`, the panel showed the shared key. Now the panel shows what the
    shop shows, and the Polish waits beside it as a proposal."""
    _description_from_both_keys()
    our_product("100", model_color="AAA.111")

    stats = import_values(both_keys({"100": ENGLISH}, {"100": POLISH}), a_run())

    assert live_values()[("AAA.111", "PL")].value == ENGLISH
    proposal = waiting_proposals()[("AAA.111", "PL")]
    assert proposal.value == POLISH
    assert proposal.source_ref == "shared_key:english"
    assert proposal.suspect_flag == "EN?"
    assert stats["shared_key_proposed"] == 1


def test_sizes_split_between_turkish_and_polish_offer_the_polish() -> None:
    """The same product's shared key, size by size: the Turkish master on most
    sizes and Polish on the rest. The vote used to crown the Turkish; the
    language tier now knows Turkish, so the Polish is what is offered."""
    _description_from_both_keys()
    for sku in ("100", "101", "102"):
        our_product(sku, model_color="AAA.111")

    import_values(
        both_keys(
            {"100": ENGLISH, "101": ENGLISH, "102": ENGLISH},
            {"100": TURKISH, "101": TURKISH, "102": POLISH},
        ),
        a_run(),
    )

    assert live_values()[("AAA.111", "PL")].value == ENGLISH
    assert waiting_proposals()[("AAA.111", "PL")].value == POLISH


def test_rival_local_texts_in_the_shared_key_offer_nothing() -> None:
    """A live value has to be something, so a vote settles it and flags it. A
    proposal is optional, and the larger of two rival texts is not an answer."""
    _description_from_both_keys()
    for sku in ("100", "101", "102"):
        our_product(sku, model_color="AAA.111")

    stats = import_values(
        both_keys(
            {"100": ENGLISH, "101": ENGLISH, "102": ENGLISH},
            {"100": POLISH, "101": POLISH, "102": "Męska kurtka pikowana z kapturem"},
        ),
        a_run(),
    )

    assert waiting_proposals() == {}
    assert stats["shared_key_proposed"] == 0


def test_local_text_on_the_shop_is_never_offered_a_replacement() -> None:
    _description_from_both_keys()
    our_product("100", model_color="AAA.111")
    our_product("200", model_color="BBB.222")

    import_values(
        both_keys(
            {"100": POLISH, "200": POLISH},
            {"100": TURKISH, "200": "Męska kurtka pikowana z kapturem i kieszeniami"},
        ),
        a_run(),
    )

    rows = live_values()
    assert rows[("AAA.111", "PL")].value == POLISH
    assert rows[("BBB.222", "PL")].value == POLISH
    assert waiting_proposals() == {}


def test_a_shared_key_in_the_wrong_language_is_not_offered_either() -> None:
    _description_from_both_keys()
    our_product("100", model_color="AAA.111")

    import_values(both_keys({"100": ENGLISH}, {"100": TURKISH}), a_run())

    assert waiting_proposals() == {}


def test_an_empty_description_on_a_listed_option_is_offered_the_shared_key() -> None:
    """The shop shows nothing, and the shared key holds Polish: that is a
    proposal. An option the shop is not selling gets none — nobody is reading
    its empty description."""
    _description_from_both_keys()
    our_product("100", model_color="AAA.111")
    our_product("200", model_color="BBB.222")
    listed("AAA.111")

    import_values(both_keys({"100": "", "200": ""}, {"100": POLISH, "200": POLISH}), a_run())

    assert live_values() == {}
    proposals = waiting_proposals()
    assert set(proposals) == {("AAA.111", "PL")}
    assert proposals[("AAA.111", "PL")].source_ref == "shared_key:missing"
    assert proposals[("AAA.111", "PL")].suspect_flag is None


def test_a_description_the_rendered_key_no_longer_holds_is_cleared() -> None:
    """The transition, and every night after: a live description whose key is
    now empty goes, and says what it was. A person's value stays, and so does
    anything on an option this run did not read."""
    desc = _description_from_both_keys()
    our_product("100", model_color="AAA.111")
    our_product("200", model_color="BBB.222")
    with session_scope() as db:
        for key, state in (("AAA.111", "live"), ("BBB.222", "approved"), ("ZZZ.999", "live")):
            db.add(AttributeValue(
                entity_kind="model_color", entity_key=key, attribute_id=desc,
                market="PL", state=state, entry_grain="model_color",
                source_kind="omnitron_api", value=TURKISH,
            ))

    stats = import_values(both_keys({"100": "", "200": ""}, {"100": "", "200": ""}), a_run())

    rows = live_values()
    assert ("AAA.111", "PL") not in rows
    assert rows[("BBB.222", "PL")].state == "approved"
    assert ("ZZZ.999", "PL") in rows
    assert stats["cleared_unrendered"] == 1
    with session_scope() as db:
        (change,) = db.scalars(select(ValueChange)).all()
    assert (change.entity_key, change.old_value, change.new_value) == (
        "AAA.111", TURKISH, None,
    )
    assert change.sync_run_id == 1


def test_a_shared_key_proposal_is_withdrawn_once_the_shop_reads_local() -> None:
    _description_from_both_keys()
    our_product("100", model_color="AAA.111")

    import_values(both_keys({"100": ENGLISH}, {"100": POLISH}), a_run())
    assert ("AAA.111", "PL") in waiting_proposals()

    stats = import_values(both_keys({"100": POLISH}, {"100": POLISH}), a_run())
    assert waiting_proposals() == {}
    assert stats["shared_key_withdrawn"] == 1


def test_another_source_s_proposal_and_a_person_s_value_are_left_alone() -> None:
    desc = _description_from_both_keys()
    our_product("100", model_color="AAA.111")
    our_product("200", model_color="BBB.222")
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="model_color", entity_key="AAA.111", attribute_id=desc,
            market="PL", state="proposed", state_lane="proposal",
            entry_grain="model_color", source_kind="proposal",
            source_ref="grouping_row:english", value="Tekst z wiersza grupującego",
        ))
        db.add(AttributeValue(
            entity_kind="model_color", entity_key="BBB.222", attribute_id=desc,
            market="PL", state="approved", entry_grain="model_color",
            source_kind="human", value=ENGLISH,
        ))

    import_values(both_keys({"100": ENGLISH, "200": ENGLISH}, {"100": POLISH, "200": POLISH}),
                  a_run())

    proposals = waiting_proposals()
    assert set(proposals) == {("AAA.111", "PL")}
    assert proposals[("AAA.111", "PL")].source_ref == "grouping_row:english"


# ── the grouping row ───────────────────────────────────────────────────────


def sizes_say(text: str, meta_text: str | None = None) -> dict:
    """Two sizes of L3558.132 saying `text` in Polish, and optionally the
    style's grouping row `L3558` saying `meta_text`."""
    baseline: dict[str, dict] = {"100": {}, "101": {}}
    polish = {"100": {"urun_adi_pl": text}, "101": {"urun_adi_pl": text}}
    if meta_text is not None:
        baseline["L3558"] = {}
        polish["L3558"] = {"urun_adi_pl": meta_text}
    return {None: baseline, "pl": polish}


def test_a_grouping_row_lands_under_its_base_code_and_never_inside_a_size() -> None:
    """The row that used to outvote eight sizes. Read again, but under its own
    key, so it can inform a proposal and cannot contend with a size."""
    seed_two_markets()
    api = localised_api(
        sizes_say("Koszulka Polo", meta_text="Koszulka Polo z bawełny organicznej"),
        metas=frozenset({"L3558"}),
    )
    stats = import_values(api, a_run())

    rows = stored_values()
    assert rows[("L3558.132", "PL")].value == "Koszulka Polo"
    assert rows[("L3558.132", "PL")].entity_kind == "model_color"
    grouping = rows[("L3558", "PL")]
    assert grouping.value == "Koszulka Polo z bawełny organicznej"
    assert grouping.entity_kind == "base_code"
    assert grouping.entry_grain == "base_code"
    assert stats["grouping_rows"] == 1
    assert stats["grouping_rows_found"] == 1
    assert stats["grouping_rows_missing"] == 0
    # Two rows, two keys, no disagreement for the resolver to judge.
    with session_scope() as db:
        assert db.query(ValueConflict).count() == 0


@pytest.mark.parametrize("product_type", ["1", 1])
def test_the_grouping_row_is_recognised_whether_its_type_is_text_or_a_number(
    product_type: str | int,
) -> None:
    """`/api/v1/products/` sends `product_type` as the string `"1"`; other
    endpoints send the integer. Recognising only one of them would make the
    whole pass a silent no-op that every count reports as success.

    The predicate moved to `models.is_grouping_row` on 2026-09-09 and this
    module imports it. It used to be private here, which is how `sync/catalogue`
    came to carry its own `== PRODUCT_META` against the same endpoint and have
    a guard that never fired — so asserting through `values` rather than
    importing `models` directly is deliberate: it pins that this stage still
    reaches for the shared one.
    """
    assert values.is_grouping_row({"product_type": product_type}) is True
    assert values.is_grouping_row({"product_type": "0"}) is False
    assert values.is_grouping_row({}) is False
    assert values.is_grouping_row(None) is False


def test_a_style_without_a_grouping_row_is_counted_not_dropped() -> None:
    seed_two_markets()
    api = localised_api(sizes_say("Koszulka Polo"))
    run = a_run()
    stats = import_values(api, run)
    assert stats["grouping_rows"] == 1
    assert stats["grouping_rows_missing"] == 1
    assert run.rows_dropped == 0
    assert set(stored_values()) == {("L3558.132", "PL")}


def test_a_size_answering_to_a_base_code_is_not_read_as_the_grouping_row() -> None:
    """`product_type` is the test, not the key: whatever answers to `L3558`
    without being a grouping row is left unread rather than filed under the
    style, which would be a size in disguise again."""
    seed_two_markets()
    api = localised_api(sizes_say("Koszulka Polo", meta_text="Koszulka z grupy"))
    stats = import_values(api, a_run())
    assert stats["grouping_rows_found"] == 0
    assert stats["grouping_rows_missing"] == 1
    assert ("L3558", "PL") not in stored_values()


def test_a_grouping_row_offers_no_sku_grain_values() -> None:
    """Stock, price and size belong to things that can be bought."""
    seed_two_markets()
    size_label = an_attribute("size_label", grain="sku", per_market=False)
    a_column(size_label, "attributes__integration_beden")
    mirror("integration_beden", is_localizable=False, pk=1801)
    api = localised_api(
        {
            None: {
                "100": {"integration_beden": "T5"},
                "101": {"integration_beden": "T5"},
                "L3558": {"integration_beden": "T5"},
            }
        },
        metas=frozenset({"L3558"}),
    )
    import_values(api, a_run())
    rows = stored_values()
    assert ("100", "*") in rows
    assert ("101", "*") in rows
    assert ("L3558", "*") not in rows
