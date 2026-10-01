"""Values a person sets in the panel, and what the database currently says.

The product page is served from the nightly extract, deliberately, so that the
page and the coverage numbers cannot disagree. That rule has one cost: a value
somebody sets this afternoon is invisible until tonight's rebuild, and a page
that swallows an edit looks broken. So the page reads a second, small payload
straight from the database — the current values of one option — and lays it
over the extract before rendering. It adds no numbers for the extract to
disagree with; it only says what one product's cells hold right now and who
last changed them.

Writing goes through `sync.writer.upsert_attribute_values` like every other
write to `attribute_values`, so a change leaves a `value_changes` row with the
person's name on it. `protect=False` is deliberate: the writer's protection
exists so the *sync* cannot overwrite a human decision, and here the human is
the one writing.

What may be written is not decided here. `sync.targets.write_targets` reads it
from the registry, and an (attribute, market) it does not list is refused with
the reason — a partner-owned field, a tenant-wide one, a read-only derivative.
"""

from __future__ import annotations

import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import integrations, locks
from ..config import settings
from ..db import get_session, session_scope
from ..locks import Advisory
from ..logging import log
from ..models import (
    MARKET_AGNOSTIC,
    Attribute,
    AttributeValue,
    Product,
    PushBatch,
    PushItem,
    StorefrontListing,
    User,
    ValueChange,
    VocabularyOption,
)
from ..omnitron.client import OmnitronClient, OmnitronError
from ..reports.values_upload import CellEdit, parse_values_upload
from ..rules.language import FLAG_THRESHOLD, english_confidence
from ..sync import proposals, push
from ..sync.runs import sync_run
from ..sync.targets import WriteTarget, write_targets
from ..sync.writer import upsert_attribute_values
from . import audit
from .auth import ROLE_DEVELOPER, ROLE_PLANNING, current_user, require_role

router = APIRouter(prefix="/api/imports", tags=["imports"])
#: The overlay lives under the product's own URL, beside its history.
values_router = APIRouter(prefix="/api/products", tags=["imports"])

Viewer = Annotated[User, Depends(current_user)]
Actor = Annotated[User, Depends(require_role(ROLE_DEVELOPER, ROLE_PLANNING))]
Db = Annotated[Session, Depends(get_session)]

#: How a value written from the panel is labelled in `source_kind`. The sync
#: writes `omnitron_api`; a conflict decision and a panel edit are both a
#: person, and the page shows them the same way.
SOURCE_HUMAN = "human"

#: A value longer than this is not a product attribute, it is a paste gone
#: wrong. Descriptions run to ~1,700 characters in the mirror.
MAX_VALUE_CHARS = 20_000


class SetValueBody(BaseModel):
    modelColor: str = Field(min_length=1, max_length=32)
    attribute: str = Field(min_length=1, max_length=64)
    #: PL|CZ|SK|HU, or `*` / `shared` for a market-agnostic attribute.
    market: str = Field(min_length=1, max_length=8)
    value: str = Field(max_length=MAX_VALUE_CHARS)


def _market(raw: str) -> str:
    return MARKET_AGNOSTIC if raw in (MARKET_AGNOSTIC, "shared") else raw


def _target_or_refuse(
    db: Session, attribute: Attribute, market: str
) -> WriteTarget:
    targets = write_targets(db)
    target = targets.get((attribute.id, market))
    if target is not None:
        return target
    # Say which of the three reasons applies; "not allowed" sends somebody to
    # look for a permission that does not exist.
    if attribute.write_channel == "product_card":
        why = "it belongs to the Turkey team and reaches Omnitron on a product card"
    elif attribute.write_channel == "read_only":
        why = "it is derived or system-owned"
    elif any(t.attribute_id == attribute.id for t in targets.values()):
        why = f"it has no write target for market {market}"
    else:
        why = "no Omnitron column is marked writable for it"
    raise HTTPException(
        422,
        f"{attribute.label} cannot be set from the panel: {why}.",
    )


def _row_payload(row: AttributeValue, attribute_key: str) -> dict[str, Any]:
    return {
        "attribute": attribute_key,
        "market": row.market,
        "value": row.value,
        "state": row.state,
        "sourceKind": row.source_kind,
        "sourceRef": row.source_ref,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
        "updatedBy": row.updated_by,
        "suspect": row.suspect_flag,
        "suspectReason": row.suspect_reason,
    }


