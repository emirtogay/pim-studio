"""Full versus incremental — the entitlement to conclude that something is gone.

Everything here defends one invariant: **only a full pass may subtract.** An
incremental read returns "what changed since the watermark", and on a quiet
night that is close to nothing. Any sweep that works by subtraction — the
catalogue's departure marking, liveness's price and stock retirement — would
read that near-nothing as "upstream has lost almost everything" and act on it.

The damage would not look like a crash. `left_at` being set is a normal thing
for that code to do, and a deleted price row is indistinguishable from one that
was never written. It would look like a successful sync and a catalogue that
quietly emptied overnight, which is precisely the class of failure this project
keeps rediscovering.

So the guard is a raise, not a branch, and these tests pin the raise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeValue,
    CatalogueMembership,
    Product,
    ValueRetirement,
)
from pim_studio.models import SyncRun as SyncRunRow
from pim_studio.omnitron import OmnitronClient
from pim_studio.sync import catalogue, liveness
from pim_studio.sync.runs import (
    MODE_FULL,
    MODE_INCREMENTAL,
    IncrementalSweepViolation,
    SyncMode,
    SyncRun,
    last_full_run,
    last_successful_run,
    sync_run,
)

pytestmark = pytest.mark.usefixtures("temp_db")

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}

PL_CATALOG = 4


# ── the mode type itself ───────────────────────────────────────────────────


def test_full_mode_carries_no_watermark() -> None:
    """`since` must be None in full mode, so `if mode.since:` builds params safely."""
    mode = SyncMode(full=True)
    assert mode.since is None
    assert mode.label == MODE_FULL


def test_incremental_mode_is_labelled_and_windowed() -> None:
    when = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)
    mode = SyncMode(full=False, since=when)
    assert mode.label == MODE_INCREMENTAL
    assert mode.since == when


def test_require_full_passes_in_full_mode() -> None:
    SyncMode(full=True).require_full("a sweep")  # does not raise


def test_require_full_raises_in_incremental_mode() -> None:
    mode = SyncMode(full=False, since=datetime(2026, 8, 14, tzinfo=UTC))
    with pytest.raises(IncrementalSweepViolation) as excinfo:
        mode.require_full("the catalogue departure sweep")
    # The message has to name the fix, not just the fault — this exception is
    # most likely to be read by whoever wired a new sweep incorrectly.
    assert "catalogue departure sweep" in str(excinfo.value)
    assert "full pass" in str(excinfo.value)


def test_resolve_promotes_to_full_when_there_is_no_watermark() -> None:
    """A first run has nothing to be incremental from, so it must read everything.

    Getting this wrong is silent either way: depending on how the filter is
    built, "since None" either fetches nothing or fetches everything.
    """
    mode = SyncMode.resolve("omnitron_catalogue", full=False)
    assert mode.full is True
    assert mode.since is None


def test_resolve_uses_the_last_successful_run_as_the_watermark() -> None:
    with sync_run("omnitron_catalogue") as run:
        run.imported(1)

    mode = SyncMode.resolve("omnitron_catalogue", full=False)
    assert mode.full is False
    assert mode.since == last_successful_run("omnitron_catalogue")


def test_resolve_honours_an_explicit_full_request_even_with_a_watermark() -> None:
    with sync_run("omnitron_catalogue") as run:
        run.imported(1)

    mode = SyncMode.resolve("omnitron_catalogue", full=True)
    assert mode.full is True
    assert mode.since is None


# ── the run row records which kind of pass it was ──────────────────────────


def test_the_run_row_records_its_mode() -> None:
    """Without this the runs page cannot tell a nightly delta from a reconcile."""
    with sync_run("omnitron_catalogue", mode=SyncMode(full=False, since=datetime.now(UTC))):
        pass
    with session_scope() as db:
        row = db.scalars(select(SyncRunRow)).one()
    assert row.mode == MODE_INCREMENTAL


def test_a_run_defaults_to_full_when_no_mode_is_given() -> None:
    """Stages that never learned about modes keep their old meaning."""
    with sync_run("omnitron_registry"):
        pass
    with session_scope() as db:
        row = db.scalars(select(SyncRunRow)).one()
    assert row.mode == MODE_FULL


def test_last_full_run_ignores_incremental_runs() -> None:
    """A chain of green nights says nothing about when we last reconciled."""
    with sync_run("omnitron_catalogue", mode=SyncMode(full=True)):
        pass
    full_at = last_full_run("omnitron_catalogue")
    assert full_at is not None

    later = datetime.now(UTC) + timedelta(seconds=1)
    with sync_run("omnitron_catalogue", mode=SyncMode(full=False, since=later)):
        pass

    # The newer run advances the plain watermark but not the reconciliation one.
    assert last_full_run("omnitron_catalogue") == full_at
    assert last_successful_run("omnitron_catalogue") != full_at


def test_retirements_are_counted_apart_from_drops() -> None:
    """A mass deletion must not hide inside a routinely-large number."""
    with sync_run("omnitron_catalogue") as run:
        run.dropped(3, reason="unparseable")
        run.retired(40_000, reason="left the catalogue")

    with session_scope() as db:
        row = db.scalars(select(SyncRunRow)).one()
    assert row.rows_dropped == 3
    assert row.rows_retired == 40_000
    assert "retired 40000 row(s)" in (row.error_summary or "")
    assert "dropped 3 row(s)" in (row.error_summary or "")


# ── the catalogue departure sweep ──────────────────────────────────────────


def _catalogue_api(memberships: dict[int, list[int]], upstream: list[dict]):
    """A tenant whose catalog_items honours `modified_date__gte` by returning none.

    That is the realistic incremental case and the dangerous one: a quiet night
    returns an empty window, and an unguarded sweep would then retire the lot.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if "catalog_items" in path:
            if params.get("modified_date__gte"):
                rows: list[dict] = []
            else:
                cat = int(params.get("catalog"))
                rows = [
                    {"pk": i, "product": p, "catalog": cat}
                    for i, p in enumerate(memberships.get(cat, []), start=1)
                ]
        else:
            asked = {int(x) for x in (params.get("pk__in") or "").split(",") if x}
            rows = [p for p in upstream if p["pk"] in asked]
        return httpx.Response(200, json={"count": len(rows), "next": None, "results": rows})

    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    return c


