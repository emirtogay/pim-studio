"""Taking a product off a market's website.

The behaviours worth pinning are the ones that make this safe to hand to a
person with a button: the market grouping is derived rather than stated, a
market that cannot be acted on alone says so, the record is written before
anything is deleted, and a half-finished run reports itself as half-finished.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import CatalogueRemoval, CatalogueTakedown, Product
from pim_studio.omnitron import OmnitronClient
from pim_studio.sync import takedown

pytestmark = pytest.mark.usefixtures("temp_db")

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}

PL, CZ, HU, SK, GLOBAL = 4, 40, 41, 172, 601

#: The one group the tenant allows since HU and SK joined PL and CZ on 601.
ALL = "PL+CZ+HU+SK"


@pytest.fixture(autouse=True)
def switches_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_takedown_enabled", True)


def seed(skus: dict[str, int | None], model_color: str = "L3539.132") -> None:
    """sku -> omnitron pk (None for a size we hold but cannot resolve)."""
    with session_scope() as db:
        for sku, pk in skus.items():
            db.add(
                Product(
                    sku=sku,
                    base_code_format=f"{model_color}.T{sku[-1]}",
                    model_color=model_color,
                    base_code=model_color.split(".")[0],
                    size=sku[-1],
                    omnitron_pk=pk,
                    omnitron_product_type=0,
                )
            )


def api_with(memberships: dict[int, dict[int, int]], *, fail: set[int] = frozenset()):
    """memberships maps catalog pk -> {product pk: catalog_item pk}.

    `fail` names catalog_item pks whose DELETE returns 500, so a partial run
    can be exercised.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        calls.append((request.method, path))
        if request.method == "DELETE":
            item = int(path.rstrip("/").rsplit("/", 1)[-1])
            if item in fail:
                return httpx.Response(500, text="upstream said no")
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(201, json={"pk": 999})
        if path.startswith("/api/v1/catalog_items/") and path.count("/") > 4:
            item = int(path.rstrip("/").rsplit("/", 1)[-1])
            for cat, rows in memberships.items():
                for product, item_pk in rows.items():
                    if item_pk == item:
                        return httpx.Response(
                            200, json={"pk": item, "product": product, "catalog": cat}
                        )
            return httpx.Response(404, text="no such item")
        cat = int(params.get("catalog"))
        rows = [
            {"pk": item_pk, "product": product, "catalog": cat}
            for product, item_pk in memberships.get(cat, {}).items()
        ]
        return httpx.Response(200, json={"count": len(rows), "next": None, "results": rows})

    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    c._calls = calls  # type: ignore[attr-defined]
    return c


# ── the grouping ───────────────────────────────────────────────────────────


def test_all_four_markets_are_one_group_because_they_all_read_601() -> None:
    """The finding this whole feature is shaped around. Derived from
    `scope.MARKETS`, so when HU and SK joined PL and CZ on catalogue 601
    (2026-09-16) the grouping followed with no change here."""
    assert takedown.GROUPS == (("PL", "CZ", "HU", "SK"),)
    assert sorted(takedown.GROUP_BY_KEY) == [ALL]


@pytest.mark.parametrize("market", ["PL", "CZ", "HU", "SK"])
def test_asking_for_one_market_alone_is_refused_with_the_reason(market: str) -> None:
    """Not widened silently. Someone asking for one market alone holds a belief
    about the tenant that is wrong, and the error is where they find out."""
    with pytest.raises(takedown.TakedownError) as e:
        takedown.group_for(market)
    assert f"'{ALL}'" in str(e.value)


def test_the_components_are_derived_not_stated(monkeypatch) -> None:
    """The layout before 2026-09-16 — only PL and CZ on 601 — still gives
    three groups, and a market that owns its catalogue outright is a group of
    its own. Pinned on the derivation, so it keeps being tested now that the
    live layout happens to be one group."""
    from pim_studio.sync import scope

    before = dict(scope.MARKETS)
    before["HU"] = scope.Market("HU", "Hungary Northwind", 102, 41, 135, 167, 134)
    before["SK"] = scope.Market(
        "SK", "Northwind SK", 167, 172, 167, 169, 266, extra_price_list_pks=(200,)
    )
    monkeypatch.setattr(takedown, "MARKETS", before)
    assert takedown._groups() == (("PL", "CZ"), ("HU",), ("SK",))


def test_the_group_touches_every_catalogue_its_markets_read() -> None:
    assert takedown.catalogs_for_group(("PL", "CZ", "HU", "SK")) == (PL, CZ, HU, SK, GLOBAL)


def test_a_group_that_leaks_into_another_market_is_refused() -> None:
    """A grouping built from a stale map must stop rather than reach a market
    nobody was shown. `("PL", "CZ")` is exactly that map since HU and SK read
    601 too."""
    with pytest.raises(takedown.TakedownError, match="out of date"):
        takedown.catalogs_for_group(("PL", "CZ"))


# ── planning ───────────────────────────────────────────────────────────────


def test_a_plan_names_every_size_and_catalogue_the_delete_would_touch() -> None:
    seed({"2001211": 101, "2001212": 102})
    api = api_with({GLOBAL: {101: 9101, 102: 9102}, PL: {101: 8101}})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
    assert plan.size_count == 2
    assert len(plan.removals) == 3, "two sizes in 601, one of them also in 4"
    assert plan.catalogs_hit == (PL, GLOBAL)
    assert plan.markets == ("PL", "CZ", "HU", "SK")


