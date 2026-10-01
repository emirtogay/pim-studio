"""Pushing approved values into Omnitron, one size at a time, and proving it.

A value a person set in the panel sits at `approved` until this carries it.
One value stored per option is written once per size. Three things make that
safe enough to run against the live catalogue, and each is enforced here
rather than hoped for:

**Only registry-named keys, under their market's locale.** `sync.targets`
decides what may be written and where; a payload is built from its targets
and nothing else, and the client's own guard
(`omnitron.client.patch_product`) checks the same set again. A Polish write
therefore carries `urun_adi_pl` under `Accept-Language: pl` and cannot carry
`urun_adi_ru`, the core `name`, or the shared description key every store
reads.

The write is `PATCH /api/v1/products/{pk}/`, addressed by Omnitron's own
product id taken from the read immediately before it. The integration upsert
`POST /api/i1/products/` cannot be used: it is a full upsert and rejects a
partial body, demanding `name`, `base_code`, `product_type` and
`attribute_set` (measured 2026-09-08). Sending `name` under a market locale
is how a Turkish master title becomes a market "translation", so the partial
PATCH is both the working door and the safer one — and being keyed on the pk
rather than the sku, a wrong identifier 404s instead of creating a product.

**Compare-and-swap.** Before writing, the size is read under its market's
locale and what Omnitron holds is compared with what the mirror last saw.
Omnitron holding something else means a person or an integration changed it
since; the item is skipped with the reason, never overwritten.

**Read-back, and a halt.** After writing, the size is read again under the
market's locale, the tenant default (no header) and `tr-tr`. The item is
confirmed only if the market copy now says what was sent *and* nothing that
was not sent changed in any view *and* the default and Turkish copies of a
localizable key did not move. A breach of the last two is an isolation
failure: the batch is marked `halted` and the run stops before touching
another product. A cross-store side effect is not something to log and carry
on past.

Only merge semantics are supported (`OMNITRON_WRITE_MODE=merge`): the body
carries the group's keys and nothing else. Echoing a locale view wholesale is
never done — an untranslated key echoes the Turkish baseline, and re-posting
that under `cs` would freeze Turkish in as a Czech translation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..logging import log
from ..models import (
    Attribute,
    AttributeValue,
    Product,
    PushBatch,
    PushItem,
    ValueChange,
    utcnow,
)
from ..models import SyncRun as SyncRunRow
from ..omnitron.client import OmnitronClient, OmnitronError
from .batching import chunked, fetch_by_sku_locales
from .runs import STATUS_RUNNING, SyncRun
from .targets import WriteTarget, keys_for_market, require_market_keys, write_targets
from .writer import upsert_attribute_values

#: The run kind a push records itself under on the Sync Runs page.
RUN_KIND = "omnitron_push"
#: The values sync must not be mid-read while a push writes: its snapshot
#: would then hold half of an edit and report the other half as divergence.
VALUES_KIND = "omnitron_values"

#: Items one batch may carry. A 500-option bulk edit across eight sizes and
#: four markets is 16,000 writes; that is several batches, deliberately, so a
#: halt loses minutes rather than an afternoon.
MAX_ITEMS = 2_000

#: SKUs per read. Matches `batching.SKU_BATCH_SIZE`.
_CHUNK = 100

#: The views every read-back includes besides the market's own: the tenant
#: default (no header) and the Turkish store's explicit locale. Both must be
#: byte-identical before and after for any localizable key we sent.
ISOLATION_LOCALES: tuple[str, ...] = ("tr-tr",)

BATCH_PENDING = "pending"
BATCH_PUSHING = "pushing"
BATCH_DONE = "done"
BATCH_PARTIAL = "partial"
BATCH_FAILED = "failed"
BATCH_HALTED = "halted"
OPEN_STATUSES = frozenset({BATCH_PENDING, BATCH_PUSHING})

ITEM_PENDING = "pending"
ITEM_WRITTEN = "written"
ITEM_CONFIRMED = "confirmed"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"


class PushError(RuntimeError):
    """A push could not be built or could not start. Nothing was written."""


class NothingToPush(PushError):
    """No approved value has a write target, or none has a sellable size."""


class TooManyItems(PushError):
    """The selection fans out past `MAX_ITEMS`; narrow it."""


class PushBusy(PushError):
    """Another batch is open, or the values sync is mid-read."""


class WritesNotEnabled(PushError):
    """The configuration does not allow a write yet."""


@dataclass(frozen=True)
class Group:
    """Everything one POST carries: one size, one market, its keys."""

    sku: str
    market: str
    locale: str | None
    items: tuple[PushItem, ...]

    @property
    def keys(self) -> dict[str, str]:
        return {i.key: i.new_value for i in self.items}


# ── building ───────────────────────────────────────────────────────────────


def _open_batch(db: Session) -> PushBatch | None:
    return db.scalar(
        select(PushBatch).where(PushBatch.status.in_(OPEN_STATUSES)).order_by(PushBatch.id)
    )


#: One cell, as a person picks it on the Imports page: option, attribute key,
#: market. Scoping a push by ModelColor alone cannot express "this product's
#: name but not its detail", which is the selection a first careful push wants.
CellKey = tuple[str, str, str]


def approved_values(
    db: Session,
    targets: dict[tuple[int, str], WriteTarget],
    model_colors: list[str] | None = None,
    only: set[CellKey] | None = None,
) -> list[AttributeValue]:
    """The option-grain values waiting to go out, in a stable order.

    `only` narrows to an exact set of cells. An empty set is not the same as
    `None`: `None` means "everything", an empty set means "nothing was picked"
    and yields nothing, so a UI that sends a selection can never accidentally
    push the whole queue by sending it empty.
    """
    query = (
        select(AttributeValue)
        .where(AttributeValue.state_lane == "current")
        .where(AttributeValue.state == "approved")
        .where(AttributeValue.entity_kind == "model_color")
        .where(AttributeValue.attribute_id.in_({a for a, _ in targets}))
        .order_by(AttributeValue.entity_key, AttributeValue.attribute_id, AttributeValue.market)
    )
    if model_colors:
        query = query.where(AttributeValue.entity_key.in_(list(model_colors)))
    keys = {a.id: a.key for a in db.scalars(select(Attribute))}
    return [
        row
        for row in db.scalars(query)
        if (row.attribute_id, row.market) in targets
        and row.value
        and (
            only is None
            or (row.entity_key, keys.get(row.attribute_id, ""), row.market) in only
        )
    ]


def sizes_of(db: Session, model_colors: set[str]) -> dict[str, list[str]]:
    """Sellable sizes per option — the SKUs a write is keyed on."""
    out: dict[str, list[str]] = defaultdict(list)
    for sku, mc in db.execute(
        select(Product.sku, Product.model_color)
        .where(Product.model_color.in_(list(model_colors)))
        .where(Product.is_size())
        .order_by(Product.sku)
    ):
        out[mc].append(sku)
    return out


def build_batch(
    db: Session,
    *,
    created_by: str | None,
    source: str,
    model_colors: list[str] | None = None,
    only: set[CellKey] | None = None,
    note: str | None = None,
) -> PushBatch:
    """Turn approved values into a batch of per-size items. Writes nothing.

    One open batch at a time: two built side by side would carry the same
    values twice, and the second would then be skipped item by item as
    "already upstream" at best.
    """
    open_batch = _open_batch(db)
    if open_batch is not None:
        raise PushBusy(
            f"Batch #{open_batch.id} is {open_batch.status}; finish or fail it first."
        )
    targets = write_targets(db)
    if not targets:
        raise NothingToPush("No Omnitron column is marked writable in the registry.")
    rows = approved_values(db, targets, model_colors, only)
    if not rows:
        raise NothingToPush(
            "None of the selected values is approved and waiting to go out."
            if only is not None
            else "Nothing is approved and waiting to go out."
        )

    sizes = sizes_of(db, {row.entity_key for row in rows})
    items: list[PushItem] = []
    sizeless = 0
    for row in rows:
        target = targets[(row.attribute_id, row.market)]
        skus = sizes.get(row.entity_key, [])
        if not skus:
            sizeless += 1
            continue
        for sku in skus:
            items.append(
                PushItem(
                    model_color=row.entity_key,
                    sku=sku,
                    attribute_id=row.attribute_id,
                    market=row.market,
                    locale=target.locale,
                    key=target.key,
                    new_value=row.value,
                    status=ITEM_PENDING,
                )
            )
    if not items:
        raise NothingToPush(
            f"{sizeless} approved value(s) belong to options with no sellable size."
        )
    if len(items) > MAX_ITEMS:
        raise TooManyItems(
            f"{len(items)} writes across {len({i.sku for i in items})} sizes; a batch "
            f"carries at most {MAX_ITEMS}. Push a smaller selection."
        )

    batch = PushBatch(
        created_by=created_by,
        source=source,
        status=BATCH_PENDING,
        note=note,
        item_count=len(items),
        product_count=len({i.model_color for i in items}),
        sku_count=len({i.sku for i in items}),
    )
    db.add(batch)
    db.flush()
    for item in items:
        item.batch_id = batch.id
    db.add_all(items)
    db.flush()
    if sizeless:
        log.info("push_batch_sizeless_values", batch_id=batch.id, values=sizeless)
    log.info(
        "push_batch_built",
        batch_id=batch.id, items=len(items), products=batch.product_count,
        skus=batch.sku_count, source=source, by=created_by,
    )
    return batch


def request_estimate(batch: PushBatch, items: list[PushItem]) -> int:
    """Roughly how many calls the batch will make, for the confirm dialog."""
    groups = len({(i.sku, i.market) for i in items})
    chunks = -(-len({i.sku for i in items}) // _CHUNK)
    locales = len({i.locale for i in items if i.locale}) + 1 + len(ISOLATION_LOCALES)
    return groups + 2 * chunks * locales


# ── running ────────────────────────────────────────────────────────────────


def _attributes(record: dict[str, Any] | None) -> dict[str, str | None]:
    """A record's flat attributes, as strings, for comparing views."""
    if not record:
        return {}
    out: dict[str, str | None] = {}
    for key, value in (record.get("attributes") or {}).items():
        out[key] = None if value is None else str(value).strip() or None
    return out