def _product(pk: int, sku: str) -> dict:
    return {
        "pk": pk,
        "sku": sku,
        "is_active": True,
        "modified_date": "2026-08-10T11:14:09.701924Z",
        "attributes": {"integration_base_code_format": "L3539.132.T2"},
    }


def _live_memberships() -> list[CatalogueMembership]:
    with session_scope() as db:
        return list(
            db.scalars(
                select(CatalogueMembership).where(CatalogueMembership.left_at.is_(None))
            )
        )


def test_a_full_pass_marks_a_product_that_left_the_catalogue() -> None:
    api = _catalogue_api({PL_CATALOG: [11, 12]}, [_product(11, "s11"), _product(12, "s12")])
    catalogue.discover_products(api, SyncRun(1, "omnitron_catalogue"), SyncMode(full=True))
    assert len(_live_memberships()) == 2

    # 12 is gone from PL upstream. A full pass is entitled to notice.
    api = _catalogue_api({PL_CATALOG: [11]}, [_product(11, "s11")])
    run = SyncRun(2, "omnitron_catalogue")
    stats = catalogue.discover_products(api, run, SyncMode(full=True))

    assert stats["departed"] == 1
    assert run.rows_retired == 1
    assert len(_live_memberships()) == 1


def test_an_incremental_pass_never_marks_a_departure() -> None:
    """The load-bearing test. An empty window must not empty the table."""
    api = _catalogue_api({PL_CATALOG: [11, 12]}, [_product(11, "s11"), _product(12, "s12")])
    catalogue.discover_products(api, SyncRun(1, "omnitron_catalogue"), SyncMode(full=True))
    assert len(_live_memberships()) == 2

    # A quiet night: the windowed read returns nothing at all.
    run = SyncRun(2, "omnitron_catalogue")
    stats = catalogue.discover_products(
        api, run, SyncMode(full=False, since=datetime(2026, 8, 19, tzinfo=UTC))
    )

    assert stats["catalogue_union"] == 0, "the window really is empty"
    assert stats["departed"] == 0
    assert run.rows_retired == 0
    assert len(_live_memberships()) == 2, "nothing may be retired on an incremental pass"


