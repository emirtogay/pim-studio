"""Wrong-language values, and where a replacement may come from.

The property that matters most here is the one that costs nothing to get wrong
and is invisible afterwards: a sibling colour's text is only safe to reuse when
it does not describe the colour. `product_detail` routinely does — the live
catalogue carries `...classic fit brązowa` and `...classic fit khaki` on one
base code — so a source that copies any sibling would confidently describe a
brown polo as khaki, in fluent Polish, for ever.

The guard is that two distinct colours must carry the text identically. Most of
these tests exist to hold that line.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeColumn,
    AttributeValue,
    OmnitronAttribute,
)
from pim_studio.sync.proposals import (
    PROPOSAL_LANE,
    Candidate,
    GroupingRowSource,
    MachineSource,
    SiblingSource,
    find_untranslated,
    generate,
    reads_foreign,
    usable_as_translation,
)

pytestmark = pytest.mark.usefixtures("temp_db")

# The detector needs 0.90 to speak, and product-speak rarely reaches it — the
# obvious fixture, "Men's long sleeve classic fit cotton polo shirt", scores
# 0.861 and is correctly ignored. That strictness is why only 45 of 15,366
# names are flagged. These read as prose and clear the bar comfortably.
ENGLISH = "This shirt is made from soft cotton and it is designed for everyday comfort"
TURKISH = "Erkek Regular Fit Uzun Kollu Bordo Pamuklu Polo Yaka Tişört"
POLISH_A = "Męska koszulka polo z długim rękawem z bawełny w kolorze bordowym"
POLISH_B = "Damska bluza dresowa z kapturem i suwakiem z bawełny organicznej"


def registry() -> dict[str, int]:
    with session_scope() as db:
        name = Attribute(
            key="product_name", label="Product name", write_channel="omnitron_import",
            value_grain="model_color", per_market=True,
        )
        sleeve = Attribute(
            key="sleeve_type", label="Sleeve", write_channel="read_only",
            value_grain="model_color", per_market=True,
        )
        # Real options declare what garment they are, and the sibling rule now
        # asks. Fixtures without these would make every sibling test pass for
        # the wrong reason — "no shared attributes" rather than the thing the
        # test names.
        fit = Attribute(
            key="fit", label="Fit", write_channel="read_only",
            value_grain="model_color", per_market=False,
        )
        pattern = Attribute(
            key="pattern", label="Pattern", write_channel="read_only",
            value_grain="model_color", per_market=False,
        )
        db.add_all([name, sleeve, fit, pattern])
        db.flush()
        GARMENT_IDS.clear()
        GARMENT_IDS.update({"fit": fit.id, "pattern": pattern.id})
        db.add_all([
            AttributeColumn(attribute_id=name.id, source="omnitron",
                            column_name="attributes__urun_adi_pl", market="PL",
                            api_writable=True),
            # Deliberately not writable: proposals must not be offered for it.
            AttributeColumn(attribute_id=sleeve.id, source="omnitron",
                            column_name="attributes__integration_kol_boyu", market="PL",
                            api_writable=False),
            OmnitronAttribute(pk=1, key="urun_adi_pl", name="x", data_type="text",
                              is_localizable=True),
            OmnitronAttribute(pk=2, key="integration_kol_boyu", name="x",
                              data_type="dropdown", is_localizable=False),
        ])
        return {"name": name.id, "sleeve": sleeve.id,
                "fit": fit.id, "pattern": pattern.id}


#: Filled by `registry()`; read by `value()` so every fixture option looks
#: like a real one without each test having to say so.
GARMENT_IDS: dict[str, int] = {}

#: One garment, unless a test says otherwise. Colours of a style share this.
DEFAULT_GARMENT = {"fit": "Regular Fit", "pattern": "Düz"}


def garment(model_color: str, **attrs: str) -> None:
    """Say what garment an option is. Idempotent per (option, attribute)."""
    with session_scope() as db:
        for key, text in attrs.items():
            attribute_id = GARMENT_IDS[key]
            exists = db.scalar(
                select(AttributeValue).filter_by(
                    entity_kind="model_color", entity_key=model_color,
                    # Stored as "*", not NULL: the model defaults a
                    # market-agnostic value to the agnostic marker, so looking
                    # for None here finds nothing and inserts a duplicate.
                    attribute_id=attribute_id, market="*", state_lane="current",
                )
            )
            if exists is not None:
                exists.value = text
                continue
            db.add(AttributeValue(
                entity_kind="model_color", entity_key=model_color,
                attribute_id=attribute_id, market="*", state="live",
                state_lane="current", entry_grain="model_color",
                source_kind="omnitron_api", value=text,
            ))


def value(attr_id: int, model_color: str, text: str, market: str = "PL",
          lane: str = "current", state: str = "live") -> None:
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="model_color", entity_key=model_color, attribute_id=attr_id,
            market=market, state=state, state_lane=lane, entry_grain="model_color",
            source_kind="omnitron_api", value=text,
        ))
    # Every option in a fixture is the same garment by default, so the sibling
    # tests keep testing what their names say. `garment()` overrides it.
    if GARMENT_IDS:
        garment(model_color, **DEFAULT_GARMENT)


def proposals_for(attr_id: int, model_color: str, market: str = "PL") -> list[AttributeValue]:
    with session_scope() as db:
        return list(db.scalars(
            select(AttributeValue).where(
                AttributeValue.entity_key == model_color,
                AttributeValue.attribute_id == attr_id,
                AttributeValue.market == market,
                AttributeValue.state_lane == PROPOSAL_LANE,
            )
        ))


def run(sources=None, **kw) -> dict:
    with session_scope() as db:
        return generate(db, sources if sources is not None else [SiblingSource(db)], **kw)


# ── the detector, over markup ──────────────────────────────────────────────


def test_markup_does_not_make_a_description_read_as_english() -> None:
    """Tag names are English in every market, so a Polish description wrapped
    in `<li>` must not be read as an English one."""
    polish = f"<ul><li>{POLISH_A}</li><li>{POLISH_B}</li></ul>"
    assert reads_foreign(polish) is None
    assert reads_foreign(f"<ul><li>{ENGLISH}</li></ul>") == "ENGLISH"


def test_both_wrong_languages_are_named() -> None:
    assert reads_foreign(ENGLISH) == "ENGLISH"
    assert reads_foreign(TURKISH) == "TURKISH"
    assert reads_foreign(POLISH_A) is None


# ── finding what needs a translation ───────────────────────────────────────


def test_only_writable_fields_are_offered_a_proposal() -> None:
    """A suggestion for a field this panel cannot push is one nobody can act
    on. The garment dropdowns are tenant-wide and stay out."""
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["sleeve"], "L3539.001", ENGLISH)

    with session_scope() as db:
        found = find_untranslated(db)

    assert [c.attribute_id for c in found] == [ids["name"]]


def test_a_value_already_carrying_a_proposal_is_not_asked_about_again() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["name"], "L3539.001", POLISH_A, lane=PROPOSAL_LANE, state="proposed")

    with session_scope() as db:
        assert find_untranslated(db) == []


# ── the colour-safety guard ────────────────────────────────────────────────


def test_two_colours_agreeing_is_enough_to_propose() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)      # the one to fix
    value(ids["name"], "L3539.002", POLISH_A)
    value(ids["name"], "L3539.003", POLISH_A)

    stats = run()

    assert stats["proposed"] == 1
    (row,) = proposals_for(ids["name"], "L3539.001")
    assert row.value == POLISH_A
    assert row.state == "proposed"
    assert row.suspect_flag == "EN?"
    assert "sibling" in (row.source_ref or "")


def test_one_sibling_alone_proves_nothing_and_proposes_nothing() -> None:
    """The live catalogue names the colour inside a detail line. A single
    sibling cannot show whether this text does, so it is not offered."""
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["name"], "L3539.002", POLISH_A)

    assert run()["proposed"] == 0
    assert proposals_for(ids["name"], "L3539.001") == []


def test_siblings_that_disagree_propose_nothing() -> None:
    """Two rival texts, each carried by two colours, is the catalogue failing
    to say one thing — not an invitation to pick."""
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["name"], "L3539.002", POLISH_A)
    value(ids["name"], "L3539.003", POLISH_A)
    value(ids["name"], "L3539.004", POLISH_B)
    value(ids["name"], "L3539.005", POLISH_B)

    assert run()["proposed"] == 0


def test_a_colour_specific_sibling_pair_is_exactly_what_the_rule_excludes() -> None:
    """Two colours, two different texts, because each names its own colour.
    This is the brown-polo-described-as-khaki case from the live catalogue."""
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["name"], "L3539.002", "Koszulka polo N.10.10 męska classic fit brązowa")
    value(ids["name"], "L3539.003", "Koszulka polo N.10.10 męska classic fit khaki")

    assert run()["proposed"] == 0


def test_a_foreign_sibling_never_becomes_the_proposal() -> None:
    """Two colours agreeing on English text is two colours that are both
    wrong, not a translation."""
    ids = registry()
    value(ids["name"], "L3539.001", TURKISH)
    value(ids["name"], "L3539.002", ENGLISH)
    value(ids["name"], "L3539.003", ENGLISH)

    assert run()["proposed"] == 0


def test_a_sibling_of_another_base_code_is_not_a_sibling() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["name"], "L1885.002", POLISH_A)
    value(ids["name"], "L1885.003", POLISH_A)

    assert run()["proposed"] == 0


def test_a_sibling_in_another_market_is_not_a_sibling() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH, market="PL")
    value(ids["name"], "L3539.002", POLISH_A, market="CZ")
    value(ids["name"], "L3539.003", POLISH_A, market="CZ")

    assert run()["proposed"] == 0


# ── what a proposal is, and is not ─────────────────────────────────────────


def test_a_proposal_never_touches_the_live_value() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["name"], "L3539.002", POLISH_A)
    value(ids["name"], "L3539.003", POLISH_A)

    run()

    with session_scope() as db:
        live = db.scalar(select(AttributeValue).where(
            AttributeValue.entity_key == "L3539.001",
            AttributeValue.state_lane == "current",
        ))
    assert live is not None
    assert live.value == ENGLISH
    assert live.state == "live"


def test_a_source_handing_back_the_current_text_is_not_a_proposal() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)

    class Echo:
        name = "echo"

        def propose(self, candidates: list[Candidate]) -> dict:
            return {c.cell: c.current for c in candidates}

    assert run([Echo()])["proposed"] == 0


def test_the_first_source_to_answer_a_cell_wins() -> None:
    """The free source is asked first so a paid one is only asked what it
    could not settle."""
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)

    class Cheap:
        name = "cheap"

        def propose(self, candidates: list[Candidate]) -> dict:
            return {c.cell: POLISH_A for c in candidates}

    class Expensive:
        name = "expensive"
        asked: list[int] = []

        def propose(self, candidates: list[Candidate]) -> dict:
            Expensive.asked.append(len(candidates))
            return {c.cell: POLISH_B for c in candidates}

    stats = run([Cheap(), Expensive()])

    (row,) = proposals_for(ids["name"], "L3539.001")
    assert row.value == POLISH_A
    assert Expensive.asked == []          # nothing left to ask about
    assert stats["by_source"]["cheap"] == 1


def test_a_machine_source_with_no_key_proposes_nothing_rather_than_failing() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)

    stats = run([MachineSource(translator=None)])

    assert stats["candidates"] == 1
    assert stats["proposed"] == 0


def test_a_turkish_value_is_flagged_as_turkish() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", TURKISH)
    value(ids["name"], "L3539.002", POLISH_A)
    value(ids["name"], "L3539.003", POLISH_A)

    stats = run()

    assert stats["by_language"] == {"TURKISH": 1}
    (row,) = proposals_for(ids["name"], "L3539.001")
    assert row.suspect_flag == "TR?"
    assert "Turkish" in (row.suspect_reason or "")


# ── the second guard: a sibling must be usable AS a translation ────────────
#
# Every string below is one the first production run actually proposed, or one
# it correctly proposed. `reads_foreign` answers "not *certainly* foreign" at
# 0.90 and short English does not reach it, so the run offered English product
# names as Polish and Slovak details, and once offered the literal text "0.0".


@pytest.mark.parametrize(
    "text",
    [
        "Northwind Men's 3-Pack Boxer",              # english 0.40, proposed as Polish
        "Northwind Men's T-Shirt",                   # english 0.44, proposed as Polish
        "Northwind Women's Short Sleeve Denim Dress",  # english 0.86
        "0.0",                                     # proposed as a Slovak description
        "   ",
        "<li></li>",
    ],
)
def test_a_sibling_that_is_not_a_translation_is_refused(text: str) -> None:
    assert usable_as_translation(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Pánske Tepláky",
        "Męskie spodnie dresowe",
        "Pánská bavlněná košile Oxford Regular Fit",
        "Bawełniana koszula Oxford regular fit",
        "<li>80 % bavlna 20 % polyamid</li>",
        "<li>80% Pamut 20% Poliamid</li>",          # Hungarian, misreads as Turkish
        "3 ks boxeriek zo strečovej bavlny",
    ],
)
def test_a_real_market_language_sibling_is_still_allowed(text: str) -> None:
    """The guard must not cost the proposals that were right. The Hungarian
    material line is the one to watch: the detector's top verdict for it is
    TURKISH at 0.37, so anything keying off the top language would drop it.
    English confidence is 0.11 and that is what is checked."""
    assert usable_as_translation(text) is True


def test_short_english_no_longer_reaches_a_proposal() -> None:
    """End to end, with the exact shape that shipped a wrong suggestion: two
    colours agreeing on an English name is two colours that are both wrong."""
    ids = registry()
    value(ids["name"], "L3539.001", TURKISH)
    value(ids["name"], "L3539.002", "Northwind Men's 3-Pack Boxer")
    value(ids["name"], "L3539.003", "Northwind Men's 3-Pack Boxer")

    assert run()["proposed"] == 0
    assert proposals_for(ids["name"], "L3539.001") == []


# ── the grouping row ───────────────────────────────────────────────────────


def grouping_value(attr_id: int, base_code: str, text: str, market: str = "PL") -> None:
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="base_code", entity_key=base_code, attribute_id=attr_id,
            market=market, state="live", state_lane="current", entry_grain="base_code",
            source_kind="omnitron_api", value=text,
        ))


def run_grouping(**kw) -> dict:
    with session_scope() as db:
        return generate(db, [GroupingRowSource(db)], **kw)


def test_the_grouping_row_answers_where_the_sizes_hold_english() -> None:
    """5H1299, in miniature: the sizes say English, the style's grouping row
    says Polish, and the Polish is offered — never written."""
    ids = registry()
    value(ids["name"], "L3558.132", ENGLISH)
    grouping_value(ids["name"], "L3558", POLISH_A)

    stats = run_grouping()
    assert stats["by_source"] == {"grouping_row": 1}
    (proposal,) = proposals_for(ids["name"], "L3558.132")
    assert proposal.value == POLISH_A
    assert proposal.source_ref == "grouping_row:english"
    with session_scope() as db:
        live = db.scalar(select(AttributeValue.value).where(
            AttributeValue.entity_key == "L3558.132", AttributeValue.state_lane == "current",
        ))
    assert live == ENGLISH


def test_a_grouping_row_in_the_wrong_language_proposes_nothing() -> None:
    ids = registry()
    value(ids["name"], "L3558.132", ENGLISH)
    grouping_value(ids["name"], "L3558", TURKISH)
    assert run_grouping()["proposed"] == 0
    assert proposals_for(ids["name"], "L3558.132") == []


def test_a_grouping_row_holding_short_english_is_not_a_translation() -> None:
    """The 0.42.1 lesson applies one level up: `reads_foreign` lets short
    English through, and offered as a Polish detail it is a plausible lie."""
    ids = registry()
    value(ids["name"], "L3558.132", ENGLISH)
    grouping_value(ids["name"], "L3558", "Northwind Men's 3-Pack Boxer")
    assert run_grouping()["proposed"] == 0
    assert proposals_for(ids["name"], "L3558.132") == []


def test_another_style_s_grouping_row_is_not_this_style_s() -> None:
    ids = registry()
    value(ids["name"], "L3558.132", ENGLISH)
    grouping_value(ids["name"], "PH9619", POLISH_A)
    assert run_grouping()["proposed"] == 0


def test_a_grouping_row_in_another_market_is_not_consulted() -> None:
    ids = registry()
    value(ids["name"], "L3558.132", ENGLISH)
    grouping_value(ids["name"], "L3558", POLISH_A, market="CZ")
    assert run_grouping()["proposed"] == 0


def test_two_agreeing_colours_outrank_the_grouping_row() -> None:
    """Both free sources answer; the one with the catalogue's proof goes
    first, and the grouping row is only asked what it could not settle."""
    ids = registry()
    value(ids["name"], "L3558.132", ENGLISH)
    value(ids["name"], "L3558.166", POLISH_B)
    value(ids["name"], "L3558.240", POLISH_B)
    grouping_value(ids["name"], "L3558", POLISH_A)

    with session_scope() as db:
        stats = generate(db, [SiblingSource(db), GroupingRowSource(db)])
    assert stats["by_source"].get("sibling") == 1
    assert stats["by_source"].get("grouping_row", 0) == 0
    (proposal,) = proposals_for(ids["name"], "L3558.132")
    assert proposal.value == POLISH_B



def test_a_sibling_that_is_a_different_garment_is_not_a_sibling() -> None:
    """CH9883 in miniature. A base code is not a style: the live catalogue
    holds an Oxford regular-fit shirt in four colours and a slim-fit checked
    shirt in four others, and two Czech colours agreeing on the Oxford wording
    got it proposed for a checked shirt. The colours agree and the text is
    real Czech, so only the garment attributes can tell."""
    ids = registry()
    value(ids["name"], "CH9883.11Y", TURKISH)
    garment("CH9883.11Y", fit="Slim Fit", pattern="Kareli")
    for colour in ("CH9883.001", "CH9883.F6Z"):
        value(ids["name"], colour, POLISH_A)
        garment(colour, fit="Regular Fit", pattern="Düz")

    assert run()["proposed"] == 0
    assert proposals_for(ids["name"], "CH9883.11Y") == []


def test_the_same_garment_in_another_colour_is_still_a_sibling() -> None:
    """The guard must not refuse the ordinary case it was built around."""
    ids = registry()
    value(ids["name"], "L3539.001", TURKISH)
    for colour in ("L3539.002", "L3539.003"):
        value(ids["name"], colour, POLISH_A)

    assert run()["proposed"] == 1


# ── taking back what is no longer needed ───────────────────────────────────


def test_a_proposal_whose_live_value_now_reads_local_is_withdrawn() -> None:
    """The proposal was raised against English. The live value is Polish now —
    fixed in Omnitron, or read from the key the shop renders — and accepting
    the proposal would push a replacement over good text."""
    ids = registry()
    value(ids["name"], "L3539.001", POLISH_A)
    value(ids["name"], "L3539.001", POLISH_B, lane=PROPOSAL_LANE, state="proposed")

    stats = run()

    assert stats["withdrawn"] == 1
    assert proposals_for(ids["name"], "L3539.001") == []


def test_a_proposal_for_a_foreign_or_empty_cell_stays() -> None:
    ids = registry()
    value(ids["name"], "L3539.001", ENGLISH)
    value(ids["name"], "L3539.001", POLISH_A, lane=PROPOSAL_LANE, state="proposed")
    # No live value at all: a shared-key proposal for a description the shop
    # does not show.
    value(ids["name"], "L3539.002", POLISH_B, lane=PROPOSAL_LANE, state="proposed")

    stats = run()

    assert stats["withdrawn"] == 0
    assert [p.value for p in proposals_for(ids["name"], "L3539.001")] == [POLISH_A]
    assert [p.value for p in proposals_for(ids["name"], "L3539.002")] == [POLISH_B]
