"""Idempotent writes into `attribute_values`.

The three behaviours these pin are the ones that decide whether the table can
be trusted later: unchanged rows stay untouched so `updated_at` keeps meaning
something, provenance churn does not count as change, and values we own are
never silently overwritten by the upstream system.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from pim_studio.db import session_scope
from pim_studio.models import (
    MARKET_AGNOSTIC,
    Attribute,
    AttributeValue,
    ValueChange,
)
from pim_studio.sync.writer import (
    _LOOKUP_CHUNK,
    PROTECTED_STATES,
    upsert_attribute_values,
)

pytestmark = pytest.mark.usefixtures("temp_db")


def an_attribute(key: str = "product_detail") -> int:
    with session_scope() as db:
        a = Attribute(
            key=key,
            label=key,
            write_channel="omnitron_import",
            value_grain="model_color",
        )
        db.add(a)
        db.flush()
        return a.id


def record(attr_id: int, **over) -> dict:
    base = {
        "entity_kind": "sku",
        "entity_key": "2908895",
        "attribute_id": attr_id,
        "market": "PL",
        "value": "Koszulka Polo",
        "state": "live",
        "entry_grain": "sku",
        "source_kind": "omnitron_api",
        "source_ref": "sync_run:1",
    }
    return {**base, **over}


def stored(attr_id: int) -> AttributeValue:
    with session_scope() as db:
        rows = list(
            db.scalars(select(AttributeValue).where(AttributeValue.attribute_id == attr_id))
        )
        assert len(rows) == 1, f"expected one row, got {len(rows)}"
        return rows[0]


# ── insert ─────────────────────────────────────────────────────────────────


def test_first_write_inserts() -> None:
    attr = an_attribute()
    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr)])
    assert (result.inserted, result.updated, result.unchanged) == (1, 0, 0)
    assert stored(attr).value == "Koszulka Polo"


def test_a_market_agnostic_value_stores_the_sentinel_not_null() -> None:
    """NULL would leave the one-row-per-key constraint unenforced, because SQL
    treats NULLs as distinct."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, market=None)])
    assert stored(attr).market == MARKET_AGNOSTIC


def test_the_same_batch_twice_does_not_duplicate() -> None:
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr)])
    assert (result.inserted, result.unchanged) == (0, 1)


# ── unchanged rows are left alone ──────────────────────────────────────────


def test_an_unchanged_value_is_not_rewritten() -> None:
    """If a nightly sync touched every row, `updated_at` would mean "when we
    last looked" and "what changed this week" would be unanswerable."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    before = stored(attr).updated_at

    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr)])

    assert result.unchanged == 1
    assert stored(attr).updated_at == before


def test_a_new_source_ref_alone_is_not_a_change() -> None:
    """`source_ref` names the observing run, so it differs every night by
    construction. Counting it would mark every row dirty every night."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, source_ref="sync_run:1")])
    before = stored(attr)
    was_updated_at, was_ref = before.updated_at, before.source_ref

    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr, source_ref="sync_run:99")])

    assert result.unchanged == 1
    after = stored(attr)
    assert after.updated_at == was_updated_at
    assert after.source_ref == was_ref, "provenance must not churn on its own"


def test_a_real_change_updates_and_refreshes_provenance() -> None:
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, source_ref="sync_run:1")])
    before = stored(attr).updated_at

    with session_scope() as db:
        result = upsert_attribute_values(
            db, [record(attr, value="Polo Shirt", source_ref="sync_run:2")]
        )

    assert result.updated == 1
    after = stored(attr)
    assert after.value == "Polo Shirt"
    assert after.source_ref == "sync_run:2"
    assert after.updated_at > before


def test_a_suspect_flag_appearing_counts_as_a_change() -> None:
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    with session_scope() as db:
        result = upsert_attribute_values(
            db, [record(attr, suspect_flag="language", suspect_reason="looks Turkish")]
        )
    assert result.updated == 1
    assert stored(attr).suspect_flag == "language"


# ── values we own are protected ────────────────────────────────────────────