def isolation_breaches(
    before: dict[str | None, dict[str, str | None]],
    after: dict[str | None, dict[str, str | None]],
    sent: dict[str, bool],
) -> list[str]:
    """What moved that should not have. Empty means the write was contained.

    `before` and `after` map a view (locale, or None for the default) to the
    size's attributes in that view; `sent` maps each key written to whether
    it is localizable. Two rules:

    - a key that was not sent must read the same in every view; and
    - a localizable key that was sent must read the same in the default and
      Turkish views, because the write was meant for one market's copy only.

    Pure, so the rule is testable without a transport.
    """
    breaches: list[str] = []
    for view in before:
        pre, post = before[view], after.get(view, {})
        label = view or "default"
        for key in sorted(set(pre) | set(post)):
            if key not in sent:
                if pre.get(key) != post.get(key):
                    breaches.append(f"{key} changed under {label}")
            elif (
                sent[key]
                and view in (None, *ISOLATION_LOCALES)
                and pre.get(key) != post.get(key)
            ):
                breaches.append(f"{key} ({label} copy) changed")
    return breaches


def _previous_upstream(
    db: Session, items: list[PushItem]
) -> dict[tuple[str, int, str], str | None]:
    """What the mirror held before the person's edit, per option value.

    The current-lane row now carries the new value, so the old one is in
    `value_changes` — the latest change to that key. A key with no change row
    was empty before, which is what `None` means here.
    """
    keys = {(i.model_color, i.attribute_id, i.market) for i in items}
    previous: dict[tuple[str, int, str], str | None] = dict.fromkeys(keys)
    rows = db.execute(
        select(
            ValueChange.entity_key, ValueChange.attribute_id, ValueChange.market,
            ValueChange.old_value,
        )
        .where(ValueChange.entity_kind == "model_color")
        .where(ValueChange.entity_key.in_({k[0] for k in keys}))
        .order_by(ValueChange.changed_at.desc(), ValueChange.id.desc())
    ).all()
    seen: set[tuple[str, int, str]] = set()
    for entity_key, attribute_id, market, old in rows:
        key = (entity_key, attribute_id, market)
        if key in keys and key not in seen:
            seen.add(key)
            previous[key] = old
    return previous


