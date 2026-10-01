"""Coverage read out of the database, and written into a workbook.

The rules are tested in `test_rules_coverage`. What is tested here is the join:
that the mirror's shape reaches the rules intact, that a source nobody has
synced produces `unknown` rather than a catalogue-wide false alarm, and that
the file a user downloads carries the same numbers the page shows.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from pim_studio.db import session_scope
from pim_studio.models import (
    MARKET_AGNOSTIC,
    Attribute,
    AttributeValue,
    BanComment,
    CatalogueMembership,
    Product,
)
from pim_studio.reports.coverage import (
    PRESENCE_ANY,
    Scope,
    apply_scope,
    build_report,
    gap_queue,
    load_catalogue,
)
from pim_studio.reports.values import (
    TooManyValues,
    ValueSelection,
    load_values,
)
from pim_studio.reports.workbook import build_workbook, filename_for
from pim_studio.rules.coverage import (
    MARKETS,
    SHARED,
    STOCK_CAVEAT,
    Owner,
    Profile,
    Requirement,
    Standing,
)

pytestmark = pytest.mark.usefixtures("temp_db")


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


def product(sku: str, model_color: str, markets: tuple[str, ...] = ()) -> None:
    with session_scope() as db:
        row = Product(
            sku=sku,
            base_code_format=sku,
            model_color=model_color,
            base_code=model_color.split(".")[0],
        )
        db.add(row)
        db.flush()
        for market in markets:
            db.add(
                CatalogueMembership(
                    product_id=row.id, market=market, catalog_pk=1
                )
            )


def value(
    attribute_id: int,
    entity_key: str,
    text: str | None,
    market: str = MARKET_AGNOSTIC,
    entity_kind: str = "model_color",
    state_lane: str = "current",
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
                state_lane=state_lane,
                entry_grain=entity_kind,
                source_kind="omnitron",
            )
        )


def ban(comment: str, bans: bool) -> None:
    with session_scope() as db:
        db.add(BanComment(comment=comment, bans=bans))


@pytest.fixture
def catalogue():
    """Three options, one of each interesting shape.

    A.1 complete and in every catalogue; B.2 missing its Polish name and its
    image; C.3 banned by merch and in no catalogue at all.
    """
    name = attribute("product_name", per_market=True)
    image = attribute("image_0", write_channel="read_only")
    sleeve = attribute(
        "sleeve_type",
        write_channel="product_card",
        applies_when_subcategory="Polo,Shirt",
    )
    category = attribute("sub_category", write_channel="product_card")
    comment = attribute("pl_comment")

    product("A.1.T1", "A.1", ("PL", "CZ"))
    product("A.1.T2", "A.1", ("PL", "CZ"))
    product("B.2.T1", "B.2", ("PL",))
    product("C.3.T1", "C.3")

    for mc, sub in (("A.1", "Polo"), ("B.2", "Polo"), ("C.3", "Wallet")):
        value(category, mc, sub)

    value(name, "A.1", "Koszulka", "PL")
    value(name, "A.1", "Polokosile", "CZ")
    value(image, "A.1", "https://example.test/a.jpg")
    value(sleeve, "A.1", "LONG SLEEVES")

    # B.2 was read for both — the rows exist and are empty, which is what makes
    # this `missing` rather than `unknown`.
    value(name, "B.2", None, "PL")
    value(name, "B.2", None, "CZ")
    value(image, "B.2", None)
    value(sleeve, "B.2", None)

    value(name, "C.3", "Portfel", "PL")
    value(image, "C.3", "https://example.test/c.jpg")
    value(comment, "C.3", "REMOVED")

    ban("REMOVED", True)
    ban("FR", False)

    with session_scope() as db:
        return load_catalogue(db)


PROFILE = Profile(
    "test",
    "Test profile",
    "Name in PL and CZ, an image, and a sleeve type.",
    (
        Requirement("product_name", ("PL", "CZ")),
        Requirement("image_0"),
        Requirement("sleeve_type"),
    ),
)


# ── loading ────────────────────────────────────────────────────────────────


def test_every_model_color_is_loaded_once_however_many_skus(catalogue):
    assert sorted(catalogue.rows) == ["A.1", "B.2", "C.3"]
    assert catalogue.rows["A.1"].sku_count == 2


def test_catalogue_membership_is_carried_per_model_color(catalogue):
    assert catalogue.rows["A.1"].markets == ("CZ", "PL")
    assert catalogue.rows["C.3"].markets == ()


def test_an_empty_row_still_counts_the_column_as_read(catalogue):
    """The distinction the whole module rests on. B.2's name rows exist and
    are empty, so PL and CZ are sourced even though nothing filled them."""
    assert set(catalogue.sourced["product_name"]) == {"PL", "CZ"}


def test_a_market_no_row_exists_for_is_not_sourced(catalogue):
    assert "SK" not in catalogue.sourced["product_name"]
    assert ("product_name", "SK") in catalogue.unread


def test_banned_comment_raises_the_removed_flag(catalogue):
    assert "removed" in catalogue.rows["C.3"].flags


def test_option_in_no_catalogue_is_flagged(catalogue):
    assert "not_in_catalogue" in catalogue.rows["C.3"].flags
    assert "not_in_catalogue" not in catalogue.rows["A.1"].flags


def test_missing_image_is_flagged_only_because_images_were_synced(catalogue):
    """A.1 and C.3 have images, so the column has been read and B.2's absence
    is real. With no image anywhere the flag must not fire at all."""
    assert "no_image" in catalogue.rows["B.2"].flags
    assert "no_image" not in catalogue.rows["A.1"].flags


def test_price_flag_stays_silent_when_no_price_was_ever_synced(catalogue):
    """The failure this codebase has hit four times: a sync that never
    completed rendering as a catalogue with no prices."""
    assert all("no_price" not in row.flags for row in catalogue.rows.values())


def test_stock_flag_stays_silent_when_no_stock_was_ever_synced(catalogue):
    assert all("no_stock" not in row.flags for row in catalogue.rows.values())


def test_an_unlisted_comment_asks_for_a_decision_rather_than_passing():
    """A comment on neither list is a third outcome. `rules/ban` is explicit
    that collapsing it to "not banned" is a silent misclassification, so
    coverage surfaces it as its own issue rather than counting the option
    clean."""
    comment = attribute("pl_comment")
    product("D.4.T1", "D.4", ("PL",))
    value(comment, "D.4", "Some new merch convention")
    ban("REMOVED", True)

    with session_scope() as db:
        loaded = load_catalogue(db)
    assert "unclassified_comment" in loaded.rows["D.4"].flags
    assert "removed" not in loaded.rows["D.4"].flags


# ── verdicts through the join ──────────────────────────────────────────────


def test_the_complete_option_is_complete(catalogue):
    report = build_report(catalogue, PROFILE)
    assert report.standings["A.1"] is Standing.COMPLETE


def test_the_empty_option_is_incomplete_with_every_gap_named(catalogue):
    report = build_report(catalogue, PROFILE)
    assert report.standings["B.2"] is Standing.INCOMPLETE
    assert report.gaps_by_product["B.2"] == 4  # PL name, CZ name, image, sleeve


def test_an_inapplicable_attribute_is_not_a_gap_for_a_wallet(catalogue):
    """C.3 is a wallet with no sleeve type. It is missing its CZ name — which
    was read and is empty for other products — and nothing else."""
    report = build_report(catalogue, PROFILE)
    assert report.gaps_by_product["C.3"] == 1


def test_requirement_tally_separates_missing_from_not_applicable(catalogue):
    report = build_report(catalogue, PROFILE)
    sleeve = next(t for t in report.requirements if t.attribute == "sleeve_type")
    assert (sleeve.met, sleeve.missing, sleeve.na) == (1, 1, 1)


def test_gaps_are_routed_to_whoever_may_write_the_attribute(catalogue):
    queue = gap_queue(catalogue, PROFILE, ["B.2"])
    assert [g[1] for g in queue[Owner.PARTNER]] == ["Sleeve Type"]
    assert {g[1] for g in queue[Owner.US]} == {"Product Name"}
    assert [g[1] for g in queue[Owner.SYSTEM]] == ["Image 0"]


# ── scoping ────────────────────────────────────────────────────────────────


def test_an_uploaded_list_narrows_the_population(catalogue):
    in_scope, _ = apply_scope(catalogue, Scope(model_colors=("A.1", "C.3")))
    assert in_scope == ["A.1", "C.3"]


def test_uploaded_codes_we_hold_nothing_for_are_reported_not_dropped(catalogue):
    _, scope = apply_scope(catalogue, Scope(model_colors=("A.1", "ZZ.9")))
    assert scope.not_found == ("ZZ.9",)


def test_uploaded_order_is_preserved(catalogue):
    in_scope, _ = apply_scope(catalogue, Scope(model_colors=("C.3", "A.1")))
    assert in_scope == ["C.3", "A.1"]


def test_narrowing_to_a_market_catalogue_excludes_options_not_in_it(catalogue):
    in_scope, _ = apply_scope(catalogue, Scope(in_catalogue="CZ"))
    assert in_scope == ["A.1"]


def test_sub_category_scope_filters(catalogue):
    in_scope, _ = apply_scope(catalogue, Scope(sub_category=("Wallet",)))
    assert in_scope == ["C.3"]


def test_scope_describes_itself_for_the_report_header(catalogue):
    scope = Scope(season=("26SS",), in_catalogue="PL")
    assert "26SS" in scope.describe()
    assert "PL catalogue" in scope.describe()
    assert Scope().describe() == "the whole catalogue"


def test_scope_description_says_how_many_uploaded_codes_missed(catalogue):
    """The header carries it as well as the sheet: a reader who never scrolls
    to 'Not found' still sees that the population is short."""
    _, scope = apply_scope(catalogue, Scope(model_colors=("A.1", "ZZ.9")))
    assert "2 uploaded codes" in scope.describe()
    assert "1 not found" in scope.describe()



# ── price and stock presence ───────────────────────────────────────────────
#
# Its own fixture rather than more values on `catalogue`: adding price and
# stock there would raise `no_price` and `no_stock` on every option the other
# tests assert flags for, and those assertions would start passing or failing
# for reasons that have nothing to do with what they are named after.


@pytest.fixture
def priced():
    """Three options that differ only in price and stock.

    P.1 priced in PL and CZ and in stock; P.2 priced in PL only and out of
    stock; P.3 priced nowhere and in stock.
    """
    price = attribute("price", per_market=True)
    stock = attribute("stock")

    for mc in ("P.1", "P.2", "P.3"):
        product(f"{mc}.T1", mc, ("PL", "CZ"))

    value(price, "P.1", "349.00", "PL")
    value(price, "P.1", "1899.00", "CZ")
    value(stock, "P.1", "5")

    value(price, "P.2", "249.00", "PL")
    # Read and empty, not unread — the distinction the whole module turns on.
    value(price, "P.2", None, "CZ")
    value(stock, "P.2", "0")

    value(price, "P.3", None, "PL")
    value(price, "P.3", None, "CZ")
    value(stock, "P.3", "12")

    with session_scope() as db:
        return load_catalogue(db)


def test_priced_anywhere_keeps_only_options_carrying_a_price(priced):
    in_scope, _ = apply_scope(priced, Scope(priced=PRESENCE_ANY))
    assert in_scope == ["P.1", "P.2"]


def test_priced_in_a_market_is_that_market_not_any(priced):
    """The point of the per-market form. P.2 has a Polish price and no Czech
    one, so a CZ price list must not be told it is done."""
    assert apply_scope(priced, Scope(priced="CZ"))[0] == ["P.1"]
    assert apply_scope(priced, Scope(priced="PL"))[0] == ["P.1", "P.2"]


def test_a_market_we_hold_no_price_for_returns_nothing_rather_than_everything(
    priced,
):
    """Failing open would be the dangerous direction: a price list for a market
    nobody has loaded would arrive claiming every option is covered."""
    assert apply_scope(priced, Scope(priced="HU"))[0] == []


def test_in_stock_ignores_markets_because_there_is_one_pool(priced):
    in_scope, _ = apply_scope(priced, Scope(stocked=PRESENCE_ANY))
    assert in_scope == ["P.1", "P.3"]


def test_zero_stock_is_out_of_stock_not_missing_stock(priced):
    """P.2 carries a stock row reading 0. It is read, and it is out of stock —
    the threshold is `>`, matching `rules.liveness.STOCK_THRESHOLD`."""
    assert "P.2" not in apply_scope(priced, Scope(stocked=PRESENCE_ANY))[0]


def test_price_and_stock_are_anded_like_every_other_facet(priced):
    scope = Scope(priced=PRESENCE_ANY, stocked=PRESENCE_ANY)
    assert apply_scope(priced, scope)[0] == ["P.1"]


def test_issue_flags_narrow_the_population_server_side(priced):
    """The filter the workbook used to drop. P.3 is the only option with no
    price in any market, so it is the only one flagged."""
    in_scope, _ = apply_scope(priced, Scope(flags=("no_price",)))
    assert in_scope == ["P.3"]


def test_several_flags_are_ored_within_the_field(priced):
    in_scope, _ = apply_scope(priced, Scope(flags=("no_price", "no_stock")))
    assert in_scope == ["P.2", "P.3"]


def test_presence_filters_describe_themselves_for_the_report_header(priced):
    del priced
    assert "priced in CZ" in Scope(priced="CZ").describe()
    assert "priced somewhere" in Scope(priced=PRESENCE_ANY).describe()
    assert "in stock" in Scope(stocked=PRESENCE_ANY).describe()
    assert "No price" in Scope(flags=("no_price",)).describe()


def test_no_presence_filter_keeps_everything(priced):
    assert apply_scope(priced, Scope())[0] == ["P.1", "P.2", "P.3"]


# ── the Values sheet at size grain ─────────────────────────────────────────
#
# The point of the grain switch: price and stock are stored per SKU, so at
# ModelColor grain they are rolled up and the sizes that disagree are counted
# rather than shown. These pin that the disagreement becomes visible.


def sized(sku: str, mcs: str, mc: str, size: str) -> None:
    with session_scope() as db:
        db.add(
            Product(
                sku=sku,
                base_code_format=mcs,
                model_color=mc,
                base_code=mc.split(".")[0],
                size=size,
            )
        )


@pytest.fixture
def sizes():
    """One option, two sizes, and a price that differs between them."""
    price = attribute("price", per_market=True, value_grain="sku")
    name = attribute("product_name", per_market=True)

    sized("SKU-1", "S.1.T1", "S.1", "T1")
    sized("SKU-2", "S.1.T2", "S.1", "T2")

    value(price, "SKU-1", "349.00", "PL", entity_kind="sku")
    value(price, "SKU-2", "399.00", "PL", entity_kind="sku")
    value(name, "S.1", "Koszulka polo", "PL")

    with session_scope() as db:
        return load_catalogue(db)


def test_at_option_grain_the_sizes_disagree_and_are_only_counted(sizes):
    """The behaviour the size sheet exists to fix, pinned so the fix is
    visibly a fix."""
    del sizes
    block = _values([ValueSelection("price", ("PL",))], ["S.1"])
    assert block.disagreements == 1
    assert len(block.row("S.1")) == 1


def test_at_size_grain_each_size_carries_its_own_price(sizes):
    del sizes
    block = _values([ValueSelection("price", ("PL",))], ["S.1"], by_size=True)
    assert block.by_size
    assert [r.base_code_format for r in block.size_rows] == ["S.1.T1", "S.1.T2"]
    assert block.row("SKU-1") == ["349.00"]
    assert block.row("SKU-2") == ["399.00"]


def test_nothing_is_rolled_up_or_disagrees_at_size_grain(sizes):
    del sizes
    block = _values([ValueSelection("price", ("PL",))], ["S.1"], by_size=True)
    assert block.rolled_up == ()
    assert block.disagreements == 0


def test_an_option_grain_value_repeats_down_the_sizes(sizes):
    """One name applies to every size of the option, so every size row shows
    it. A blank on half the rows would read as a gap that is not there."""
    del sizes
    block = _values([ValueSelection("product_name", ("PL",))], ["S.1"], by_size=True)
    assert block.row("SKU-1") == ["Koszulka polo"]
    assert block.row("SKU-2") == ["Koszulka polo"]


def test_size_rows_carry_the_sku_as_well_as_the_mcs(sizes):
    """Two different codes. The MCS is what a person matches on, the SKU is
    what Omnitron is keyed by, and the sheet has to carry both."""
    del sizes
    block = _values([], ["S.1"], by_size=True)
    assert [(r.sku, r.base_code_format, r.size) for r in block.size_rows] == [
        ("SKU-1", "S.1.T1", "T1"),
        ("SKU-2", "S.1.T2", "T2"),
    ]


def test_ticking_only_the_size_key_still_produces_a_sheet(sizes):
    """No attributes, just the key columns — a list of every SKU in scope. The
    old early return dropped the sheet entirely."""
    del sizes
    block = _values([], ["S.1"], by_size=True)
    assert block.columns == []
    assert len(block.size_rows) == 2


def test_size_grain_never_widens_past_the_options_it_was_given(sizes):
    """The invariant the whole live-read exception rests on, at the new grain:
    a size can only be a size of an option the report already resolved."""
    del sizes
    assert _values([], ["NOPE.9"], by_size=True).size_rows == ()


def test_the_cell_cap_counts_sizes_not_options(sizes):
    """Two SKUs and one column is two cells. A cap of one must refuse, or the
    server would accept a request six times larger than it can build."""
    del sizes
    with pytest.raises(TooManyValues) as caught:
        _values(
            [ValueSelection("price", ("PL",))], ["S.1"], by_size=True, max_cells=1
        )
    assert caught.value.options == 2


def test_size_grain_gets_its_own_cap_without_being_asked(sizes):
    """A per-SKU sheet is an import file, and Omnitron imports by SKU, so the
    whole catalogue at a few columns is the normal request rather than an
    extreme one. Under the option-grain cap that request was refused at two
    columns, which made the sheet useless for its only purpose."""
    del sizes
    from pim_studio.reports.values import MAX_SIZE_CELLS, MAX_VALUE_CELLS

    assert MAX_SIZE_CELLS > MAX_VALUE_CELLS
    # 47,135 SKUs against four value columns — today's catalogue, and the shape
    # of an import file. It has to fit.
    assert MAX_SIZE_CELLS >= 47_135 * 4


def test_the_size_sheet_is_named_for_what_it_is(sizes):
    """`Values` is where a per-option lookup lives. A per-SKU import file under
    the same tab name is findable only by someone who already knows."""
    del sizes
    from pim_studio.reports.workbook import _values_title

    assert _values_title(_values([], ["S.1"], by_size=True)) == "SKU Detail"
    assert _values_title(_values([ValueSelection("price", ("PL",))], ["S.1"])) == (
        "Values"
    )

# ── report assembly ────────────────────────────────────────────────────────


def test_buckets_sum_to_the_population(catalogue):
    report = build_report(catalogue, PROFILE)
    assert sum(report.buckets.values()) == report.population == 3


def test_the_unread_caveat_is_raised_when_a_column_was_never_read(catalogue):
    report = build_report(catalogue, PROFILE)
    assert any("never been read" in c for c in report.caveats)


def test_no_stock_caveat_when_no_stock_flag_fired(catalogue):
    """The caveat is about a number being shown. With nothing stock-derived on
    the report, printing it would be noise."""
    report = build_report(catalogue, PROFILE)
    assert STOCK_CAVEAT not in report.caveats


def test_stock_caveat_appears_once_stock_has_been_synced():
    stock = attribute("stock", write_channel="read_only")
    product("E.5.T1", "E.5", ("PL",))
    product("F.6.T1", "F.6", ("PL",))
    value(stock, "E.5.T1", "12", "PL", entity_kind="sku")
    value(stock, "F.6.T1", "0", "PL", entity_kind="sku")
    with session_scope() as db:
        loaded = load_catalogue(db)
    report = build_report(loaded, PROFILE)
    assert "no_stock" in loaded.rows["F.6"].flags
    assert STOCK_CAVEAT in report.caveats


# ── the workbook ───────────────────────────────────────────────────────────


def test_workbook_has_the_expected_sheets(catalogue):
    book = load_workbook(BytesIO(build_workbook(build_report(catalogue, PROFILE))))
    assert book.sheetnames == ["Summary", "Requirements", "ModelColor Detail"]


def test_not_found_sheet_appears_only_when_something_was_not_found(catalogue):
    report = build_report(catalogue, PROFILE, Scope(model_colors=("A.1", "ZZ.9")))
    book = load_workbook(BytesIO(build_workbook(report)))
    assert "Not found" in book.sheetnames
    assert book["Not found"]["A5"].value == "ZZ.9"


def test_detail_sheet_has_one_row_per_option(catalogue):
    report = build_report(catalogue, PROFILE)
    book = load_workbook(BytesIO(build_workbook(report)))
    sheet = book["ModelColor Detail"]
    codes = {sheet.cell(row=r, column=1).value for r in range(5, 5 + report.population)}
    assert codes == {"A.1", "B.2", "C.3"}


def test_detail_sheet_puts_the_incomplete_option_first(catalogue):
    """The sheet is opened to find work, so the work is at the top."""
    report = build_report(catalogue, PROFILE)
    sheet = load_workbook(BytesIO(build_workbook(report)))["ModelColor Detail"]
    assert sheet["A5"].value == "B.2"


def test_requirements_sheet_carries_the_unknown_column(catalogue):
    sheet = load_workbook(
        BytesIO(build_workbook(build_report(catalogue, PROFILE)))
    )["Requirements"]
    headers = [sheet.cell(row=4, column=c).value for c in range(1, 10)]
    assert "Unknown" in headers
    assert "Fill rate" in headers


def test_methodology_carries_the_caveats_into_the_file(catalogue):
    """A caveat that lives only in the UI does not survive being emailed."""
    report = build_report(catalogue, PROFILE)
    sheet = load_workbook(BytesIO(build_workbook(report)))["Summary"]
    text = " ".join(
        str(c.value) for row in sheet.iter_rows() for c in row if c.value is not None
    )
    assert "never been read" in text
    assert "UNKNOWN is never counted as a gap" in text


def test_workbook_recalculates_on_open(catalogue):
    """Percentages are formulas; without this they open blank and read as a
    broken file."""
    book = load_workbook(BytesIO(build_workbook(build_report(catalogue, PROFILE))))
    assert book.calculation.fullCalcOnLoad is True


def test_filename_names_the_profile_and_the_date(catalogue):
    report = build_report(catalogue, PROFILE)
    name = filename_for(report)
    assert name.startswith("Northwind_coverage_test_")
    assert name.endswith(".xlsx")


# ── conditional attributes that never matched ──────────────────────────────


def _mismatched_catalogue():
    """A catalogue whose sub-categories are breadcrumbs, not registry terms.

    This is the shape the mirror actually holds: `sub_category` comes from
    `active_categories__category__name`, a category path, while the registry's
    applies-when list holds bare terms. Nothing errors — every conditional
    attribute simply reads `na`.
    """
    sleeve = attribute(
        "sleeve_type",
        write_channel="product_card",
        applies_when_subcategory="Polo,Shirt",
    )
    category = attribute("sub_category", write_channel="product_card")
    for mc in ("A.1", "B.2"):
        product(f"{mc}.T1", mc, ("PL",))
        value(category, mc, "Textile > Polo Shirts > Men")
        value(sleeve, mc, None)
    with session_scope() as db:
        return load_catalogue(db)


CONDITIONAL = Profile("c", "Conditional", "", (Requirement("sleeve_type"),))


def test_a_name_mismatch_erases_the_requirement_rather_than_inflating_it():
    """The direction matters. An inflated queue gets complained about; an
    erased one does not, so this is the failure worth detecting."""
    report = build_report(_mismatched_catalogue(), CONDITIONAL)
    (row,) = report.requirements
    assert (row.met, row.missing, row.unknown, row.na) == (0, 0, 0, 2)


def test_a_requirement_that_matched_nothing_is_called_out():
    report = build_report(_mismatched_catalogue(), CONDITIONAL)
    assert any("applied to no option in scope" in c for c in report.caveats)
    assert any("Sleeve Type" in c for c in report.caveats)


def test_no_such_caveat_when_the_condition_does_match(catalogue):
    """The fixture's sub-categories are registry terms, so `sleeve_type`
    matches the polos and is `na` only on the wallet. Nothing to warn about."""
    report = build_report(catalogue, Profile("c", "c", "", (Requirement("sleeve_type"),)))
    assert not any("applied to no option" in c for c in report.caveats)


def test_no_such_caveat_on_an_empty_scope(catalogue):
    """Everything is `na` when there is nothing to judge. Warning there would
    fire on every narrow filter and stop being read."""
    report = build_report(catalogue, CONDITIONAL, Scope(model_colors=("ZZ.9",)))
    assert not any("applied to no option" in c for c in report.caveats)


def test_the_caveat_reaches_the_workbook():
    report = build_report(_mismatched_catalogue(), CONDITIONAL)
    sheet = load_workbook(BytesIO(build_workbook(report)))["Summary"]
    text = " ".join(
        str(c.value) for row in sheet.iter_rows() for c in row if c.value is not None
    )
    assert "applied to no option in scope" in text


# ── values, read for the Values sheet ──────────────────────────────────────
#
# The loader is the one thing in `reports/` that reads value text, so what is
# tested here is mostly what it refuses to do: widen past the options it was
# given, take a proposal over a current value, or let one bad character take
# the whole download down with it.


def _values(selections, model_colors, **caps):
    with session_scope() as db:
        return load_values(db, selections, model_colors, **caps)


def test_a_market_agnostic_attribute_gets_one_column_not_four(catalogue):
    """The picker draws one box against four. The sheet has to agree, even
    when the request asks for markets it has no business having."""
    del catalogue
    block = _values([ValueSelection("image_0", ("PL", "CZ"))], ["A.1"])
    assert [c.market for c in block.columns] == [SHARED]


def test_only_the_ticked_markets_become_columns(catalogue):
    del catalogue
    block = _values([ValueSelection("product_name", ("PL",))], ["A.1"])
    assert [(c.attribute, c.market) for c in block.columns] == [("product_name", "PL")]
    assert block.row("A.1") == ["Koszulka"]


def test_no_markets_on_a_per_market_attribute_means_all_four(catalogue):
    del catalogue
    block = _values([ValueSelection("product_name")], ["A.1"])
    assert [c.market for c in block.columns] == list(MARKETS)
    # PL and CZ hold values, SK and HU have never been written.
    assert block.row("A.1") == ["Koszulka", "Polokosile", "", ""]


def test_columns_keep_selection_order_then_market_order(catalogue):
    del catalogue
    block = _values(
        [ValueSelection("image_0"), ValueSelection("product_name", ("CZ", "PL"))],
        ["A.1"],
    )
    assert [(c.attribute, c.market) for c in block.columns] == [
        ("image_0", SHARED),
        ("product_name", "PL"),
        ("product_name", "CZ"),
    ]


def test_an_option_out_of_scope_never_reaches_the_block(catalogue):
    """The guarantee the whole design rests on: the loader fills cells for the
    options it was handed and cannot invent one."""
    del catalogue
    block = _values([ValueSelection("product_name", ("PL",))], ["A.1"])
    assert set(block.cells) == {"A.1"}
    assert block.row("B.2") == [""]


def test_an_option_with_no_values_is_blank_not_missing(catalogue):
    del catalogue
    block = _values([ValueSelection("product_name", ("PL",))], ["A.1", "B.2"])
    # B.2's PL name row exists and is empty — the distinction coverage cares
    # about, but on this sheet both read as a blank cell.
    assert block.row("B.2") == [""]
    assert block.filled(0) == 1


def test_a_sku_grain_value_rolls_up_to_its_model_color():
    price = attribute("price", value_grain="sku", per_market=True)
    product("A.1.T1", "A.1")
    product("A.1.T2", "A.1")
    value(price, "A.1.T1", "199", "PL", entity_kind="sku")

    block = _values([ValueSelection("price", ("PL",))], ["A.1"])
    assert block.row("A.1") == ["199"]
    assert block.rolled_up == (0,)


def test_disagreeing_sizes_are_counted_not_hidden():
    """Two sizes of one option holding different values is the known defect,
    not a tie to break quietly. First by SKU wins and the count says so."""
    price = attribute("price", value_grain="sku", per_market=True)
    product("A.1.T1", "A.1")
    product("A.1.T2", "A.1")
    value(price, "A.1.T2", "249", "PL", entity_kind="sku")
    value(price, "A.1.T1", "199", "PL", entity_kind="sku")

    block = _values([ValueSelection("price", ("PL",))], ["A.1"])
    assert block.row("A.1") == ["199"]
    assert block.disagreements == 1


def test_sizes_that_agree_are_not_a_disagreement():
    price = attribute("price", value_grain="sku", per_market=True)
    product("A.1.T1", "A.1")
    product("A.1.T2", "A.1")
    value(price, "A.1.T1", "199", "PL", entity_kind="sku")
    value(price, "A.1.T2", "199", "PL", entity_kind="sku")

    assert _values([ValueSelection("price", ("PL",))], ["A.1"]).disagreements == 0


def test_a_proposal_never_beats_the_current_value():
    name = attribute("product_name", per_market=True)
    product("A.1.T1", "A.1")
    value(name, "A.1", "Koszulka", "PL")
    value(name, "A.1", "Proposed", "PL", state_lane="proposal")

    assert _values([ValueSelection("product_name", ("PL",))], ["A.1"]).row("A.1") == [
        "Koszulka"
    ]


def test_a_base_code_grain_row_is_invisible_here_as_it_is_everywhere():
    """`load_catalogue` skips base-code rows and so does this. One shared blind
    spot is right; a second mapping here would put a value on the Values sheet
    for an option the Summary sheet counts as having none."""
    detail = attribute("product_detail", value_grain="base_code", per_market=True)
    product("A.1.T1", "A.1")
    value(detail, "A", "From the base code", "PL", entity_kind="base_code")

    assert _values([ValueSelection("product_detail", ("PL",))], ["A.1"]).row("A.1") == [
        ""
    ]


def test_an_unknown_attribute_is_reported_not_raised(catalogue):
    del catalogue
    block = _values(
        [ValueSelection("product_name", ("PL",)), ValueSelection("no_such_thing")],
        ["A.1"],
    )
    assert block.unknown_keys == ("no_such_thing",)
    assert len(block.columns) == 1


def test_html_is_carried_through_untouched():
    """Raw HTML is what was asked for. The one requirement most likely to be
    tidied away by a well-meaning later change."""
    markup = '<p>Koszulka <b>polo</b> &amp; pi&quot;k&eacute;</p>'
    description = attribute("description", data_type="html", long_text=True, per_market=True)
    product("A.1.T1", "A.1")
    value(description, "A.1", markup, "PL")

    assert _values([ValueSelection("description", ("PL",))], ["A.1"]).row("A.1") == [
        markup
    ]


def test_a_control_character_does_not_take_the_download_with_it():
    """openpyxl raises `IllegalCharacterError` on save for these, so one bad
    character in one description would 500 the whole request."""
    name = attribute("product_name", per_market=True)
    product("A.1.T1", "A.1")
    value(name, "A.1", "bad\x0bchar", "PL")

    block = _values([ValueSelection("product_name", ("PL",))], ["A.1"])
    assert block.row("A.1") == ["badchar"]
    assert block.scrubbed == 1
    # And it survives the thing it would otherwise have broken.
    with session_scope() as db:
        build_workbook(build_report(load_catalogue(db), PROFILE), block)


def test_too_many_cells_is_refused_before_a_row_is_read(catalogue):
    del catalogue
    with pytest.raises(TooManyValues) as caught:
        _values([ValueSelection("product_name")], ["A.1", "B.2"], max_cells=4)
    assert "The limit is 4 values" in str(caught.value)


def test_too_many_characters_is_refused_after_the_read(catalogue):
    del catalogue
    with pytest.raises(TooManyValues) as caught:
        _values([ValueSelection("product_name", ("PL",))], ["A.1"], max_chars=2)
    assert "characters" in str(caught.value)


def test_too_many_columns_is_refused(catalogue):
    del catalogue
    with pytest.raises(TooManyValues) as caught:
        _values([ValueSelection("product_name")], ["A.1"], max_columns=3)
    assert "The limit is 3 columns" in str(caught.value)


# ── the Values sheet ───────────────────────────────────────────────────────
#
# `_title` leaves the header on row 4 and the first option on row 5. Named here
# because a loaded workbook reports `freeze_panes` as a string like "C5", which
# is no use for arithmetic.

VALUES_HEADER_ROW = 4
VALUES_FIRST_ROW = 5


def _with_values(catalogue, selections, profile=None):
    report = build_report(catalogue, profile or PROFILE)
    block = _values(selections, [row.model_color for row in report.rows])
    return load_workbook(BytesIO(build_workbook(report, block))), report, block


def test_no_values_sheet_when_nothing_was_asked_for(catalogue):
    """The regression guard for every caller that predates this. An unchanged
    request has to produce an unchanged file."""
    book = load_workbook(BytesIO(build_workbook(build_report(catalogue, PROFILE))))
    assert "Values" not in book.sheetnames
    assert book.sheetnames == ["Summary", "Requirements", "ModelColor Detail"]


def test_the_values_sheet_lists_exactly_the_options_the_detail_sheet_does(catalogue):
    """The mechanical proof of the claim the whole design rests on. The
    database fills cells; it never decides which options the file is about."""
    book, _, _ = _with_values(catalogue, [ValueSelection("product_name", ("PL",))])

    def codes(name: str) -> set[str]:
        sheet = book[name]
        return {
            row[0].value
            for row in sheet.iter_rows(min_row=VALUES_FIRST_ROW)
            if row[0].value
        }

    assert codes("Values") == codes("ModelColor Detail")


def test_the_header_the_widths_and_the_autofilter_all_agree(catalogue):
    """`_detail_sheet` states its column count in four places and they are kept
    in step by hand. This pins that the Values sheet derives all of them from
    one number instead."""
    book, _, block = _with_values(
        catalogue,
        [ValueSelection("product_name", ("PL", "CZ")), ValueSelection("image_0")],
    )
    sheet = book["Values"]
    expected = 2 + len(block.columns)

    assert sheet.max_column == expected
    assert sheet.auto_filter.ref.split(":")[1].rstrip("0123456789") == (
        get_column_letter(expected)
    )
    for index in range(1, expected + 1):
        assert sheet.column_dimensions[get_column_letter(index)].width


def test_a_per_market_column_names_its_market_and_a_shared_one_does_not(catalogue):
    book, _, _ = _with_values(
        catalogue,
        [ValueSelection("product_name", ("PL",)), ValueSelection("image_0")],
    )
    sheet = book["Values"]
    headers = [cell.value for cell in sheet[VALUES_HEADER_ROW]]
    assert headers[:2] == ["ModelColor", "Name"]
    assert headers[2] == "Product Name · PL"
    assert headers[3] == "Image 0"


def test_the_values_sheet_says_when_it_was_read_and_when_the_counts_were_built(
    catalogue,
):
    book, report, block = _with_values(
        catalogue, [ValueSelection("product_name", ("PL",))]
    )
    subtitle = book["Values"].cell(row=2, column=1).value
    assert block.read_at.strftime("%d %b %Y") in subtitle
    assert report.generated_at.strftime("%d %b %Y") in subtitle
    assert "still counted as missing on the Summary" in subtitle


def test_the_staleness_note_reaches_the_summary_too(catalogue):
    """Whoever is emailed this reads the Summary and may never open the fifth
    tab. A caveat only one reader sees is not a caveat."""
    book, _, _ = _with_values(catalogue, [ValueSelection("product_name", ("PL",))])
    sheet = book["Summary"]
    text = " ".join(
        str(c.value) for row in sheet.iter_rows() for c in row if c.value is not None
    )
    assert "read live from the mirror" in text


def test_a_value_that_looks_like_a_formula_stays_text():
    """openpyxl stores a leading `=` as a formula and it survives the save, so
    Excel opens the cell as #NAME? and the copy is gone."""
    name = attribute("product_name", per_market=True)
    product("A.1.T1", "A.1", ("PL",))
    value(name, "A.1", "=1+1", "PL")

    with session_scope() as db:
        report = build_report(load_catalogue(db), PROFILE)
    block = _values([ValueSelection("product_name", ("PL",))], ["A.1"])
    sheet = load_workbook(BytesIO(build_workbook(report, block)))["Values"]
    cell = sheet.cell(row=VALUES_FIRST_ROW, column=3)

    assert cell.value == "=1+1"
    assert cell.data_type == "s"


def test_html_survives_the_round_trip_into_the_file():
    markup = '<p>Koszulka <b>polo</b></p><li>Fine piqu&eacute;</li>'
    description = attribute(
        "description", data_type="html", long_text=True, per_market=True
    )
    product("A.1.T1", "A.1", ("PL",))
    value(description, "A.1", markup, "PL")

    with session_scope() as db:
        report = build_report(load_catalogue(db), PROFILE)
    block = _values([ValueSelection("description", ("PL",))], ["A.1"])
    sheet = load_workbook(BytesIO(build_workbook(report, block)))["Values"]

    assert sheet.cell(row=VALUES_FIRST_ROW, column=3).value == markup


def test_a_column_nobody_filled_is_marked_rather_than_left_looking_broken(catalogue):
    """An empty column reads exactly like one that failed to load."""
    book, _, block = _with_values(catalogue, [ValueSelection("product_name", ("SK",))])
    sheet = book["Values"]
    assert block.filled(0) == 0
    assert sheet.cell(row=VALUES_HEADER_ROW, column=3).fill.fgColor.rgb.endswith(
        "EDEDED"
    )