def _overlay(db: Session, model_color: str) -> dict[str, Any]:
    rows = db.execute(
        select(AttributeValue, Attribute.key)
        .join(Attribute, Attribute.id == AttributeValue.attribute_id)
        .where(AttributeValue.entity_kind == "model_color")
        .where(AttributeValue.entity_key == model_color)
        .where(AttributeValue.state_lane == "current")
        .order_by(Attribute.key, AttributeValue.market)
    ).all()
    # The proposal lane, beside the live value rather than over it. A cell
    # with one draws the accept/edit/reject actions; without one it is an
    # ordinary editable cell.
    proposed = db.execute(
        select(AttributeValue, Attribute.key)
        .join(Attribute, Attribute.id == AttributeValue.attribute_id)
        .where(AttributeValue.entity_kind == "model_color")
        .where(AttributeValue.entity_key == model_color)
        .where(AttributeValue.state_lane == proposals.PROPOSAL_LANE)
        .order_by(Attribute.key, AttributeValue.market)
    ).all()
    targets = write_targets(db)
    keys = {a.id: a.key for a in db.scalars(select(Attribute))}
    return {
        "modelColor": model_color,
        "rows": [_row_payload(row, key) for row, key in rows],
        "proposals": [
            {
                "attribute": key,
                "market": row.market,
                "value": _shown(row.value),
                "why": row.suspect_reason,
                "flag": row.suspect_flag,
                "source": (row.source_ref or "").split(":")[0] or None,
                "proposedAt": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row, key in proposed
        ],
        "writable": [
            {"attribute": keys[t.attribute_id], "market": t.market}
            for t in targets.values()
            if t.attribute_id in keys
        ],
        "writesEnabled": bool(settings.omnitron_writes_enabled),
    }


@values_router.get("/{model_color}/values")
def product_values(model_color: str, user: Viewer, db: Db) -> dict[str, Any]:
    """The option's current values, live from the database.

    Every current-lane row at option grain, whatever its state — the page
    decides how to draw `live` against `approved`; this only reports. The
    `writable` list is the registry's answer to "which cells may be edited",
    so the page never has to guess it from the owner.
    """
    del user
    return _overlay(db, model_color)


@router.post("/values")
def set_value(
    body: SetValueBody, request: Request, user: Actor, db: Db
) -> dict[str, Any]:
    """Set one cell of one option, as `approved`.

    Approved, not live: nothing has reached Omnitron yet. The sync will not
    overwrite it (`writer.PROTECTED_STATES`), a push will carry it, and a
    read-back will move it to `live`.
    """
    attribute = db.scalar(select(Attribute).where(Attribute.key == body.attribute))
    if attribute is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown attribute {body.attribute}")
    market = _market(body.market)
    _target_or_refuse(db, attribute, market)
    if attribute.value_grain != "model_color":
        raise HTTPException(
            422,
            f"{attribute.label} is held per {attribute.value_grain}, not per option.",
        )

    value = body.value.strip()
    if not value:
        raise HTTPException(
            422,
            "Value cannot be empty. Clearing a value in Omnitron is not something "
            "this panel does.",
        )
    if db.scalar(select(Product.id).where(Product.model_color == body.modelColor)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No product {body.modelColor}")

    result = upsert_attribute_values(
        db,
        [
            {
                "entity_kind": "model_color",
                "entity_key": body.modelColor,
                "attribute_id": attribute.id,
                "market": market,
                "state_lane": "current",
                "value": value,
                "state": "approved",
                "entry_grain": "model_color",
                "source_kind": SOURCE_HUMAN,
                "source_ref": f"panel:{user.email}",
                "updated_by": user.email,
            }
        ],
        protect=False,
        changed_by=user.email,
    )
    db.commit()

    audit.mark_recorded(request)
    audit.record(
        action=audit.SET_VALUE,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path="/api/imports/values",
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"{body.modelColor} {attribute.key} [{market}] = {value[:120]}",
    )

    row = db.scalar(
        select(AttributeValue).filter_by(
            entity_kind="model_color",
            entity_key=body.modelColor,
            attribute_id=attribute.id,
            market=market,
            state_lane="current",
        )
    )
    assert row is not None
    return {
        "ok": True,
        "written": result.written,
        "unchanged": result.unchanged,
        "row": _row_payload(row, attribute.key),
    }


# ── pushing ────────────────────────────────────────────────────────────────
#
# A push runs in a thread of this process, like an image publish: the request
# builds the batch, proves Omnitron is reachable, takes the cross-process lock
# and returns; the thread does the writing under a `sync_runs` row. `_pushing`
# covers this process, `locks.OMNITRON_PUSH` covers the host's CLI, and the
# page polls the batch to watch it finish.

_pushing = threading.Lock()

#: How much of a value the lists carry. The full text is on the product.
_SHOWN = 160


def _shown(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "…"


def _push_readiness() -> tuple[bool, str]:
    """Whether a push could start here, and if not, the sentence to show."""
    if not settings.omnitron_writes_enabled:
        return False, (
            "Writes to Omnitron are switched off on this server "
            "(OMNITRON_WRITES_ENABLED). They are switched on deliberately, after "
            "the write probe has been run on one product."
        )
    if settings.omnitron_write_mode != "merge":
        return False, (
            f"OMNITRON_WRITE_MODE is {settings.omnitron_write_mode!r}. It is set to "
            "`merge` once the probe has shown the upsert touches only the keys it "
            "names; until then nothing is written."
        )
    if not (
        settings.omnitron_base_url and settings.omnitron_username and settings.omnitron_password
    ):
        return False, (
            "The panel has no Omnitron credentials, so a push has to run from the "
            "host: `python -m pim_studio.sync push`."
        )
    return True, ""


#: Statuses whose item is still waiting on the shop. A `failed` or `skipped`
#: item was never written, so it is not owed a publication and does not count
#: against either number below.
_AWAITING_SHOP = ("written", "confirmed")


def _live_options(db: Session, model_colors: set[str]) -> set[tuple[str, str]]:
    """`(model_color, market)` pairs the storefront sweep currently sees.

    `sync.publication` confirms by crawling each market's listing feed, so an
    option that is not in that feed cannot be confirmed by any number of
    passes. This is the same table the sweep writes, read back to say which
    of those two a zero is.
    """
    if not model_colors:
        return set()
    rows = db.execute(
        select(StorefrontListing.model_color, StorefrontListing.market).where(
            StorefrontListing.model_color.in_(model_colors),
            StorefrontListing.left_at.is_(None),
        )
    ).all()
    return {(mc, market) for mc, market in rows}


def _is_listed(item: PushItem, live: set[tuple[str, str]]) -> bool:
    """Can the shop check reach this item at all?

    A `*` item is one copy every market serves, so any market still listing
    the option can confirm it — which is exactly how `confirm_published`
    now asks.
    """
    if item.market == MARKET_AGNOSTIC:
        return any(mc == item.model_color for mc, _ in live)
    return (item.model_color, item.market) in live


def _unreachable(db: Session, items: list[PushItem]) -> set[int]:
    """Ids of written items the publication crawl can never match.

    Not a failure: the value is in Omnitron and the product page may well be
    showing it. It is only that the listing feed — the one source that check
    reads — no longer carries the option, so the wait is not a wait.
    """
    waiting = [
        i for i in items if i.published_at is None and i.status in _AWAITING_SHOP
    ]
    if not waiting:
        return set()
    live = _live_options(db, {i.model_color for i in waiting})
    return {i.id for i in waiting if not _is_listed(i, live)}


def _batch_payload(batch: PushBatch, published: int = 0, unlisted: int = 0) -> dict[str, Any]:
    """`published` is counted from the items, not stored on the batch.

    A batch finishes when Omnitron confirms it, which is seconds after the
    write; the shop follows hours later and item by item. So this number keeps
    changing after the batch says `done`, and storing it on the row would mean
    a counter nothing updates.

    `unlisted` is the part of the gap that is not a wait. Without it the panel
    shows one number for two situations — the crawl has not run since the
    push, and the option is out of the listing the crawl reads — and a batch
    in the second reads as a batch that failed to land.
    """
    return {
        "id": batch.id,
        "createdAt": batch.created_at.isoformat() if batch.created_at else None,
        "createdBy": batch.created_by,
        "source": batch.source,
        "status": batch.status,
        "note": batch.note,
        "itemCount": batch.item_count,
        "productCount": batch.product_count,
        "skuCount": batch.sku_count,
        "writtenCount": batch.written_count,
        "confirmedCount": batch.confirmed_count,
        #: Items the market's own shop has been seen serving. `confirmedCount`
        #: is Omnitron agreeing; this is a customer being able to read it.
        "publishedCount": published,
        #: Of the rest, how many are out of their market's listing feed, so no
        #: pass of the shop check can ever see them.
        "unlistedCount": unlisted,
        "failedCount": batch.failed_count,
        "skippedCount": batch.skipped_count,
        "syncRunId": batch.sync_run_id,
        "startedAt": batch.started_at.isoformat() if batch.started_at else None,
        "finishedAt": batch.finished_at.isoformat() if batch.finished_at else None,
        "errorSummary": batch.error_summary,
    }


def _value_rows(
    rows: list[AttributeValue], labels: dict[int, tuple[str, str]], sizes: dict[str, list[str]]
) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        key, label = labels.get(row.attribute_id, ("?", "?"))
        out.append(
            {
                "modelColor": row.entity_key,
                "attribute": key,
                "label": label,
                "market": row.market,
                "value": _shown(row.value),
                "state": row.state,
                "updatedBy": row.updated_by,
                "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
                "sizes": len(sizes.get(row.entity_key, [])),
            }
        )
    return out


@router.get("/pending")
def pending(user: Viewer, db: Db) -> dict[str, Any]:
    """What is waiting to go out, what went out unconfirmed, and whether a
    push can start from here.

    `approved` is the work queue; `exported` is the "published but not
    landed" queue the vault has always asked for — values a batch wrote whose
    read-back never confirmed them.
    """
    del user
    targets = write_targets(db)
    labels = {a.id: (a.key, a.label) for a in db.scalars(select(Attribute))}
    approved = push.approved_values(db, targets)
    exported = [
        row
        for row in db.scalars(
            select(AttributeValue)
            .where(AttributeValue.state_lane == "current")
            .where(AttributeValue.state == "exported")
            .where(AttributeValue.entity_kind == "model_color")
            .order_by(AttributeValue.updated_at.desc())
        )
        if (row.attribute_id, row.market) in targets
    ]
    sizes = push.sizes_of(db, {r.entity_key for r in approved} | {r.entity_key for r in exported})
    # Transient items, never added to the session: the same fan-out the batch
    # would do, so the page quotes the number the confirm dialog will quote.
    preview = [
        PushItem(
            model_color=r.entity_key, sku=sku, attribute_id=r.attribute_id, market=r.market,
            locale=targets[(r.attribute_id, r.market)].locale,
            key=targets[(r.attribute_id, r.market)].key, new_value=r.value or "",
        )
        for r in approved
        for sku in sizes.get(r.entity_key, [])
    ]
    open_batch = push._open_batch(db)
    ready, reason = _push_readiness()
    if ready and open_batch is not None:
        ready, reason = False, f"Batch #{open_batch.id} is {open_batch.status}."
    if ready and not preview:
        ready, reason = False, "Nothing is approved and waiting to go out."
    return {
        "approved": _value_rows(approved, labels, sizes),
        "approvedCount": len(approved),
        "itemCount": len(preview),
        "productCount": len({i.model_color for i in preview}),
        "skuCount": len({i.sku for i in preview}),
        "requestEstimate": push.request_estimate(PushBatch(), preview) if preview else 0,
        "maxItems": push.MAX_ITEMS,
        "exported": _value_rows(exported, labels, sizes),
        "openBatch": _batch_payload(open_batch) if open_batch else None,
        "canPush": ready,
        "reason": reason,
        "writesEnabled": bool(settings.omnitron_writes_enabled),
        "writeMode": settings.omnitron_write_mode,
    }


class CellRef(BaseModel):
    modelColor: str = Field(min_length=1, max_length=32)
    attribute: str = Field(min_length=1, max_length=64)
    market: str = Field(min_length=1, max_length=8)


class PushBody(BaseModel):
    #: Empty means everything approved.
    modelColors: list[str] = Field(default_factory=list, max_length=2000)
    #: An exact set of cells. Sent by the page when the person has unticked
    #: anything; absent when they have not. `None` and `[]` mean different
    #: things here — see `push.approved_values`.
    values: list[CellRef] | None = Field(default=None, max_length=5000)
    note: str | None = Field(default=None, max_length=255)


def _fail_batch(batch_id: int, why: str) -> None:
    with session_scope() as db:
        batch = db.get(PushBatch, batch_id)
        if batch is not None and batch.status in push.OPEN_STATUSES:
            batch.status = push.BATCH_FAILED
            batch.error_summary = why[:2000]


def _push_in_background(
    batch_id: int, actor: str, api: OmnitronClient, lock: Advisory
) -> None:
    """The run itself. Its own session, because the request's is long gone."""
    if not _pushing.acquire(blocking=False):
        log.warning("omnitron_push_already_running", batch_id=batch_id)
        _fail_batch(batch_id, "another push was already running in this process")
        lock.release()
        api.close()
        return
    try:
        with sync_run(push.RUN_KIND, triggered_by=actor) as run:
            push.run_batch(api, batch_id, run)
    except push.PushError as exc:
        log.error("omnitron_push_refused", batch_id=batch_id, error=str(exc))
        _fail_batch(batch_id, str(exc))
    except Exception as exc:  # noqa: BLE001 — a background thread must not die silently
        log.error("omnitron_push_crashed", batch_id=batch_id, error=f"{type(exc).__name__}: {exc}")
        _fail_batch(batch_id, f"{type(exc).__name__}: {exc}")
    finally:
        api.close()
        _pushing.release()
        lock.release()


@router.post("/push", status_code=status.HTTP_202_ACCEPTED)
def start_push(body: PushBody, request: Request, user: Actor, db: Db) -> dict[str, Any]:
    """Build a batch from approved values, prove Omnitron answers, and start.

    Everything that can be refused is refused before the thread exists: the
    configuration, another run, an empty or oversized selection, and the
    network. A batch that cannot start is marked failed with the reason rather
    than left pending, because a pending batch blocks the next one.
    """
    ready, reason = _push_readiness()
    if not ready:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)
    if _pushing.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "a push is already running")
    try:
        batch = push.build_batch(
            db, created_by=user.email, source="ui",
            model_colors=body.modelColors or None,
            only=(
                None
                if body.values is None
                else {(v.modelColor, v.attribute, _market(v.market)) for v in body.values}
            ),
            note=body.note,
        )
    except push.PushBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (push.NothingToPush, push.TooManyItems) as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    batch_id, item_count, sku_count, product_count = (
        batch.id, batch.item_count, batch.sku_count, batch.product_count,
    )
    first_sku = db.scalar(
        select(PushItem.sku).where(PushItem.batch_id == batch_id).order_by(PushItem.id)
    )

    # Prove Omnitron answers before spawning the thread: a run that fails in
    # the background leaves only a red row minutes later, for somebody who
    # has already walked away. One read now buys a sentence on screen.
    try:
        api = integrations.pim_client(real=OmnitronClient)
        api.get("/api/v1/products/", params={"sku__in": first_sku, "limit": 1})
    except OmnitronError as exc:
        _fail_batch(batch_id, f"preflight: {exc}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Omnitron did not answer: {exc}"
        ) from exc

    lock = Advisory(locks.OMNITRON_PUSH)
    if not lock.acquire():
        api.close()
        _fail_batch(batch_id, "a push is already running on this server")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a push is already running on this server — from this panel or from "
            "the host. Wait for it to finish.",
        )
    try:
        threading.Thread(
            target=_push_in_background,
            args=(batch_id, user.email, api, lock),
            name="omnitron-push",
            daemon=True,
        ).start()
    except BaseException:
        lock.release()
        api.close()
        raise

    audit.mark_recorded(request)
    audit.record(
        action=audit.STARTED_PUSH,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path="/api/imports/push",
        status_code=202,
        ip=audit.client_ip(request),
        detail=f"batch #{batch_id}: {item_count} writes across {sku_count} sizes",
    )
    return {
        "started": True,
        "batchId": batch_id,
        "items": item_count,
        "skus": sku_count,
        "products": product_count,
    }