# ── the liveness retirement sweep ──────────────────────────────────────────


def _seed_liveness_fixture() -> dict[str, int]:
    """One SKU with a PL price and stock already held, plus the registry rows."""
    with session_scope() as db:
        ids: dict[str, int] = {}
        for key in ("price", "retail_price", "stock"):
            attr = Attribute(
                key=key, label=key, data_type="number", write_channel="read_only",
                value_grain="sku", per_market=True, long_text=False,
                gates_go_live=False, display_group="live", display_order=1,
            )
            db.add(attr)
            db.flush()
            ids[key] = attr.id
        db.add(Product(sku="s11", base_code_format="L3539.132.T2",
                       model_color="L3539.132", base_code="L3539", omnitron_pk=11))
        for key in ("price", "stock"):
            db.add(AttributeValue(
                entity_kind="sku", entity_key="s11", attribute_id=ids[key],
                market="PL", state_lane="current", value="10", state="live",
                entry_grain="sku", source_kind="omnitron_api",
            ))
        return ids


def _held(market: str = "PL") -> set[tuple[str, int]]:
    with session_scope() as db:
        return {
            (r[0], r[1])
            for r in db.execute(
                select(AttributeValue.entity_key, AttributeValue.attribute_id)
                .where(AttributeValue.market == market)
            ).all()
        }


def _retirements() -> list[ValueRetirement]:
    with session_scope() as db:
        return list(db.scalars(select(ValueRetirement).order_by(ValueRetirement.id)))


def test_a_full_pass_retires_a_price_upstream_no_longer_has() -> None:
    """The bug this fixes: withdrawn prices used to live forever, still `live`."""
    ids = _seed_liveness_fixture()
    assert len(_held()) == 2

    run = SyncRun(1, "omnitron_liveness", mode=SyncMode(full=True))
    retired = liveness._retire_unobserved("PL", list(ids.values()), observed=set(), run=run)

    assert retired == 2
    assert _held() == set()
    assert run.rows_retired == 2


def test_a_retirement_records_what_was_lost() -> None:
    """A count is a number; this is what makes it a work queue.

    Deleting without recording left "retired 500 rows" with no way to ask which
    500, which is the gap this table exists to close. `last_value` matters
    specifically: "lost its PL price" and "lost its PL price, which was 10" are
    different amounts of help to whoever picks it up.
    """
    ids = _seed_liveness_fixture()
    run = SyncRun(7, "omnitron_liveness", mode=SyncMode(full=True))

    liveness._retire_unobserved("PL", list(ids.values()), observed=set(), run=run)

    events = _retirements()
    assert len(events) == 2
    assert {e.entity_key for e in events} == {"s11"}
    assert {e.market for e in events} == {"PL"}
    assert {e.last_value for e in events} == {"10"}, "the value we lost, not just its key"
    assert all(e.sync_run_id == 7 for e in events), "traceable back to its own run"
    assert all("no longer priced or stocked" in e.reason for e in events)


def test_a_retirement_survives_the_value_it_describes() -> None:
    """The event outlives the row — that is the whole design.

    The `attribute_values` row must go, because the model's invariant is that
    an absent row is the absence. Keeping it behind a flag would put a filter
    in every read, and the read that forgot it would count a sold-out product
    as live.
    """
    ids = _seed_liveness_fixture()
    run = SyncRun(1, "omnitron_liveness", mode=SyncMode(full=True))

    liveness._retire_unobserved("PL", list(ids.values()), observed=set(), run=run)

    assert _held() == set(), "the value is gone from the current-state table"
    assert len(_retirements()) == 2, "and the event remains"