def _value_row(db: Session, item: PushItem) -> AttributeValue | None:
    return db.scalar(
        select(AttributeValue).filter_by(
            entity_kind="model_color",
            entity_key=item.model_color,
            attribute_id=item.attribute_id,
            market=item.market,
            state_lane="current",
        )
    )


def _hop(db: Session, row: AttributeValue, state: str, changed_by: str | None) -> None:
    """Move a value's lifecycle state through the writer, value unchanged."""
    if row.state == state:
        return
    record = {
        "entity_kind": row.entity_kind,
        "entity_key": row.entity_key,
        "attribute_id": row.attribute_id,
        "market": row.market,
        "state_lane": row.state_lane,
        "value": row.value,
        "state": state,
        "entry_grain": row.entry_grain,
        "source_kind": row.source_kind,
        "suspect_flag": row.suspect_flag,
        "suspect_reason": row.suspect_reason,
    }
    upsert_attribute_values(db, [record], protect=False, changed_by=changed_by)


def _groups(items: list[PushItem], targets: dict[tuple[int, str], WriteTarget]) -> list[Group]:
    by_group: dict[tuple[str, str], list[PushItem]] = defaultdict(list)
    for item in items:
        by_group[(item.sku, item.market)].append(item)
    groups: list[Group] = []
    for (sku, market), members in sorted(by_group.items()):
        locale = require_market_keys(targets, market, [m.key for m in members])
        groups.append(Group(sku=sku, market=market, locale=locale, items=tuple(members)))
    return groups