@pytest.mark.parametrize("state", sorted(PROTECTED_STATES))
def test_in_flight_values_are_not_overwritten(state: str) -> None:
    """`approved` is a human's decision not yet published; `exported` went out
    and is awaiting confirmation. Omnitron disagreeing does not make ours
    wrong — that is the `stale` condition, and resolving it is a decision, not
    a side effect of a nightly write."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, state=state, value="ours")])

    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr, value="theirs")])

    assert result.updated == 0
    assert len(result.protected) == 1
    divergence = result.protected[0]
    assert (divergence.our_value, divergence.their_value) == ("ours", "theirs")
    assert divergence.our_state == state
    assert stored(attr).value == "ours", "the protected value must survive"


def test_agreement_on_a_protected_row_is_not_a_divergence() -> None:
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, state="exported", value="same")])

    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr, value="same")])

    assert result.protected == []
    assert result.unchanged == 1


def test_protection_can_be_waived_for_a_deliberate_rebuild() -> None:
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, state="approved", value="ours")])

    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr, value="theirs")], protect=False)

    assert result.updated == 1
    assert stored(attr).value == "theirs"


def test_a_proposal_lane_row_is_never_protected() -> None:
    """Protection is about the `current` lane. A proposal is not something the
    upstream system is competing with."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(
            db, [record(attr, state="approved", state_lane="proposal", value="ours")]
        )

    with session_scope() as db:
        result = upsert_attribute_values(
            db, [record(attr, state_lane="proposal", value="theirs")]
        )

    assert result.protected == []
    assert result.updated == 1


# ── batching ───────────────────────────────────────────────────────────────


def test_a_mixed_batch_is_counted_accurately() -> None:
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(
            db,
            [
                record(attr, entity_key="a", value="unchanged"),
                record(attr, entity_key="b", value="will change"),
                record(attr, entity_key="c", state="approved", value="protected"),
            ],
        )

    with session_scope() as db:
        result = upsert_attribute_values(
            db,
            [
                record(attr, entity_key="a", value="unchanged"),
                record(attr, entity_key="b", value="changed"),
                record(attr, entity_key="c", value="theirs"),
                record(attr, entity_key="d", value="brand new"),
            ],
        )

    assert (result.inserted, result.updated, result.unchanged) == (1, 1, 1)
    assert len(result.protected) == 1
    assert result.written == 2


def test_an_empty_batch_is_a_no_op() -> None:
    with session_scope() as db:
        result = upsert_attribute_values(db, [])
    assert (result.inserted, result.updated, result.unchanged) == (0, 0, 0)


def test_a_batch_larger_than_the_lookup_chunk_is_handled() -> None:
    """The realistic shape is 100 SKUs times ~40 attributes, and the existence
    query binds one parameter per key column. Without internal chunking that is
    20,000 binds — fine on a current SQLite, a hard failure on an older build
    or against PostgreSQL's 65,535 ceiling. This crosses the boundary twice.
    """
    attr = an_attribute()
    size = _LOOKUP_CHUNK * 2 + 7
    batch = [record(attr, entity_key=f"sku{n}", value=f"v{n}") for n in range(size)]

    with session_scope() as db:
        first = upsert_attribute_values(db, batch)
    assert first.inserted == len(batch)

    # Second pass: every row must be recognised as already present, which only
    # works if the chunked lookup saw all of them.
    with session_scope() as db:
        second = upsert_attribute_values(db, batch)
    assert second.unchanged == len(batch)
    assert second.inserted == 0

    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AttributeValue)) == len(batch)


def test_market_is_part_of_the_key() -> None:
    """Four markets, four rows, one attribute — the thing the schema exists for."""
    attr = an_attribute()
    with session_scope() as db:
        result = upsert_attribute_values(
            db, [record(attr, market=m, value=f"name in {m}") for m in ("PL", "CZ", "HU", "SK")]
        )
    assert result.inserted == 4
    with session_scope() as db:
        rows = list(db.scalars(select(AttributeValue)))
    assert {r.market for r in rows} == {"PL", "CZ", "HU", "SK"}