def test_a_size_with_no_omnitron_id_is_named_not_counted() -> None:
    """"Two sizes stayed on sale" needs to say which two."""
    seed({"2001211": 101, "2001212": None})
    api = api_with({GLOBAL: {101: 9101}})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
    assert plan.unresolved_skus == ["2001212"]
    assert plan.size_count == 1


def test_membership_absent_from_the_group_is_an_empty_plan() -> None:
    """A product no catalogue of the group carries has nothing to take off."""
    seed({"2001211": 101})
    api = api_with({})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
    assert plan.is_empty


def test_planning_an_option_the_panel_does_not_hold_is_refused() -> None:
    api = api_with({})
    with session_scope() as db, pytest.raises(takedown.TakedownError, match="not an option"):
        takedown.plan(db, api, "L8635.999", ALL)


# ── executing ──────────────────────────────────────────────────────────────


def test_the_record_is_written_before_anything_is_deleted() -> None:
    """After the delete the (product, catalogue) pairs exist nowhere else, so
    recording them afterwards would leave a crash mid-run unrecoverable."""
    seed({"2001211": 101})
    api = api_with({GLOBAL: {101: 9101}})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(
            db, plan, created_by="a@b.c", created_by_user_id=1
        )
        assert row.status == "planned"
        stored = db.scalars(
            select(CatalogueRemoval).where(CatalogueRemoval.takedown_id == row.id)
        ).all()
        assert [r.state for r in stored] == ["planned"]
        assert stored[0].omnitron_product_pk == 101
        assert stored[0].catalog_pk == GLOBAL
    assert api._calls == [("GET", "/api/v1/catalog_items/")] * 5, "no delete yet"


def test_a_clean_run_deletes_every_row_and_reads_done() -> None:
    seed({"2001211": 101, "2001212": 102})
    api = api_with({GLOBAL: {101: 9101, 102: 9102}})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(db, plan, created_by="a@b.c", created_by_user_id=1)
        takedown.execute(db, api, row)
        assert row.status == "done"
        assert row.removed_count == 2
        assert row.failed_count == 0
    assert sum(1 for m, _ in api._calls if m == "DELETE") == 2


def test_a_half_finished_run_reads_partial_and_names_the_survivor() -> None:
    """Six sizes gone and two still selling is worse than either end, and
    nobody finds it by reading a success message."""
    seed({"2001211": 101, "2001212": 102})
    api = api_with({GLOBAL: {101: 9101, 102: 9102}}, fail={9102})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(db, plan, created_by="a@b.c", created_by_user_id=1)
        takedown.execute(db, api, row)
        assert row.status == "partial"
        assert row.removed_count == 1
        assert row.failed_count == 1
        assert row.error_summary
        failed = db.scalars(
            select(CatalogueRemoval).where(CatalogueRemoval.state == "failed")
        ).all()
        assert [r.sku for r in failed] == ["2001212"]


def test_a_run_where_nothing_lands_reads_failed_not_partial() -> None:
    seed({"2001211": 101})
    api = api_with({GLOBAL: {101: 9101}}, fail={9101})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(db, plan, created_by="a@b.c", created_by_user_id=1)
        takedown.execute(db, api, row)
        assert row.status == "failed"


# ── restoring ──────────────────────────────────────────────────────────────


def test_a_restore_puts_every_removed_row_back() -> None:
    seed({"2001211": 101, "2001212": 102})
    api = api_with({GLOBAL: {101: 9101, 102: 9102}})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(db, plan, created_by="a@b.c", created_by_user_id=1)
        takedown.execute(db, api, row)
        takedown.restore(db, api, row)
        assert row.status == "restored"
        assert row.restored_at is not None
        states = db.scalars(
            select(CatalogueRemoval.state).where(CatalogueRemoval.takedown_id == row.id)
        ).all()
        assert set(states) == {"restored"}
    assert sum(1 for m, _ in api._calls if m == "POST") == 2


def test_a_restore_leaves_rows_that_never_came_out_alone() -> None:
    """The failed size was never deleted; posting it back would put a product
    into a catalogue on the strength of a delete that did not happen."""
    seed({"2001211": 101, "2001212": 102})
    api = api_with({GLOBAL: {101: 9101, 102: 9102}}, fail={9102})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(db, plan, created_by="a@b.c", created_by_user_id=1)
        takedown.execute(db, api, row)
        takedown.restore(db, api, row)
    assert sum(1 for m, _ in api._calls if m == "POST") == 1


def test_restoring_a_takedown_with_nothing_removed_is_refused() -> None:
    seed({"2001211": 101})
    api = api_with({GLOBAL: {101: 9101}})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(db, plan, created_by="a@b.c", created_by_user_id=1)
        with pytest.raises(takedown.TakedownError, match="nothing to restore"):
            takedown.restore(db, api, row)


def test_a_takedown_survives_as_the_record_of_what_was_removed() -> None:
    """The row is the undo, not the log — so it holds the pks a restore needs
    even after the product row has moved on."""
    seed({"2001211": 101})
    api = api_with({GLOBAL: {101: 9101}})
    with session_scope() as db:
        plan = takedown.plan(db, api, "L3539.132", ALL)
        row = takedown.record_plan(db, plan, created_by="a@b.c", created_by_user_id=1)
        takedown.execute(db, api, row)
    with session_scope() as db:
        stored = db.scalars(select(CatalogueTakedown)).one()
        assert stored.markets == "PL,CZ,HU,SK"
        assert stored.catalogs == "4,40,41,172,601"
        assert stored.created_by == "a@b.c"
        removal = db.scalars(select(CatalogueRemoval)).one()
        assert (removal.omnitron_product_pk, removal.catalog_pk) == (101, GLOBAL)