@router.get("/batches")
def batches(
    user: Viewer, db: Db, limit: Annotated[int, Query(ge=1, le=200)] = 20
) -> dict[str, Any]:
    del user
    rows = db.scalars(select(PushBatch).order_by(PushBatch.id.desc()).limit(limit)).all()
    ids = [b.id for b in rows]
    published = dict(
        db.execute(
            select(PushItem.batch_id, func.count(PushItem.id))
            .where(
                PushItem.batch_id.in_(ids),
                PushItem.published_at.is_not(None),
            )
            .group_by(PushItem.batch_id)
        ).all()
    )
    # Only the items still waiting are loaded, which on a healthy panel is a
    # few hundred rows across every batch on the page — the published ones are
    # counted in SQL above and never come back over the wire.
    waiting = db.scalars(
        select(PushItem).where(
            PushItem.batch_id.in_(ids),
            PushItem.published_at.is_(None),
            PushItem.status.in_(_AWAITING_SHOP),
        )
    ).all()
    live = _live_options(db, {i.model_color for i in waiting})
    unlisted: dict[int, int] = {}
    for item in waiting:
        if not _is_listed(item, live):
            unlisted[item.batch_id] = unlisted.get(item.batch_id, 0) + 1
    return {
        "rows": [
            _batch_payload(b, published.get(b.id, 0), unlisted.get(b.id, 0)) for b in rows
        ]
    }


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: int, user: Viewer, db: Db) -> dict[str, Any]:
    del user
    batch = db.get(PushBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No batch #{batch_id}")
    labels = {a.id: a.key for a in db.scalars(select(Attribute))}
    items = db.scalars(
        select(PushItem).where(PushItem.batch_id == batch_id).order_by(PushItem.id)
    ).all()
    unreachable = _unreachable(db, list(items))
    return {
        **_batch_payload(
            batch,
            sum(1 for i in items if i.published_at is not None),
            len(unreachable),
        ),
        "items": [
            {
                "sku": i.sku,
                "modelColor": i.model_color,
                "attribute": labels.get(i.attribute_id, "?"),
                "market": i.market,
                "locale": i.locale,
                "key": i.key,
                "oldUpstream": _shown(i.old_upstream),
                "newValue": _shown(i.new_value),
                "status": i.status,
                "reason": i.reason,
                "writtenAt": i.written_at.isoformat() if i.written_at else None,
                "confirmedAt": i.confirmed_at.isoformat() if i.confirmed_at else None,
                "publishedAt": i.published_at.isoformat() if i.published_at else None,
                #: True where the wait is a real wait. False says the option is
                #: out of its market's listing feed, so this row will never go
                #: `live` however long it is left — which is a different thing
                #: to tell somebody than "not yet".
                "unlisted": i.id in unreachable,
            }
            for i in items
        ],
    }


# ── many values at once: preview, then apply ───────────────────────────────
#
# A bulk edit from the grid and an edited spreadsheet arrive as the same
# thing: a list of (option, attribute, market, value). Both go through one
# preview that says, per cell, what it would change and what is wrong with it,
# and the person unticks what they do not want before anything is written.
# The preview is the review; nothing is written by the preview.

#: The largest list one request may carry. An edited SKU Detail sheet of
#: 2,000 sizes across four markets is a few thousand cells; anything bigger
#: is two uploads.
MAX_EDITS = 5_000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

FLAG_NOT_WRITABLE = "not_writable"
FLAG_UNKNOWN_ATTRIBUTE = "unknown_attribute"
FLAG_UNKNOWN_PRODUCT = "unknown_product"
FLAG_UNCHANGED = "unchanged"
FLAG_EMPTY = "empty"
FLAG_LANGUAGE = "language_suspect"
FLAG_VOCABULARY = "vocabulary_miss"
FLAG_SIZES_DISAGREE = "sizes_disagree"
#: A row carrying any of these is not applied, whatever the person ticks.
BLOCKING = frozenset(
    {FLAG_NOT_WRITABLE, FLAG_UNKNOWN_ATTRIBUTE, FLAG_UNKNOWN_PRODUCT, FLAG_UNCHANGED, FLAG_EMPTY}
)


class EditBody(BaseModel):
    modelColor: str = Field(min_length=1, max_length=32)
    attribute: str = Field(min_length=1, max_length=64)
    market: str = Field(min_length=1, max_length=8)
    value: str = Field(max_length=MAX_VALUE_CHARS)


class EditsBody(BaseModel):
    edits: list[EditBody] = Field(max_length=MAX_EDITS)


def _preview(db: Session, edits: list[CellEdit]) -> dict[str, Any]:
    """Every proposed cell, with what it would replace and what is wrong."""
    attributes = {a.key: a for a in db.scalars(select(Attribute))}
    targets = write_targets(db)
    model_colors = {e.model_color for e in edits}
    known = set(
        db.scalars(select(Product.model_color).where(Product.model_color.in_(model_colors)))
    ) if model_colors else set()
    attribute_ids = {attributes[e.attribute].id for e in edits if e.attribute in attributes}
    current: dict[tuple[str, int, str], AttributeValue] = {}
    if model_colors and attribute_ids:
        for row in db.scalars(
            select(AttributeValue)
            .where(AttributeValue.state_lane == "current")
            .where(AttributeValue.entity_kind == "model_color")
            .where(AttributeValue.entity_key.in_(model_colors))
            .where(AttributeValue.attribute_id.in_(attribute_ids))
        ):
            current[(row.entity_key, row.attribute_id, row.market)] = row
    vocabulary: dict[int, set[str]] = {}
    for option in db.scalars(
        select(VocabularyOption).where(VocabularyOption.status == "approved")
    ):
        vocabulary.setdefault(option.attribute_id, set()).add(option.value.lower())

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {"applicable": 0}
    for edit in edits:
        flags: list[str] = []
        value = edit.value.strip()
        attribute = attributes.get(edit.attribute)
        market = _market(edit.market)
        before: AttributeValue | None = None
        if attribute is None:
            flags.append(FLAG_UNKNOWN_ATTRIBUTE)
        else:
            if (attribute.id, market) not in targets:
                flags.append(FLAG_NOT_WRITABLE)
            before = current.get((edit.model_color, attribute.id, market))
        if edit.model_color not in known:
            flags.append(FLAG_UNKNOWN_PRODUCT)
        if not value:
            flags.append(FLAG_EMPTY)
        elif before is not None and (before.value or "").strip() == value:
            flags.append(FLAG_UNCHANGED)
        if attribute is not None and value:
            allowed = vocabulary.get(attribute.id)
            if allowed and value.lower() not in allowed:
                flags.append(FLAG_VOCABULARY)
            if (
                market != MARKET_AGNOSTIC
                and attribute.data_type in ("text", "html")
                and english_confidence(value) >= FLAG_THRESHOLD
            ):
                flags.append(FLAG_LANGUAGE)
        applicable = not any(f in BLOCKING for f in flags)
        if applicable:
            counts["applicable"] += 1
        for f in flags:
            counts[f] = counts.get(f, 0) + 1
        rows.append(
            {
                "modelColor": edit.model_color,
                "attribute": edit.attribute,
                "label": attribute.label if attribute else edit.attribute,
                "market": market,
                "current": _shown(before.value if before else None),
                "currentState": before.state if before else None,
                "value": value,
                "flags": flags,
                "applicable": applicable,
            }
        )
    return {"rows": rows, "counts": counts, "total": len(rows)}


def _edits(body: EditsBody) -> list[CellEdit]:
    return [CellEdit(e.modelColor, e.attribute, _market(e.market), e.value) for e in body.edits]


@router.get("/targets")
def targets(user: Viewer, db: Db) -> dict[str, Any]:
    """The attributes a person may set, for the grid's bulk edit to offer."""
    del user
    found = write_targets(db)
    attributes = {a.id: a for a in db.scalars(select(Attribute))}
    by_attribute: dict[int, list[str]] = {}
    for (attribute_id, market) in found:
        by_attribute.setdefault(attribute_id, []).append(market)
    return {
        "attributes": [
            {
                "key": attributes[aid].key,
                "label": attributes[aid].label,
                "perMarket": bool(attributes[aid].per_market),
                "longText": bool(attributes[aid].long_text),
                "markets": sorted(markets),
            }
            for aid, markets in by_attribute.items()
            if aid in attributes
        ],
    }


@router.post("/preview")
def preview(body: EditsBody, user: Viewer, db: Db) -> dict[str, Any]:
    del user
    return _preview(db, _edits(body))


@router.post("/apply")
def apply(body: EditsBody, request: Request, user: Actor, db: Db) -> dict[str, Any]:
    """Write the applicable rows as `approved`. Re-checked here, not trusted
    from the page: the preview is a courtesy, this is the gate."""
    edits = _edits(body)
    checked = _preview(db, edits)
    attributes = {a.key: a for a in db.scalars(select(Attribute))}
    records = []
    for edit, row in zip(edits, checked["rows"], strict=True):
        if not row["applicable"]:
            continue
        records.append(
            {
                "entity_kind": "model_color",
                "entity_key": edit.model_color,
                "attribute_id": attributes[edit.attribute].id,
                "market": edit.market,
                "state_lane": "current",
                "value": edit.value.strip(),
                "state": "approved",
                "entry_grain": "model_color",
                "source_kind": SOURCE_HUMAN,
                "source_ref": f"panel:{user.email}",
                "updated_by": user.email,
            }
        )
    result = upsert_attribute_values(db, records, protect=False, changed_by=user.email)
    db.commit()
    audit.mark_recorded(request)
    audit.record(
        action=audit.APPLIED_VALUES,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path="/api/imports/apply",
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"{result.written} of {len(edits)} values",
    )
    return {
        "ok": True,
        "applied": result.written,
        "unchanged": result.unchanged,
        "skipped": len(edits) - len(records),
    }


@router.post("/upload")
async def upload(
    user: Actor,
    db: Db,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    """An edited Values or SKU Detail sheet, turned into a preview.

    Nothing is written. The page shows every cell the file would change, with
    its flags, and `apply` is a second request the person makes.
    """
    del user
    if file is None or not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload a .xlsx or .csv file.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"{file.filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    registry = {a.key: (a.label, bool(a.per_market)) for a in db.scalars(select(Attribute))}
    try:
        parsed = await run_in_threadpool(parse_values_upload, file.filename, data, registry)
    except Exception as exc:  # noqa: BLE001 — any parser failure is the user's file
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Could not read {file.filename}: {exc}"
        ) from exc
    if parsed.problem and not parsed.edits:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, parsed.problem)
    previewed = _preview(db, parsed.edits)
    for model_color, attribute, market in parsed.sizes_disagree:
        previewed["rows"].append(
            {
                "modelColor": model_color,
                "attribute": attribute,
                "label": attribute,
                "market": market,
                "current": None,
                "currentState": None,
                "value": "",
                "flags": [FLAG_SIZES_DISAGREE],
                "applicable": False,
            }
        )
        previewed["counts"][FLAG_SIZES_DISAGREE] = (
            previewed["counts"].get(FLAG_SIZES_DISAGREE, 0) + 1
        )
    previewed["total"] = len(previewed["rows"])
    previewed["file"] = {
        "name": file.filename,
        "sheet": parsed.sheet,
        "headerRow": parsed.header_row,
        "rowsRead": parsed.rows_read,
        "columns": parsed.columns,
        "unknownColumns": parsed.unknown_columns,
    }
    return previewed