# ── history ────────────────────────────────────────────────────────────────
#
# `value_changes` is written from inside this function because this is the only
# place that holds the old value and the new one at the same instant. One line
# further on, the old value does not exist anywhere. What these pin is mostly
# what does *not* get recorded — a history that logs non-events is a history
# nobody reads twice.


def changes() -> list[ValueChange]:
    with session_scope() as db:
        return list(db.scalars(select(ValueChange).order_by(ValueChange.id)))


def test_a_changed_value_records_what_it_was_and_what_it_became():
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, value="Polo Shirt")])

    logged = changes()
    assert len(logged) == 1
    assert (logged[0].old_value, logged[0].new_value) == ("Koszulka Polo", "Polo Shirt")


def test_an_insertion_is_not_a_change():
    """There is no 'before'. Recording insertions would also make the first
    sync against an empty database write a row per value to say nothing
    happened."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    assert changes() == []


def test_writing_the_same_value_again_records_nothing():
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    assert changes() == []


def test_a_lifecycle_hop_alone_is_not_a_value_change():
    """`state` is material — it is written — but it is not what this table is
    about. Mixing the two would make 'what did this say' a query with a filter
    on it."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, state="exported")])
    assert changes() == []


def test_a_refused_overwrite_records_nothing():
    """Upstream tried and the writer declined, so the value is what it always
    was. Recording it would put a change in the log that never happened."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, state=sorted(PROTECTED_STATES)[0])])
    with session_scope() as db:
        result = upsert_attribute_values(db, [record(attr, value="Overwritten")])
    assert result.protected
    assert changes() == []


def test_blank_to_a_value_is_a_change_not_an_insertion():
    """A row that exists holding nothing is evidence the column was read — the
    distinction the coverage module turns on — so filling it is a change."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, value=None)])
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, value="Koszulka")])

    logged = changes()
    assert len(logged) == 1
    assert logged[0].old_value is None
    assert logged[0].new_value == "Koszulka"


def test_going_back_is_two_events_not_an_erasure():
    """A → B → A. Append-only: 'how often does this flap' is a question about
    the sequence, and a row edited twice cannot answer it."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, value="A")])
    for value in ("B", "A"):
        with session_scope() as db:
            upsert_attribute_values(db, [record(attr, value=value)])

    assert [(c.old_value, c.new_value) for c in changes()] == [("A", "B"), ("B", "A")]


def test_the_run_and_the_person_are_stamped_when_given():
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    with session_scope() as db:
        upsert_attribute_values(
            db,
            [record(attr, value="Edited by hand")],
            sync_run_id=77,
            changed_by="emir@example.test",
        )

    logged = changes()
    assert (logged[0].sync_run_id, logged[0].changed_by) == (77, "emir@example.test")


def test_history_is_unattributed_rather_than_missing():
    """Optional on purpose. A caller with no run to name should still record
    the change — an unattributed change is far more useful than none."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, value="Nameless")])

    assert changes()[0].sync_run_id is None


def test_history_rolls_back_with_the_write_it_describes():
    """In the caller's transaction. A history that claims changes which never
    landed is worse than none, because it reads exactly like the real thing."""
    attr = an_attribute()
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr)])

    with pytest.raises(RuntimeError), session_scope() as db:
        upsert_attribute_values(db, [record(attr, value="Never committed")])
        raise RuntimeError("the caller failed after writing")

    assert changes() == []
    assert stored(attr).value == "Koszulka Polo"


def test_one_upstream_edit_across_sizes_shares_a_timestamp():
    """Six SKUs changed by one edit is one event. Six timestamps milliseconds
    apart would render as six lines on the product timeline."""
    attr = an_attribute()
    skus = [f"29088{n}" for n in range(6)]
    with session_scope() as db:
        upsert_attribute_values(db, [record(attr, entity_key=s) for s in skus])
    with session_scope() as db:
        upsert_attribute_values(
            db, [record(attr, entity_key=s, value="New") for s in skus]
        )

    assert len({c.changed_at for c in changes()}) == 1