def _read_views(
    api: OmnitronClient, skus: list[str], locales: list[str]
) -> tuple[dict[str, dict[str | None, dict[str, str | None]]], dict[str, int]]:
    """Every SKU's attributes under every view, and its Omnitron product id.

    The id comes from this read and nowhere else. It is what the write is
    addressed to, so taking it from the record we just looked at — rather
    than from our own mirror, which can be a day stale — means a write can
    only ever land on a product this pass actually saw.
    """
    out: dict[str, dict[str | None, dict[str, str | None]]] = {}
    pks: dict[str, int] = {}
    for views in fetch_by_sku_locales(api, skus, [None, *locales], batch_size=_CHUNK):
        for sku, record in views[None].found.items():
            out[sku] = {
                loc: _attributes(view.found.get(sku)) for loc, view in views.items()
            }
            pk = record.get("pk")
            if isinstance(pk, int):
                pks[sku] = pk
    return out, pks


def _finish_item(item: PushItem, status: str, reason: str | None = None) -> None:
    item.status = status
    item.reason = reason
    if status == ITEM_CONFIRMED:
        item.confirmed_at = utcnow()


def run_batch(api: OmnitronClient, batch_id: int, run: SyncRun) -> dict[str, Any]:
    """Write a pending batch and confirm it, size by size.

    Refuses before touching anything if writes are not enabled, the write
    mode is not `merge`, or the values sync is mid-read. Commits after every
    group so a crash leaves honest item rows rather than a batch that claims
    nothing happened.
    """
    if not settings.omnitron_writes_enabled:
        raise WritesNotEnabled(
            "OMNITRON_WRITES_ENABLED is not set. Run scripts/probe_omnitron_write.py "
            "first, then switch writes on deliberately."
        )
    if settings.omnitron_write_mode != "merge":
        raise WritesNotEnabled(
            f"OMNITRON_WRITE_MODE is {settings.omnitron_write_mode!r}; only `merge` "
            "is supported, and it is set once the probe has shown the upsert "
            "touches the keys it names and nothing else."
        )

    with session_scope() as db:
        if db.scalar(
            select(SyncRunRow.id)
            .where(SyncRunRow.kind == VALUES_KIND)
            .where(SyncRunRow.status == STATUS_RUNNING)
        ):
            raise PushBusy("The values sync is running; push after it finishes.")
        batch = db.get(PushBatch, batch_id)
        if batch is None:
            raise PushError(f"No batch #{batch_id}.")
        if batch.status != BATCH_PENDING:
            raise PushError(f"Batch #{batch_id} is {batch.status}, not pending.")
        batch.status = BATCH_PUSHING
        batch.started_at = utcnow()
        batch.sync_run_id = run.id
        created_by = batch.created_by
        targets = write_targets(db)
        items = list(
            db.scalars(
                select(PushItem)
                .where(PushItem.batch_id == batch_id)
                .where(PushItem.status == ITEM_PENDING)
                .order_by(PushItem.sku, PushItem.market, PushItem.id)
            )
        )
        previous = _previous_upstream(db, items)
        groups = _groups(items, targets)
        db.commit()

    run.seen(len(items))
    reasons: Counter[str] = Counter()
    halted: str | None = None
    written = confirmed = failed = skipped = 0
    localizable_keys = {t.key for t in targets.values() if t.localizable}
    locales = sorted({g.locale for g in groups if g.locale} | set(ISOLATION_LOCALES))

    for chunk in chunked([g.sku for g in groups], _CHUNK):
        if halted:
            break
        chunk_skus = sorted(set(chunk))
        chunk_groups = [g for g in groups if g.sku in set(chunk_skus)]
        before, pks = _read_views(api, chunk_skus, locales)
        sent_by_sku: dict[str, dict[str, bool]] = defaultdict(dict)

        with session_scope() as db:
            for group in chunk_groups:
                views = before.get(group.sku)
                if views is None:
                    for item in group.items:
                        _finish_item(
                            item, ITEM_SKIPPED,
                            "unknown product: Omnitron did not return this SKU",
                        )
                        db.add(item)
                    skipped += len(group.items)
                    reasons["unknown product"] += len(group.items)
                    db.commit()
                    continue

                to_send: dict[str, str] = {}
                for item in group.items:
                    db.add(item)
                    view = views.get(group.locale) if group.locale else views.get(None)
                    current = (view or {}).get(item.key)
                    item.old_upstream = current
                    if current is not None and current == item.new_value.strip():
                        _finish_item(item, ITEM_CONFIRMED, "already upstream")
                        confirmed += 1
                        continue
                    expected = previous.get((item.model_color, item.attribute_id, item.market))
                    expected = expected.strip() if isinstance(expected, str) else expected
                    if current is not None and current != (expected or None):
                        _finish_item(
                            item, ITEM_SKIPPED,
                            f"changed upstream: Omnitron holds {current[:80]!r}, "
                            f"the mirror last saw {(expected or '')[:80]!r}",
                        )
                        skipped += 1
                        reasons["changed upstream"] += 1
                        continue
                    to_send[item.key] = item.new_value

                pending_items = [i for i in group.items if i.status == ITEM_PENDING]
                if not to_send:
                    for item in pending_items:
                        _finish_item(item, ITEM_SKIPPED, "nothing to send")
                    db.commit()
                    continue

                pk = pks.get(group.sku)
                if pk is None:
                    for item in group.items:
                        if item.status == ITEM_PENDING:
                            _finish_item(
                                item, ITEM_SKIPPED,
                                "no Omnitron product id on the record we just read",
                            )
                            skipped += 1
                            reasons["no product id"] += 1
                    db.commit()
                    continue
                try:
                    api.patch_product(
                        pk,
                        to_send,
                        allowed_keys=set(keys_for_market(targets, group.market)),
                        locale=group.locale,
                        localizable_keys=localizable_keys,
                    )
                except OmnitronError as exc:
                    for item in pending_items:
                        _finish_item(item, ITEM_FAILED, f"write failed: {exc}"[:500])
                    failed += len(pending_items)
                    reasons["write failed"] += len(pending_items)
                    db.commit()
                    continue

                now = utcnow()
                for item in pending_items:
                    item.status = ITEM_WRITTEN
                    item.written_at = now
                    written += 1
                    sent_by_sku[group.sku][item.key] = item.key in localizable_keys
                    row = _value_row(db, item)
                    if row is not None and row.state == "approved":
                        _hop(db, row, "exported", created_by)
                db.commit()

        # Read back the whole chunk once, then judge each written group.
        written_skus = sorted(sent_by_sku)
        if not written_skus:
            continue
        after, _ = _read_views(api, written_skus, locales)
        with session_scope() as db:
            for group in chunk_groups:
                members = [i for i in group.items if i.status == ITEM_WRITTEN]
                if not members:
                    continue
                for item in members:
                    db.add(item)
                pre = before.get(group.sku, {})
                post = after.get(group.sku)
                if post is None:
                    for item in members:
                        _finish_item(item, ITEM_FAILED, "not landed: SKU absent on read-back")
                    failed += len(members)
                    reasons["not landed"] += len(members)
                    db.commit()
                    continue
                breaches = isolation_breaches(pre, post, sent_by_sku[group.sku])
                if breaches:
                    halted = f"{group.sku}: " + "; ".join(breaches[:5])
                    for item in members:
                        _finish_item(item, ITEM_FAILED, f"isolation: {halted}"[:500])
                    failed += len(members)
                    reasons["isolation"] += len(members)
                    db.commit()
                    log.error("push_halted", batch_id=batch_id, sku=group.sku, breaches=breaches)
                    break
                view = post.get(group.locale) if group.locale else post.get(None)
                for item in members:
                    landed = (view or {}).get(item.key) == item.new_value.strip()
                    if landed:
                        _finish_item(item, ITEM_CONFIRMED)
                        confirmed += 1
                    else:
                        _finish_item(
                            item, ITEM_FAILED, "not landed: read-back does not show the value"
                        )
                        failed += 1
                        reasons["not landed"] += 1
                db.commit()

    # Promote a value to live only when every size confirmed it.
    with session_scope() as db:
        batch = db.get(PushBatch, batch_id)
        assert batch is not None
        all_items = list(db.scalars(select(PushItem).where(PushItem.batch_id == batch_id)))
        by_value: dict[tuple[str, int, str], list[PushItem]] = defaultdict(list)
        for item in all_items:
            by_value[(item.model_color, item.attribute_id, item.market)].append(item)
        promoted = 0
        for members in by_value.values():
            if all(m.status == ITEM_CONFIRMED for m in members):
                row = _value_row(db, members[0])
                if row is not None and row.state in ("approved", "exported"):
                    _hop(db, row, "live", created_by)
                    promoted += 1

        batch.written_count = written
        batch.confirmed_count = confirmed
        batch.failed_count = failed
        batch.skipped_count = skipped
        batch.finished_at = utcnow()
        if halted:
            batch.status = BATCH_HALTED
            batch.error_summary = f"halted: {halted}"
        elif failed == 0 and skipped == 0:
            batch.status = BATCH_DONE
        elif confirmed == 0:
            batch.status = BATCH_FAILED
            batch.error_summary = ", ".join(f"{k}: {n}" for k, n in reasons.most_common())
        else:
            batch.status = BATCH_PARTIAL
            batch.error_summary = ", ".join(f"{k}: {n}" for k, n in reasons.most_common())
        status = batch.status

    run.imported(confirmed)
    for reason, count in reasons.items():
        run.dropped(count, reason=reason)
    log.info(
        "push_finished",
        batch_id=batch_id, status=status, written=written, confirmed=confirmed,
        failed=failed, skipped=skipped, promoted=promoted, halted=halted,
    )
    return {
        "batch_id": batch_id,
        "status": status,
        "items": len(items),
        "written": written,
        "confirmed": confirmed,
        "failed": failed,
        "skipped": skipped,
        "promoted": promoted,
        "halted": halted,
        "reasons": dict(reasons),
    }