def _restock(ids: dict[str, int]) -> None:
    """Put the price and stock values back, as a later sync would."""
    with session_scope() as db:
        for key in ("price", "stock"):
            db.add(AttributeValue(
                entity_kind="sku", entity_key="s11", attribute_id=ids[key],
                market="PL", state_lane="current", value="10", state="live",
                entry_grain="sku", source_kind="omnitron_api",
            ))


def test_selling_out_twice_is_two_events() -> None:
    """Append-only: 'how often does this sell out' is a question about the
    sequence, so a repeat is a second row rather than an edit of the first."""
    ids = _seed_liveness_fixture()
    liveness._retire_unobserved(
        "PL", list(ids.values()), observed=set(),
        run=SyncRun(1, "omnitron_liveness", mode=SyncMode(full=True)),
    )
    _restock(ids)
    liveness._retire_unobserved(
        "PL", list(ids.values()), observed=set(),
        run=SyncRun(2, "omnitron_liveness", mode=SyncMode(full=True)),
    )

    assert len(_retirements()) == 4
    assert {e.sync_run_id for e in _retirements()} == {1, 2}


def test_a_refused_sweep_records_nothing() -> None:
    """The guard must fire before anything is written, not after."""
    ids = _seed_liveness_fixture()
    run = SyncRun(1, "omnitron_liveness", mode=SyncMode(full=False, since=datetime.now(UTC)))

    with pytest.raises(IncrementalSweepViolation):
        liveness._retire_unobserved("PL", list(ids.values()), observed=set(), run=run)

    assert _retirements() == []


def test_a_full_pass_keeps_what_it_observed() -> None:
    ids = _seed_liveness_fixture()
    run = SyncRun(1, "omnitron_liveness", mode=SyncMode(full=True))

    retired = liveness._retire_unobserved(
        "PL", list(ids.values()), observed={("s11", ids["price"])}, run=run
    )

    assert retired == 1, "only the stock row was unobserved"
    assert _held() == {("s11", ids["price"])}


def test_the_sweep_refuses_to_run_on_an_incremental_pass() -> None:
    ids = _seed_liveness_fixture()
    run = SyncRun(1, "omnitron_liveness", mode=SyncMode(full=False, since=datetime.now(UTC)))

    with pytest.raises(IncrementalSweepViolation):
        liveness._retire_unobserved("PL", list(ids.values()), observed=set(), run=run)

    assert len(_held()) == 2, "the refusal must happen before anything is deleted"


def test_the_sweep_leaves_values_it_did_not_write() -> None:
    """A human's approved price is not ours to delete because we did not read it.

    Same boundary `writer.PROTECTED_STATES` draws on the write path: work in
    flight belongs to whoever put it there.
    """
    ids = _seed_liveness_fixture()
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="sku", entity_key="s99", attribute_id=ids["price"],
            market="PL", state_lane="current", value="99", state="approved",
            entry_grain="sku", source_kind="manual",
        ))

    run = SyncRun(1, "omnitron_liveness", mode=SyncMode(full=True))
    liveness._retire_unobserved("PL", list(ids.values()), observed=set(), run=run)

    assert _held() == {("s99", ids["price"])}


def test_the_sweep_stays_inside_its_own_market() -> None:
    ids = _seed_liveness_fixture()
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="sku", entity_key="s11", attribute_id=ids["price"],
            market="CZ", state_lane="current", value="12", state="live",
            entry_grain="sku", source_kind="omnitron_api",
        ))

    run = SyncRun(1, "omnitron_liveness", mode=SyncMode(full=True))
    liveness._retire_unobserved("PL", list(ids.values()), observed=set(), run=run)

    assert _held("PL") == set()
    assert _held("CZ") == {("s11", ids["price"])}