@router.post("/discard")
def discard(body: CellRef, request: Request, user: Actor, db: Db) -> dict[str, Any]:
    """Take one approved value out of the queue by deleting it.

    Deleted rather than flagged, which is the same call `ValueRetirement` made
    and for the same reason: the model's invariant is that an absent row *is*
    the absence, so a "discarded" flag would have to be remembered by every
    read and the one that forgot would push it anyway. The discard is recorded
    in `value_changes` instead, with the person's name and the value that went.

    The cell then reads as missing until the next values sync, which restores
    whatever Omnitron actually holds. That is the honest intermediate state:
    we stopped asserting a value we were not going to send.

    Only `approved` can be discarded. A `live` value is the sync's to manage,
    and an `exported` one has already been written to Omnitron — dropping the
    record of that would lose the only trace that it went out.
    """
    attribute = db.scalar(select(Attribute).where(Attribute.key == body.attribute))
    if attribute is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown attribute {body.attribute}")
    market = _market(body.market)
    row = db.scalar(
        select(AttributeValue).filter_by(
            entity_kind="model_color",
            entity_key=body.modelColor,
            attribute_id=attribute.id,
            market=market,
            state_lane="current",
        )
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No value held for {body.modelColor} {body.attribute} [{market}].",
        )
    if row.state != "approved":
        raise HTTPException(
            422,
            f"That value is {row.state}, not approved. Only a value waiting to go "
            "out can be discarded; a live one belongs to the sync and an exported "
            "one has already been written to Omnitron.",
        )

    gone = row.value
    db.add(
        ValueChange(
            entity_kind=row.entity_kind,
            entity_key=row.entity_key,
            attribute_id=row.attribute_id,
            market=row.market,
            old_value=gone,
            new_value=None,
            changed_by=user.email,
        )
    )
    db.delete(row)
    db.commit()

    audit.mark_recorded(request)
    audit.record(
        action=audit.DISCARDED_VALUE,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path="/api/imports/discard",
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"{body.modelColor} {attribute.key} [{market}] = {(gone or '')[:120]}",
    )
    return {"ok": True, "discarded": gone}


