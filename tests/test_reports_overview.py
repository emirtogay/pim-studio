"""The funnel read out of the database, snapshotted, and compared to itself.

The stage arithmetic is tested in `test_rules_funnel`. What is tested here is
the join and everything that depends on it: that SKU grain survives the trip
from the mirror to the rules, that a source nobody has synced produces
indeterminate rather than a catalogue-wide false alarm, that the deep links
carry the exact facet strings the Products page filters on, and that a delta is
only ever reported against a baseline that is really comparable.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from pim_studio.db import session_scope
from pim_studio.models import (
    MARKET_AGNOSTIC,
    Attribute,
    AttributeValue,
    BanComment,
    CatalogueMembership,
    OverviewSnapshot,
    Product,
    ValueConflict,
)
from pim_studio.reports.overview import (
    ALL_SLICE,
    OVERVIEW_VERSION,
    build_overview,
    compute_deltas,
    load_funnel,
    record_snapshot,
)
from pim_studio.rules.coverage import STOCK_CAVEAT

pytestmark = pytest.mark.usefixtures("temp_db")

MARKETS = ("PL", "CZ", "SK", "HU")


# ── fixture building ───────────────────────────────────────────────────────


def attribute(key: str, **over) -> int:
    fields = {
        "key": key,
        "label": key.replace("_", " ").title(),
        "write_channel": "omnitron_import",
        "value_grain": "model_color",
        "per_market": False,
    }
    fields.update(over)
    with session_scope() as db:
        row = Attribute(**fields)
        db.add(row)
        db.flush()
        return row.id


def product(
    sku: str,
    model_color: str,
    markets: tuple[str, ...] = (),
    product_type: int | None = 0,
) -> None:
    with session_scope() as db:
        row = Product(
            sku=sku,
            base_code_format=sku,
            model_color=model_color,
            base_code=model_color.split(".")[0],
            omnitron_product_type=product_type,
        )
        db.add(row)
        db.flush()
        for market in markets:
            db.add(CatalogueMembership(product_id=row.id, market=market, catalog_pk=1))


def value(
    attribute_id: int,
    entity_key: str,
    text: str | None,
    market: str = MARKET_AGNOSTIC,
    entity_kind: str = "model_color",
) -> None:
    with session_scope() as db:
        db.add(
            AttributeValue(
                entity_kind=entity_kind,
                entity_key=entity_key,
                attribute_id=attribute_id,
                market=market,
                value=text,
                state="live",
                entry_grain=entity_kind,
                source_kind="omnitron",
            )
        )


def registry() -> dict[str, int]:
    """The attributes the funnel asks about, with their real ownership."""
    ids = {}
    for key in ("main_category", "sub_category", "gender", "composition"):
        ids[key] = attribute(key, write_channel="product_card")
    ids["image_0"] = attribute("image_0")
    ids["pl_sezon"] = attribute("pl_sezon")
    ids["product_name"] = attribute("product_name", per_market=True)
    ids["product_detail"] = attribute("product_detail", per_market=True)
    ids["description"] = attribute("description", per_market=True)
    ids["price"] = attribute("price", per_market=True, value_grain="sku")
    ids["stock"] = attribute("stock", per_market=True, value_grain="sku")
    return ids


def base_filled(
    ids: dict[str, int], mc: str, season: str = "26SS", blank: tuple[str, ...] = ()
) -> None:
    """One row per base attribute. `blank` writes the row empty — read as a
    real gap, which is not the same as omitting it (that is never read)."""
    for key in ("main_category", "sub_category", "gender", "composition"):
        value(ids[key], mc, "" if key in blank else key.upper())
    value(ids["image_0"], mc, f"https://img/{mc}.jpg")
    value(ids["pl_sezon"], mc, season)


def localised(
    ids: dict[str, int],
    mc: str,
    markets=MARKETS,
    descriptions: dict[str, str] | None = None,
) -> None:
    for market in markets:
        value(ids["product_name"], mc, f"Nazwa {market}", market)
        text = (descriptions or {}).get(market, f"Opis produktu {market}")
        value(ids["description"], mc, text, market)


def priced_and_stocked(ids: dict[str, int], sku: str, markets=MARKETS) -> None:
    for market in markets:
        value(ids["price"], sku, "349.00", market, entity_kind="sku")
        value(ids["stock"], sku, "5", market, entity_kind="sku")


def overview() -> dict:
    with session_scope() as db:
        return build_overview(load_funnel(db))


def stage_bucket(extract: dict, stage: str, market: str | None = None) -> dict:
    counts = extract["slices"][ALL_SLICE]["counts"][stage]
    return counts["markets"][market] if market else counts["shared"]


def drop_for(extract: dict, stage: str, market: str | None = None) -> dict:
    entry = next(d for d in extract["slices"][ALL_SLICE]["drops"] if d["to"] == stage)
    return entry["markets"][market] if market else entry["shared"]


@pytest.fixture
def catalogue():
    """Four options, one of each shape the funnel has to tell apart."""
    ids = registry()

    # A: complete and live everywhere.
    product("A.1.T4", "A.1", MARKETS)
    base_filled(ids, "A.1")
    localised(ids, "A.1")
    priced_and_stocked(ids, "A.1.T4")

    # B: everything but composition — the partner queue.
    product("B.1.T4", "B.1", MARKETS)
    base_filled(ids, "B.1", blank=("composition",))
    localised(ids, "B.1")
    priced_and_stocked(ids, "B.1.T4")

    # C: in the catalogue everywhere, but no price in PL.
    product("C.1.T4", "C.1", MARKETS)
    base_filled(ids, "C.1")
    localised(ids, "C.1")
    priced_and_stocked(ids, "C.1.T4", ("CZ", "SK", "HU"))
    value(ids["price"], "C.1.T4", "", "PL", entity_kind="sku")
    value(ids["stock"], "C.1.T4", "5", "PL", entity_kind="sku")

    # D: Poland only — outside the other three catalogues entirely.
    product("D.1.T4", "D.1", ("PL",))
    base_filled(ids, "D.1", season="OUTLET")
    localised(ids, "D.1")
    priced_and_stocked(ids, "D.1.T4", ("PL",))
    return ids


# ── The shape of the funnel ────────────────────────────────────────────────


def test_the_population_is_every_option_in_the_mirror(catalogue):
    assert overview()["population"] == 4


def test_a_shared_stage_is_reported_once_not_once_per_market(catalogue):
    counts = overview()["slices"][ALL_SLICE]["counts"]
    assert "shared" in counts["base"] and "markets" not in counts["base"]
    assert "markets" in counts["priced"]


def test_a_missing_base_attribute_drops_the_option_at_the_partner_stage(catalogue):
    extract = overview()
    assert stage_bucket(extract, "base")["passed"] == 3
    assert drop_for(extract, "base")["count"] == 1


def test_the_base_drop_names_the_attribute_and_its_owner(catalogue):
    reasons = drop_for(overview(), "base")["reasons"]
    assert [r["key"] for r in reasons] == ["composition"]
    assert reasons[0]["owner"] == "partner"
    assert "Turkey" in reasons[0]["remedy"]


def test_an_unpriced_market_drops_at_priced(catalogue):
    assert drop_for(overview(), "priced", "PL")["count"] == 1


def test_a_market_outside_the_catalogue_drops_at_priced_not_at_live(catalogue):
    # D is PL-only. In Czechia it is not a missing price, it is not sold there
    # — but it still has to leave the funnel somewhere a reader can act on.
    czech = drop_for(overview(), "priced", "CZ")
    assert czech["count"] == 1
    assert [r["key"] for r in czech["reasons"]] == ["not_in_market_catalogue"]


def test_population_is_conserved_in_every_market(catalogue):
    extract = overview()
    slice_ = extract["slices"][ALL_SLICE]
    for market in MARKETS:
        final = slice_["counts"]["live"]["markets"][market]
        dropped = sum(
            d["markets"][market]["count"] for d in slice_["drops"] if "markets" in d
        ) + sum(d["shared"]["count"] for d in slice_["drops"] if "shared" in d)
        assert final["passed"] + final["indeterminate"] + dropped == slice_["population"]


# ── Unknown discipline ─────────────────────────────────────────────────────


def test_a_never_read_description_is_indeterminate_not_a_gap():
    ids = registry()
    product("E.1.T4", "E.1", MARKETS)
    base_filled(ids, "E.1")
    priced_and_stocked(ids, "E.1.T4")
    for market in MARKETS:
        value(ids["product_name"], "E.1", f"Name {market}", market)
    # No `description` row anywhere: the column was never read.
    extract = overview()
    assert stage_bucket(extract, "localised", "PL")["indeterminate"] == 1
    assert stage_bucket(extract, "localised", "PL")["passed"] == 0
    assert drop_for(extract, "localised", "PL")["count"] == 0


def test_a_never_read_price_column_does_not_raise_a_catalogue_wide_alarm():
    ids = registry()
    product("F.1.T4", "F.1", MARKETS)
    base_filled(ids, "F.1")
    localised(ids, "F.1")
    # Stock synced, price never was.
    for market in MARKETS:
        value(ids["stock"], "F.1.T4", "5", market, entity_kind="sku")
    extract = overview()
    assert stage_bucket(extract, "priced", "PL")["indeterminate"] == 1
    assert drop_for(extract, "priced", "PL")["count"] == 0


def test_the_unread_caveat_names_the_attribute_it_could_not_judge():
    ids = registry()
    product("G.1.T4", "G.1", MARKETS)
    base_filled(ids, "G.1")
    priced_and_stocked(ids, "G.1.T4")
    for market in MARKETS:
        value(ids["product_name"], "G.1", "Name", market)
    caveat = next(c for c in overview()["caveats"] if c["key"] == "unread")
    assert "description" in caveat["text"]


# ── Live is not the last stage ─────────────────────────────────────────────


def test_an_option_live_despite_an_earlier_gap_is_counted_not_reconciled_away(
    catalogue,
):
    """B is missing composition but the site does not check composition."""
    slice_ = overview()["slices"][ALL_SLICE]
    assert slice_["liveNow"]["PL"] == 3
    assert slice_["counts"]["live"]["markets"]["PL"]["passed"] == 2
    assert slice_["liveDespiteGaps"] >= 1


def test_the_headline_counts_options_live_on_the_site(catalogue):
    headline = overview()["slices"][ALL_SLICE]["headline"]
    assert headline["live"] == 4
    assert headline["blocked"] == 0


def test_a_banned_option_is_blocked_in_every_market():
    ids = registry()
    with session_scope() as db:
        db.add(BanComment(comment="REMOVED", bans=True))
    pl_comment = attribute("pl_comment")
    product("H.1.T4", "H.1", MARKETS)
    base_filled(ids, "H.1")
    localised(ids, "H.1")
    priced_and_stocked(ids, "H.1.T4")
    value(pl_comment, "H.1", "REMOVED")
    slice_ = overview()["slices"][ALL_SLICE]
    assert slice_["headline"]["live"] == 0
    assert slice_["headline"]["blocked"] == 1


def test_a_product_meta_row_is_not_an_option_of_its_own():
    """4,449 of the panel's 18,036 options on 2026-08-20 were only ever a
    grouping row. None was live in any market; they sat in the grid and in the
    work queues as "No price" and "Not in catalogue", and in the denominator
    under every percentage on the page."""
    ids = registry()
    product("TH2312", "TH2312.166", MARKETS, product_type=1)
    base_filled(ids, "TH2312.166")

    with session_scope() as db:
        assert load_funnel(db).products == {}


def test_a_product_meta_row_adds_no_size_to_the_option_it_landed_in():
    """Its identifiers come from one arbitrary colour and size, so it lands
    inside a real ModelColor as a twin of a real size — and anything only it
    carries becomes that option's. `pl_comment` is the one that mattered: 404
    of the 780 options the panel banned in PL had no sellable SKU carrying a
    banning comment."""
    ids = registry()
    product("J.1.T4", "J.1", MARKETS)
    product("J", "J.1", MARKETS, product_type=1)
    base_filled(ids, "J.1")
    localised(ids, "J.1")
    priced_and_stocked(ids, "J.1.T4")

    with session_scope() as db:
        facts = load_funnel(db).products
    assert set(facts) == {"J.1"}
    assert overview()["slices"][ALL_SLICE]["headline"]["live"] == 1


# ── Deep links ─────────────────────────────────────────────────────────────


def test_price_links_carry_the_exact_facet_string_products_filters_on(catalogue):
    reason = next(
        r for r in drop_for(overview(), "priced", "PL")["reasons"] if r["key"] == "no_price"
    )
    assert reason["link"] == {"page": "products", "params": [["missing", "PL price"]]}


def test_localisation_links_point_at_the_market_that_is_short():
    ids = registry()
    product("I.1.T4", "I.1", MARKETS)
    base_filled(ids, "I.1")
    localised(ids, "I.1", ("PL", "CZ"))
    for market in ("SK", "HU"):
        value(ids["product_name"], "I.1", "", market)
        value(ids["description"], "I.1", "", market)
    priced_and_stocked(ids, "I.1.T4")
    links = [r["link"]["params"] for r in drop_for(overview(), "localised", "SK")["reasons"]]
    assert [["missing", "SK name"]] in links
    assert [["missing", "SK description"]] in links


def test_the_banned_reason_offers_no_link_rather_than_one_that_finds_nothing():
    ids = registry()
    with session_scope() as db:
        db.add(BanComment(comment="REMOVED", bans=True))
    pl_comment = attribute("pl_comment")
    product("J.1.T4", "J.1", MARKETS)
    base_filled(ids, "J.1")
    localised(ids, "J.1")
    priced_and_stocked(ids, "J.1.T4")
    value(pl_comment, "J.1", "REMOVED")
    reasons = drop_for(overview(), "priced", "PL")["reasons"]
    live_reasons = drop_for(overview(), "live", "PL")["reasons"]
    assert all(r["link"] is None for r in live_reasons) or reasons


# ── Seasons ────────────────────────────────────────────────────────────────


def test_each_season_gets_its_own_slice_and_all_holds_everything(catalogue):
    extract = overview()
    assert extract["slices"][ALL_SLICE]["population"] == 4
    assert extract["slices"]["26SS"]["population"] == 3
    assert extract["slices"]["OUTLET"]["population"] == 1


def test_seasons_are_listed_largest_first(catalogue):
    seasons = overview()["seasons"]
    assert [s["key"] for s in seasons] == ["26SS", "OUTLET"]
    assert [s["count"] for s in seasons] == [3, 1]


def test_an_option_with_no_season_is_its_own_slice_rather_than_dropped():
    ids = registry()
    product("K.1.T4", "K.1", MARKETS)
    base_filled(ids, "K.1", season="")
    extract = overview()
    assert extract["slices"][ALL_SLICE]["population"] == 1
    assert any(s["label"] == "No season" for s in extract["seasons"])


# ── Defects ────────────────────────────────────────────────────────────────


def test_english_in_a_local_field_is_a_defect_not_a_gap():
    ids = registry()
    product("L.1.T4", "L.1", MARKETS)
    base_filled(ids, "L.1")
    priced_and_stocked(ids, "L.1.T4")
    localised(
        ids,
        "L.1",
        descriptions={
            "SK": "This is a classic fit polo shirt made from breathable cotton pique."
        },
    )
    extract = overview()
    labels = [d["label"] for d in extract["slices"][ALL_SLICE]["defects"]]
    assert "SK description reads as English" in labels
    # And it is still a pass for the funnel: the field is filled.
    assert stage_bucket(extract, "localised", "SK")["passed"] == 1


def test_a_name_that_differs_from_its_detail_is_flagged():
    ids = registry()
    product("M.1.T4", "M.1", MARKETS)
    base_filled(ids, "M.1")
    localised(ids, "M.1")
    priced_and_stocked(ids, "M.1.T4")
    for market in MARKETS:
        value(ids["product_detail"], "M.1", f"Zupelnie inny opis {market}", market)
    labels = [d["label"] for d in overview()["slices"][ALL_SLICE]["defects"]]
    assert "PL name ≠ detail" in labels


def test_conflicting_values_across_sizes_are_counted(catalogue):
    with session_scope() as db:
        db.add(
            ValueConflict(
                entity_kind="model_color",
                entity_key="A.1",
                attribute_id=catalogue["product_name"],
                market="PL",
                candidates=json.dumps({"A": 2, "B": 1}),
                sku_count=3,
                sources="omnitron",
                resolved_value="A",
                resolved_rule="majority",
            )
        )
    assert overview()["conflicts"]["productName"] == 1


# ── Caveats ────────────────────────────────────────────────────────────────


def test_the_stock_caveat_travels_with_the_numbers(catalogue):
    texts = [c["text"] for c in overview()["caveats"]]
    assert STOCK_CAVEAT in texts


def test_the_missing_assortment_source_is_stated_rather_than_hidden(catalogue):
    caveat = next(c for c in overview()["caveats"] if c["key"] == "assortment")
    assert caveat["stage"] == "assortment"


def test_the_unchecked_category_rule_is_declared_on_the_live_stage(catalogue):
    caveat = next(c for c in overview()["caveats"] if c["key"] == "category")
    assert caveat["stage"] == "live"


def test_the_hungarian_locale_question_is_carried_into_the_report(catalogue):
    caveat = next(c for c in overview()["caveats"] if c["key"] == "hu_locale")
    assert "probe_locale" in caveat["text"]


def test_the_assortment_stage_ships_uncomputable_rather_than_absent(catalogue):
    assortment = next(s for s in overview()["stages"] if s["key"] == "assortment")
    assert assortment["computable"] is False


# ── Snapshots and deltas ───────────────────────────────────────────────────


def test_a_second_build_on_one_day_refines_the_row_rather_than_adding_one(catalogue):
    extract = overview()
    today = date(2026, 8, 25)
    with session_scope() as db:
        record_snapshot(db, extract, today)
    with session_scope() as db:
        record_snapshot(db, extract, today)
    with session_scope() as db:
        assert db.query(OverviewSnapshot).count() == 1


def test_two_days_are_two_rows(catalogue):
    extract = overview()
    with session_scope() as db:
        record_snapshot(db, extract, date(2026, 8, 24))
        record_snapshot(db, extract, date(2026, 8, 25))
    with session_scope() as db:
        assert db.query(OverviewSnapshot).count() == 2


def test_no_history_means_no_delta_rather_than_a_delta_of_zero(catalogue):
    extract = overview()
    with session_scope() as db:
        assert compute_deltas(db, extract, date(2026, 8, 25)) is None


def test_a_delta_is_measured_against_the_baseline_it_names(catalogue):
    extract = overview()
    baseline = json.loads(json.dumps(extract))
    baseline["slices"][ALL_SLICE]["headline"]["live"] = 1
    baseline["slices"][ALL_SLICE]["liveNow"]["PL"] = 1
    with session_scope() as db:
        record_snapshot(db, baseline, date(2026, 8, 17))
    with session_scope() as db:
        deltas = compute_deltas(db, extract, date(2026, 8, 25))
    assert deltas[ALL_SLICE]["live"] == 3
    assert deltas[ALL_SLICE]["baselineDate"] == "2026-08-17"
    assert deltas[ALL_SLICE]["ageDays"] == 8
    assert deltas[ALL_SLICE]["liveByMarket"]["PL"] == 2


def test_a_snapshot_from_a_different_definition_is_not_a_baseline(catalogue):
    extract = overview()
    with session_scope() as db:
        record_snapshot(db, extract, date(2026, 8, 17))
    with session_scope() as db:
        row = db.query(OverviewSnapshot).one()
        row.version = OVERVIEW_VERSION + 1
    with session_scope() as db:
        assert compute_deltas(db, extract, date(2026, 8, 25)) is None


def test_a_snapshot_keeps_the_numbers_and_drops_the_queues(catalogue):
    extract = overview()
    with session_scope() as db:
        record_snapshot(db, extract, date(2026, 8, 25))
    with session_scope() as db:
        payload = json.loads(db.query(OverviewSnapshot).one().payload)
    assert payload["slices"][ALL_SLICE]["headline"]["live"] == 4
    assert "drops" not in payload["slices"][ALL_SLICE]


# ── The empty mirror ───────────────────────────────────────────────────────


def test_an_empty_mirror_builds_a_valid_extract_rather_than_crashing():
    registry()
    extract = overview()
    assert extract["population"] == 0
    assert extract["slices"][ALL_SLICE]["population"] == 0
    assert extract["deltas"] is None
    assert [s["key"] for s in extract["stages"]][0] == "assortment"


# ── The terminal report ────────────────────────────────────────────────────


def test_the_cli_prints_the_same_numbers_the_page_draws(catalogue, capsys):
    from pim_studio.reports.__main__ import _funnel

    assert _funnel([]) == 0
    out = capsys.readouterr().out
    assert "GO-LIVE FUNNEL" in out
    assert "no baseline yet" in out
    # The population and the live count, exactly as the extract reports them.
    assert "options               4" in out
    assert "live on the site      4" in out


def test_the_cli_scopes_to_a_season(catalogue, capsys):
    from pim_studio.reports.__main__ import _funnel

    assert _funnel(["OUTLET"]) == 0
    out = capsys.readouterr().out
    assert "scope                 OUTLET" in out
    assert "options               1" in out


def test_an_unknown_season_lists_the_real_ones_rather_than_reporting_zero(
    catalogue, capsys
):
    from pim_studio.reports.__main__ import _funnel

    assert _funnel(["99XX"]) == 2
    captured = capsys.readouterr()
    assert "unknown season" in captured.err
    assert "26SS" in captured.out


def test_the_report_never_writes_a_snapshot(catalogue):
    from pim_studio.reports.__main__ import _funnel

    _funnel([])
    with session_scope() as db:
        assert db.query(OverviewSnapshot).count() == 0


# ── The deploy race ────────────────────────────────────────────────────────


def test_a_snapshot_that_cannot_be_written_does_not_cost_the_extract(catalogue):
    """The table may not exist yet when the host regenerates after a deploy.

    Losing the whole extract to protect a baseline nobody can read yet is the
    wrong trade: the panel would serve a stale catalogue until 03:00.
    """
    extract = overview()
    with session_scope() as db:
        OverviewSnapshot.__table__.drop(db.get_bind())
        assert record_snapshot(db, extract, date(2026, 8, 25)) is False


def test_an_unreadable_baseline_reads_as_no_baseline(catalogue):
    extract = overview()
    with session_scope() as db:
        OverviewSnapshot.__table__.drop(db.get_bind())
        assert compute_deltas(db, extract, date(2026, 8, 25)) is None


def test_a_successful_write_says_so(catalogue):
    with session_scope() as db:
        assert record_snapshot(db, overview(), date(2026, 8, 25)) is True
