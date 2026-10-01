"""The push — what it sends, what it refuses, and when it stops.

A fake Omnitron sits behind the client: a store of attributes per SKU per
locale view, with merge semantics on the upsert, so every test can say what
the tenant held before and after. The properties defended are the ones that
keep a Polish edit out of the Turkish store: only the market's keys go out,
under the market's locale, a changed upstream value is never overwritten, and
a write that moved anything else halts the batch.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from pim_studio.config import settings
from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeColumn,
    AttributeValue,
    OmnitronAttribute,
    Product,
    PushBatch,
    PushItem,
    ValueChange,
)
from pim_studio.models import SyncRun as SyncRunRow
from pim_studio.omnitron import OmnitronClient
from pim_studio.sync import push
from pim_studio.sync.push import (
    NothingToPush,
    PushBusy,
    TooManyItems,
    WritesNotEnabled,
    build_batch,
    isolation_breaches,
    run_batch,
)
from pim_studio.sync.runs import SyncRun

pytestmark = pytest.mark.usefixtures("temp_db")

CREDS = {"base_url": "https://pim.example.test", "username": "u", "password": "p"}
LOCALIZABLE = {"urun_adi_pl", "urun_adi_cz"}


# ── a fake tenant ──────────────────────────────────────────────────────────


class FakeOmnitron:
    """Attributes per SKU per view. `None` is the default (Turkish) view.

    A localizable key holds one copy per view; a plain key holds one copy the
    default view owns and every other view echoes. The upsert merges. `leak`
    makes it also clobber an unrelated key, which is the failure the read-back
    exists to catch.
    """

    def __init__(self, leak: str | None = None, fail_skus: set[str] | None = None) -> None:
        self.store: dict[str, dict[str | None, dict[str, str]]] = {}
        #: (locale, pk, attributes) per PATCH, so a test can assert exactly
        #: what went out and under which language.
        self.posts: list[tuple[str | None, dict]] = []
        self.leak = leak
        self.fail_skus = fail_skus or set()
        #: Omnitron's own product id, which the write is addressed to. Handed
        #: out here so the fake can map a patched pk back to its sku.
        self.pks: dict[str, int] = {}
        self._next_pk = 1000

    def add(self, sku: str, **attrs: str) -> None:
        self.store[sku] = {None: dict(attrs)}
        self._next_pk += 1
        self.pks[sku] = self._next_pk

    def sku_of(self, pk: int) -> str | None:
        return next((s for s, p in self.pks.items() if p == pk), None)

    def view(self, sku: str, locale: str | None) -> dict[str, str]:
        base = dict(self.store[sku][None])
        if locale is None:
            return base
        base.update(self.store[sku].get(locale, {}))
        return base

    def handler(self, request: httpx.Request) -> httpx.Response:
        locale = request.headers.get("accept-language")
        if request.method == "GET":
            asked = parse_qs(request.url.query.decode())["sku__in"][0].split(",")
            results = [
                {"sku": sku, "pk": self.pks[sku], "attributes": self.view(sku, locale)}
                for sku in asked
                if sku in self.store
            ]
            return httpx.Response(
                200, json={"count": len(results), "next": None, "results": results}
            )
        # The one write: PATCH /api/v1/products/{pk}/ carrying `attributes`.
        assert request.method == "PATCH", request.method
        pk = int(request.url.path.rstrip("/").rsplit("/", 1)[-1])
        sku = self.sku_of(pk)
        assert sku is not None, f"patched an unknown pk {pk}"
        body = json.loads(request.content)
        assert set(body) == {"attributes"}, body
        self.posts.append((locale, {"sku": sku, **body}))
        if sku in self.fail_skus:
            return httpx.Response(500, json={"detail": "boom"})
        for key, value in body["attributes"].items():
            if key in LOCALIZABLE and locale is not None:
                self.store[sku].setdefault(locale, {})[key] = value
            else:
                self.store[sku][None][key] = value
        if self.leak:
            self.store[sku][None][self.leak] = "clobbered"
        return httpx.Response(200, json={"pk": pk})


def client(fake: FakeOmnitron) -> OmnitronClient:
    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    c._token = "test-token"
    return c


# ── our side ───────────────────────────────────────────────────────────────


def registry() -> dict[str, int]:
    with session_scope() as db:
        name = Attribute(
            key="product_name", label="Product name", write_channel="omnitron_import",
            value_grain="model_color", per_market=True,
        )
        season = Attribute(
            key="pl_sezon", label="Season", write_channel="omnitron_import",
            value_grain="model_color", per_market=False,
        )
        db.add_all([name, season])
        db.flush()
        db.add_all([
            AttributeColumn(attribute_id=name.id, source="omnitron",
                            column_name="attributes__urun_adi_pl", market="PL", api_writable=True),
            AttributeColumn(attribute_id=name.id, source="omnitron",
                            column_name="attributes__urun_adi_cz", market="CZ", api_writable=True),
            AttributeColumn(attribute_id=season.id, source="omnitron",
                            column_name="attributes__PL_sezon", market=None, api_writable=True),
            OmnitronAttribute(
                pk=1, key="urun_adi_pl", name="x", data_type="text", is_localizable=True
            ),
            OmnitronAttribute(
                pk=2, key="urun_adi_cz", name="x", data_type="text", is_localizable=True
            ),
            OmnitronAttribute(
                pk=3, key="PL_sezon", name="x", data_type="text", is_localizable=False
            ),
        ])
        return {"name": name.id, "season": season.id}


def sizes(model_color: str, *labels: str) -> None:
    with session_scope() as db:
        for size in labels:
            db.add(Product(
                sku=f"{model_color}.{size}", base_code_format=f"{model_color}.{size}",
                model_color=model_color, base_code=model_color.split(".")[0], size=size,
            ))


def approved(
    attr_id: int, model_color: str, market: str, value: str, was: str | None = None
) -> None:
    """A value a person set — and, when `was` is given, what it replaced."""
    with session_scope() as db:
        db.add(AttributeValue(
            entity_kind="model_color", entity_key=model_color, attribute_id=attr_id,
            market=market, state="approved", entry_grain="model_color",
            source_kind="human", value=value, updated_by="p@example.com",
        ))
        if was is not None:
            db.add(ValueChange(
                entity_kind="model_color", entity_key=model_color, attribute_id=attr_id,
                market=market, old_value=was, new_value=value, changed_by="p@example.com",
            ))


def state_of(attr_id: int, model_color: str, market: str) -> str:
    with session_scope() as db:
        row = db.scalar(select(AttributeValue).filter_by(
            entity_key=model_color, attribute_id=attr_id, market=market,
        ))
        assert row is not None
        return row.state


def items_of(batch_id: int) -> list[PushItem]:
    with session_scope() as db:
        return list(
            db.scalars(
                select(PushItem).where(PushItem.batch_id == batch_id).order_by(PushItem.id)
            )
        )


def batch_row(batch_id: int) -> PushBatch:
    with session_scope() as db:
        row = db.get(PushBatch, batch_id)
        assert row is not None
        return row


def writes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_write_mode", "merge")


def a_run() -> SyncRun:
    return SyncRun(row_id=1, kind="omnitron_push")


def built(**kw) -> int:
    with session_scope() as db:
        return build_batch(db, created_by="p@example.com", source="ui", **kw).id


# ── building ───────────────────────────────────────────────────────────────


def test_a_batch_fans_one_option_value_out_to_every_size() -> None:
    ids = registry()
    sizes("L3558.132", "T2", "T3", "T4")
    approved(ids["name"], "L3558.132", "PL", "Koszulka")
    approved(ids["season"], "L3558.132", "*", "26AW")

    batch_id = built()
    items = items_of(batch_id)
    assert len(items) == 6
    assert {(i.sku, i.key, i.locale) for i in items} == {
        ("L3558.132.T2", "urun_adi_pl", "pl"), ("L3558.132.T3", "urun_adi_pl", "pl"),
        ("L3558.132.T4", "urun_adi_pl", "pl"), ("L3558.132.T2", "PL_sezon", None),
        ("L3558.132.T3", "PL_sezon", None), ("L3558.132.T4", "PL_sezon", None),
    }
    row = batch_row(batch_id)
    assert (row.item_count, row.product_count, row.sku_count, row.status) == (6, 1, 3, "pending")


def test_nothing_approved_means_nothing_to_push() -> None:
    registry()
    sizes("L3558.132", "T2")
    with session_scope() as db, pytest.raises(NothingToPush):
        build_batch(db, created_by=None, source="ui")


def test_a_second_open_batch_is_refused() -> None:
    ids = registry()
    sizes("L3558.132", "T2")
    approved(ids["name"], "L3558.132", "PL", "Koszulka")
    built()
    with session_scope() as db, pytest.raises(PushBusy):
        build_batch(db, created_by=None, source="ui")


def test_a_selection_past_the_cap_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(push, "MAX_ITEMS", 2)
    ids = registry()
    sizes("L3558.132", "T2", "T3", "T4")
    approved(ids["name"], "L3558.132", "PL", "Koszulka")
    with session_scope() as db, pytest.raises(TooManyItems, match="smaller"):
        build_batch(db, created_by=None, source="ui")


# ── refusing to start ──────────────────────────────────────────────────────


def test_a_push_refuses_while_writes_are_off_or_the_mode_is_unknown(monkeypatch) -> None:
    ids = registry()
    sizes("L3558.132", "T2")
    approved(ids["name"], "L3558.132", "PL", "Koszulka")
    batch_id = built()
    fake = FakeOmnitron()
    fake.add("L3558.132.T2", urun_adi_pl="Polo")

    monkeypatch.setattr(settings, "omnitron_writes_enabled", False)
    with pytest.raises(WritesNotEnabled, match="WRITES_ENABLED"):
        run_batch(client(fake), batch_id, a_run())
    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)
    monkeypatch.setattr(settings, "omnitron_write_mode", "unknown")
    with pytest.raises(WritesNotEnabled, match="WRITE_MODE"):
        run_batch(client(fake), batch_id, a_run())
    assert fake.posts == []
    assert batch_row(batch_id).status == "pending"


def test_a_push_waits_for_the_values_sync(monkeypatch) -> None:
    writes_on(monkeypatch)
    ids = registry()
    sizes("L3558.132", "T2")
    approved(ids["name"], "L3558.132", "PL", "Koszulka")
    batch_id = built()
    with session_scope() as db:
        db.add(SyncRunRow(kind="omnitron_values", status="running"))
    with pytest.raises(PushBusy, match="values sync"):
        run_batch(client(FakeOmnitron()), batch_id, a_run())


# ── writing ────────────────────────────────────────────────────────────────


def test_a_clean_push_writes_each_market_under_its_locale_and_goes_live(monkeypatch) -> None:
    writes_on(monkeypatch)
    ids = registry()
    sizes("L3558.132", "T2", "T3")
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo", was="Polo")
    approved(ids["name"], "L3558.132", "CZ", "Polokošile", was=None)
    approved(ids["season"], "L3558.132", "*", "26AW", was="OUTLET")
    fake = FakeOmnitron()
    for sku in ("L3558.132.T2", "L3558.132.T3"):
        fake.add(sku, urun_adi_pl="Polo", urun_adi_ru="Поло", PL_sezon="OUTLET",
                 integration_kalem_icerik="Türkçe açıklama")
        fake.store[sku]["pl"] = {"urun_adi_pl": "Polo"}

    batch_id = built()
    out = run_batch(client(fake), batch_id, a_run())

    assert out["status"] == "done", out
    assert (out["written"], out["confirmed"], out["failed"], out["skipped"]) == (6, 6, 0, 0)
    # One POST per (size, market): PL under `pl`, CZ under `cs`, the global key with no header.
    sent = sorted(
        ((loc, body["sku"], tuple(sorted(body["attributes"]))) for loc, body in fake.posts),
        key=lambda t: (t[0] or "", t[1], t[2]),
    )
    assert sent == [
        (None, "L3558.132.T2", ("PL_sezon",)), (None, "L3558.132.T3", ("PL_sezon",)),
        ("cs", "L3558.132.T2", ("urun_adi_cz",)), ("cs", "L3558.132.T3", ("urun_adi_cz",)),
        ("pl", "L3558.132.T2", ("urun_adi_pl",)), ("pl", "L3558.132.T3", ("urun_adi_pl",)),
    ]
    # The Turkish view, the Russian key and the shared description never moved.
    assert fake.store["L3558.132.T2"][None]["urun_adi_pl"] == "Polo"
    assert fake.store["L3558.132.T2"][None]["urun_adi_ru"] == "Поло"
    assert fake.store["L3558.132.T2"][None]["integration_kalem_icerik"] == "Türkçe açıklama"
    assert fake.store["L3558.132.T2"]["pl"]["urun_adi_pl"] == "Koszulka polo"
    assert state_of(ids["name"], "L3558.132", "PL") == "live"
    assert state_of(ids["name"], "L3558.132", "CZ") == "live"
    assert state_of(ids["season"], "L3558.132", "*") == "live"
    assert all(i.old_upstream in ("Polo", None, "OUTLET") for i in items_of(batch_id))


def test_a_value_changed_upstream_since_the_edit_is_skipped_not_overwritten(monkeypatch) -> None:
    writes_on(monkeypatch)
    ids = registry()
    sizes("L3558.132", "T2")
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo", was="Polo")
    fake = FakeOmnitron()
    fake.add("L3558.132.T2", urun_adi_pl="Polo")
    fake.store["L3558.132.T2"]["pl"] = {"urun_adi_pl": "Somebody else's text"}

    batch_id = built()
    out = run_batch(client(fake), batch_id, a_run())
    assert fake.posts == []
    assert out["status"] == "failed" and out["skipped"] == 1
    (item,) = items_of(batch_id)
    assert item.status == "skipped" and "changed upstream" in (item.reason or "")
    assert state_of(ids["name"], "L3558.132", "PL") == "approved"


def test_a_value_already_upstream_is_confirmed_without_a_write(monkeypatch) -> None:
    writes_on(monkeypatch)
    ids = registry()
    sizes("L3558.132", "T2")
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo", was="Polo")
    fake = FakeOmnitron()
    fake.add("L3558.132.T2", urun_adi_pl="Polo")
    fake.store["L3558.132.T2"]["pl"] = {"urun_adi_pl": "Koszulka polo"}

    batch_id = built()
    out = run_batch(client(fake), batch_id, a_run())
    assert fake.posts == []
    assert out["status"] == "done" and out["confirmed"] == 1
    assert state_of(ids["name"], "L3558.132", "PL") == "live"


def test_a_size_omnitron_does_not_return_is_never_posted(monkeypatch) -> None:
    writes_on(monkeypatch)
    ids = registry()
    sizes("L3558.132", "T2", "T9")
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo")
    fake = FakeOmnitron()
    fake.add("L3558.132.T2", urun_adi_pl="")

    batch_id = built()
    out = run_batch(client(fake), batch_id, a_run())
    assert [body["sku"] for _, body in fake.posts] == ["L3558.132.T2"]
    assert out["status"] == "partial" and out["skipped"] == 1 and out["confirmed"] == 1
    # Not every size confirmed, so the option's value stays exported.
    assert state_of(ids["name"], "L3558.132", "PL") == "exported"
    by_sku = {i.sku: i for i in items_of(batch_id)}
    assert by_sku["L3558.132.T9"].status == "skipped"
    assert "unknown product" in (by_sku["L3558.132.T9"].reason or "")


def test_a_failed_write_on_one_size_leaves_the_value_exported(monkeypatch) -> None:
    writes_on(monkeypatch)
    ids = registry()
    sizes("L3558.132", "T2", "T3")
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo")
    fake = FakeOmnitron(fail_skus={"L3558.132.T3"})
    fake.add("L3558.132.T2")
    fake.add("L3558.132.T3")

    batch_id = built()
    out = run_batch(client(fake), batch_id, a_run())
    assert out["status"] == "partial"
    assert (out["confirmed"], out["failed"]) == (1, 1)
    assert state_of(ids["name"], "L3558.132", "PL") == "exported"
    row = batch_row(batch_id)
    assert row.error_summary and "write failed" in row.error_summary


def test_a_write_that_moved_anything_else_halts_the_batch(monkeypatch) -> None:
    writes_on(monkeypatch)
    ids = registry()
    sizes("L3558.132", "T2")
    sizes("PH9619.166", "T3")
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo")
    approved(ids["name"], "PH9619.166", "PL", "Druga koszulka")
    fake = FakeOmnitron(leak="urun_adi_ru")
    fake.add("L3558.132.T2", urun_adi_ru="Поло")
    fake.add("PH9619.166.T3", urun_adi_ru="Поло")

    batch_id = built()
    out = run_batch(client(fake), batch_id, a_run())
    assert out["status"] == "halted"
    assert "urun_adi_ru changed under default" in (out["halted"] or "")
    row = batch_row(batch_id)
    assert row.status == "halted" and "urun_adi_ru" in (row.error_summary or "")
    statuses = {i.sku: i.status for i in items_of(batch_id)}
    assert statuses["L3558.132.T2"] == "failed"
    assert state_of(ids["name"], "L3558.132", "PL") == "exported"


def test_the_isolation_rule_on_its_own() -> None:
    before = {None: {"a": "1", "urun_adi_pl": "tr"}, "pl": {"a": "1", "urun_adi_pl": "old"},
              "tr-tr": {"a": "1", "urun_adi_pl": "tr"}}
    after_ok = {None: {"a": "1", "urun_adi_pl": "tr"}, "pl": {"a": "1", "urun_adi_pl": "new"},
                "tr-tr": {"a": "1", "urun_adi_pl": "tr"}}
    assert isolation_breaches(before, after_ok, {"urun_adi_pl": True}) == []

    turkish_moved = {**after_ok, None: {"a": "1", "urun_adi_pl": "new"}}
    assert isolation_breaches(before, turkish_moved, {"urun_adi_pl": True}) == [
        "urun_adi_pl (default copy) changed"
    ]
    other_moved = {**after_ok, "pl": {"a": "2", "urun_adi_pl": "new"}}
    assert isolation_breaches(before, other_moved, {"urun_adi_pl": True}) == ["a changed under pl"]
    # A plain key is one copy: it moves in every view by design, and that is fine.
    plain_before = {None: {"PL_sezon": "OUTLET"}, "tr-tr": {"PL_sezon": "OUTLET"}}
    plain_after = {None: {"PL_sezon": "26AW"}, "tr-tr": {"PL_sezon": "26AW"}}
    assert isolation_breaches(plain_before, plain_after, {"PL_sezon": False}) == []


# ── one value, every size ──────────────────────────────────────────────────


def test_a_value_reaches_every_size_whatever_its_stock() -> None:
    """One name set on the option is written to all nine sizes, including the
    ones on zero stock and the ones with no stock row at all.

    Stated as a requirement 2026-09-08 and pinned here because nothing in the
    push mentions stock — which is exactly the kind of correctness that is
    true by accident until someone adds a filter that looks sensible.
    """
    ids = registry()
    labels = [f"T{n}" for n in range(1, 10)]
    sizes("L3558.132", *labels)
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo")

    with session_scope() as db:
        stock = Attribute(
            key="stock", label="Stock", write_channel="read_only",
            value_grain="sku", per_market=False,
        )
        db.add(stock)
        db.flush()
        # Three sizes sold out, one never stocked at all, the rest with depth.
        for label, qty in zip(labels, [0, 0, 0, None, 5, 5, 5, 5, 5], strict=True):
            if qty is None:
                continue
            db.add(AttributeValue(
                entity_kind="sku", entity_key=f"L3558.132.{label}",
                attribute_id=stock.id, market="*", state="live",
                entry_grain="sku", source_kind="omnitron_api", value=str(qty),
            ))

    batch_id = built()
    items = items_of(batch_id)
    assert {i.sku for i in items} == {f"L3558.132.{label}" for label in labels}
    assert len(items) == 9
    assert batch_row(batch_id).sku_count == 9


def test_a_size_omnitron_has_dropped_is_not_written_to() -> None:
    """The one thing that does exclude a size: `delisted_at`, set only when
    `deleted_items` proves Omnitron no longer holds it. Writing there would
    upsert the SKU back into existence."""
    from pim_studio.models import utcnow as _utcnow

    ids = registry()
    sizes("L3558.132", "T2", "T3")
    approved(ids["name"], "L3558.132", "PL", "Koszulka polo")
    with session_scope() as db:
        gone = db.scalar(select(Product).where(Product.sku == "L3558.132.T3"))
        assert gone is not None
        gone.delisted_at = _utcnow()

    items = items_of(built())
    assert {i.sku for i in items} == {"L3558.132.T2"}