# ── proposed translations ──────────────────────────────────────────────────
#
# A proposal sits in its own lane beside the live value and is never sent
# anywhere on its own. Accepting copies it into the `current` lane as
# `approved` — the same state a hand-typed value gets, joining the same push
# queue through the same door — and removes the proposal. Rejecting removes it
# and leaves the live value alone. Either way the lane ends up empty, because
# a proposal somebody has ruled on is not a proposal any more.


class AcceptProposalBody(CellRef):
    #: The accepted text. Absent means "as proposed"; present means the person
    #: edited it first, which is the `Edit and accept` button and the reason
    #: this is not just a cell reference.
    value: str | None = Field(default=None, max_length=MAX_VALUE_CHARS)


def _proposal_or_404(db: Session, model_color: str, attribute: Attribute, market: str):
    row = db.scalar(
        select(AttributeValue).filter_by(
            entity_kind="model_color",
            entity_key=model_color,
            attribute_id=attribute.id,
            market=market,
            state_lane=proposals.PROPOSAL_LANE,
        )
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No proposal waiting for {model_color} {attribute.key} [{market}].",
        )
    return row


@router.get("/proposals")
def proposals_waiting(
    user: Viewer,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    market: str | None = None,
    attribute: str | None = None,
) -> dict[str, Any]:
    """Every waiting suggestion, with the value it would replace.

    Built because the feature shipped without one and was therefore unusable:
    a proposal was only visible if you already had the right product open, and
    there are thousands of them across a hundred thousand options.

    `total` follows the filters — it is what you are looking at. The `by*`
    counts do not: they describe everything waiting, so choosing a market does
    not collapse the very breakdown you would use to choose the next one.

    The live value is fetched alongside — a suggestion without the text it
    replaces is not reviewable, and that comparison is the entire decision.
    """
    del user
    labels = {a.id: (a.key, a.label) for a in db.scalars(select(Attribute))}
    by_key = {key: aid for aid, (key, _label) in labels.items()}

    # Everything waiting, once. There are thousands of these at most, and the
    # facets have to be counted over the unfiltered set anyway.
    everything = db.scalars(
        select(AttributeValue)
        .where(
            AttributeValue.state_lane == proposals.PROPOSAL_LANE,
            AttributeValue.entity_kind == "model_color",
        )
        .order_by(AttributeValue.updated_at.desc())
    ).all()

    wanted_market = _market(market) if market else None
    wanted_attribute = by_key.get(attribute, -1) if attribute else None
    every = [
        r
        for r in everything
        if (wanted_market is None or r.market == wanted_market)
        and (wanted_attribute is None or r.attribute_id == wanted_attribute)
    ]
    page = every[:limit]

    # One read for the live values behind this page, keyed the same way.
    wanted = {(r.entity_key, r.attribute_id, r.market) for r in page}
    live: dict[tuple[str, int, str], AttributeValue] = {}
    if wanted:
        for row in db.scalars(
            select(AttributeValue).where(
                AttributeValue.state_lane == "current",
                AttributeValue.entity_kind == "model_color",
                AttributeValue.entity_key.in_({k for k, _a, _m in wanted}),
                AttributeValue.attribute_id.in_({a for _k, a, _m in wanted}),
            )
        ):
            live[(row.entity_key, row.attribute_id, row.market)] = row

    def payload(row: AttributeValue) -> dict[str, Any]:
        key, label = labels.get(row.attribute_id, ("?", "?"))
        current = live.get((row.entity_key, row.attribute_id, row.market))
        return {
            "modelColor": row.entity_key,
            "attribute": key,
            "label": label,
            "market": row.market,
            "current": _shown(current.value) if current else None,
            "proposed": _shown(row.value),
            "why": row.suspect_reason,
            "flag": row.suspect_flag,
            "source": (row.source_ref or "").split(":")[0] or None,
            "proposedAt": row.updated_at.isoformat() if row.updated_at else None,
        }

    counts_market: dict[str, int] = {}
    counts_attribute: dict[str, int] = {}
    counts_flag: dict[str, int] = {}
    for row in everything:
        counts_market[row.market] = counts_market.get(row.market, 0) + 1
        key, _label = labels.get(row.attribute_id, ("?", "?"))
        counts_attribute[key] = counts_attribute.get(key, 0) + 1
        if row.suspect_flag:
            counts_flag[row.suspect_flag] = counts_flag.get(row.suspect_flag, 0) + 1

    return {
        "rows": [payload(r) for r in page],
        "total": len(every),
        "shown": len(page),
        "byMarket": counts_market,
        "byAttribute": counts_attribute,
        "byFlag": counts_flag,
    }


@router.post("/proposals/accept")
def accept_proposal(
    body: AcceptProposalBody, request: Request, user: Actor, db: Db
) -> dict[str, Any]:
    """Take a proposed translation into the queue, as the person's own value.

    It becomes `approved`, not `live`: accepting a suggestion is the same act
    as typing one, and it goes out on the next push with the same preview and
    the same read-back. `changed_by` is the person, not the source — they are
    answering for the text now.
    """
    attribute = db.scalar(select(Attribute).where(Attribute.key == body.attribute))
    if attribute is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown attribute {body.attribute}")
    market = _market(body.market)
    _target_or_refuse(db, attribute, market)
    row = _proposal_or_404(db, body.modelColor, attribute, market)

    text = (body.value if body.value is not None else row.value or "").strip()
    if not text:
        raise HTTPException(422, "An accepted proposal cannot be empty.")

    upsert_attribute_values(
        db,
        [{
            "entity_kind": "model_color",
            "entity_key": body.modelColor,
            "attribute_id": attribute.id,
            "market": market,
            "state_lane": "current",
            "value": text,
            "state": "approved",
            "entry_grain": "model_color",
            "source_kind": SOURCE_HUMAN,
            # Both halves matter later: where the words came from, and who
            # answered for them. A machine wrote it; a person let it out.
            "source_ref": f"{row.source_ref or 'proposal'}|accepted:{user.email}",
            "updated_by": user.email,
        }],
        changed_by=user.email,
    )
    db.delete(row)
    db.commit()

    audit.mark_recorded(request)
    audit.record(
        action=audit.ACCEPTED_PROPOSAL,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path="/api/imports/proposals/accept",
        status_code=200,
        ip=audit.client_ip(request),
        detail=(
            f"{body.modelColor} {attribute.key} [{market}] = {text[:120]}"
            + ("" if body.value is None else " (edited before accepting)")
        ),
    )
    return {"ok": True, "value": text, "edited": body.value is not None}


@router.post("/proposals/reject")
def reject_proposal(
    body: CellRef, request: Request, user: Actor, db: Db
) -> dict[str, Any]:
    """Drop a proposal and leave the live value as it is.

    No record is kept in `value_changes`: nothing changed. The proposal was
    never asserted about the product, and a rejected suggestion is not part of
    the value's history. The audit log carries who rejected what.

    The next `proposals` run may offer the same text again — the pass has no
    memory of a refusal. If that becomes tiresome it wants a decision about
    remembering rejections, not a quiet filter here.
    """
    attribute = db.scalar(select(Attribute).where(Attribute.key == body.attribute))
    if attribute is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown attribute {body.attribute}")
    market = _market(body.market)
    row = _proposal_or_404(db, body.modelColor, attribute, market)

    gone = row.value
    db.delete(row)
    db.commit()

    audit.mark_recorded(request)
    audit.record(
        action=audit.REJECTED_PROPOSAL,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path="/api/imports/proposals/reject",
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"{body.modelColor} {attribute.key} [{market}] = {(gone or '')[:120]}",
    )
    return {"ok": True, "rejected": gone}
