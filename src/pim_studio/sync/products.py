"""Refreshing our product rows against the live API.

Scope is our own 26,813 SKUs, never the tenant's 288,990 — the user's call,
and the practical one: `sku__in` batches of 100 make this ~270 requests. The
rows themselves came from a Check_list export loaded 2026-08-13; this stage
gives each one its Omnitron identity (`omnitron_pk`, `is_active`,
`modified_date`) and a fresh `last_seen_at`.

Two deliberate non-actions:

**Every row is read, including the product_meta rows minted before
`sync.catalogue` learned to skip them.** This is the stage that stamps
`omnitron_product_type`, so it has to see them; `Product.is_size()` is what
keeps them out of the counts afterwards.

**A SKU the API cannot find is counted, not delisted.** Absence from a batched
read can be a partial failure on their side or a scope quirk on ours, and
neither should quietly retire a product. Deletion has an authoritative source —
`deleted_items` — and `delist_deleted` below is the only thing allowed to set
`delisted_at`.

**A base_code disagreement is reported, not overwritten.** Ours derives from
the export's MCS string, theirs from their own product record. If they differ,
one of the two derivations is wrong, and papering over it with an UPDATE would
hide the only evidence.

`deleted_items` gives back `object_id` — their pk, not a SKU — which is why the
refresh stores `omnitron_pk`: without it a deletion could not be matched to a
row without asking the API who it was.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..db import session_scope
from ..logging import log
from ..models import Product, utcnow
from ..omnitron.client import OmnitronClient
from .batching import fetch_by_sku
from .registry import _as_utc, _parse_dt
from .runs import SyncRun, last_successful_run

#: Our SKUs per read batch — see `batching.SKU_BATCH_SIZE` for provenance.
_BATCH = 100


def refresh_products(api: OmnitronClient, run: SyncRun) -> dict[str, int]:
    """One pass over every non-delisted SKU we hold.

    Delisted rows are skipped: a product `deleted_items` retired stays retired
    even if something with the same SKU answers again — resurrecting it is a
    decision for a human with the evidence in front of them, not a side effect
    of a nightly read.
    """
    with session_scope() as db:
        skus = list(
            db.scalars(
                select(Product.sku)
                .where(Product.delisted_at.is_(None))
                .order_by(Product.sku)
            )
        )

    found = missing = updated = 0
    base_code_disagreements = 0

    for batch in fetch_by_sku(api, skus, batch_size=_BATCH):
        run.seen(batch.requested)
        now = utcnow()

        with session_scope() as db:
            rows = {
                p.sku: p
                for p in db.scalars(
                    select(Product).where(Product.sku.in_(list(batch.found)))
                )
            }
            for sku, record in batch.found.items():
                row = rows.get(sku)
                if row is None:  # pragma: no cover — sku vanished mid-run
                    continue
                changed = _apply(row, record, now)
                found += 1
                if changed:
                    updated += 1
                theirs = (record.get("base_code") or "").strip().upper()
                if theirs and theirs != row.base_code:
                    base_code_disagreements += 1
                    log.warning(
                        "base_code_disagreement",
                        sku=sku, ours=row.base_code, theirs=theirs,
                    )

        if batch.missing:
            missing += len(batch.missing)
            run.dropped(len(batch.missing), reason="sku not found in Omnitron")

    run.imported(found)
    stats = {
        "skus": len(skus),
        "found": found,
        "missing": missing,
        "updated": updated,
        "base_code_disagreements": base_code_disagreements,
    }
    log.info("products_refreshed", **stats)
    return stats


def _apply(row: Product, record: dict[str, Any], now: datetime) -> bool:
    """Write the API's identity fields onto our row. True if anything changed.

    `last_seen_at` moves on every pass by definition — it records the look,
    not a change — so it is excluded from the changed test for the same reason
    `source_ref` is in `writer.py`.
    """
    row.last_seen_at = now

    pk = record.get("pk")
    is_active = record.get("is_active")
    # The one field here that changes what a row *counts as* rather than what
    # it says about itself. Rows minted before `catalogue` learned to skip
    # product_meta are still in the table, and this pass is what tells them
    # apart: it reads every SKU we hold, so one full refresh stamps the lot.
    product_type = record.get("product_type")
    modified = _parse_dt(record.get("modified_date"))

    changed = (
        row.omnitron_pk != pk
        or row.omnitron_is_active != is_active
        or row.omnitron_product_type != product_type
        or _ne_dt(row.omnitron_modified_date, modified)
    )
    row.omnitron_pk = pk
    row.omnitron_is_active = is_active
    row.omnitron_product_type = product_type
    row.omnitron_modified_date = modified
    return changed


def _ne_dt(ours: datetime | None, theirs: datetime | None) -> bool:
    """Datetime inequality that survives the SQLite naive round-trip."""
    if ours is None or theirs is None:
        return ours is not theirs
    if ours.tzinfo is None:
        ours = ours.replace(tzinfo=theirs.tzinfo) if theirs.tzinfo else ours
    if theirs.tzinfo is None and ours.tzinfo is not None:
        theirs = theirs.replace(tzinfo=ours.tzinfo)
    return ours != theirs


def delist_deleted(api: OmnitronClient, run: SyncRun) -> dict[str, int]:
    """Retire rows the tenant's own deletion feed says are gone.

    `GET /api/v1/deleted_items/?content_type_model=product` is the one source
    that means deleted rather than merely absent. Windowed by the last
    successful run of this kind so the feed is a delta; the first run reads the
    whole history, which is correct — a deletion from before we synced still
    applies to a row loaded from an old export.

    Deletions for products we never held are normal (most of the tenant is not
    ours) and are counted as seen, not as drops.
    """
    watermark = last_successful_run(run.kind)
    params: dict[str, Any] = {"content_type_model": "product"}
    if watermark is not None:
        # Explicit UTC. The value is stored UTC either way, but SQLite hands it
        # back naive — formatting without the Z would leave the server free to
        # read it in its own timezone and quietly shift the window.
        params["deleted_date__gte"] = _as_utc(watermark).strftime("%Y-%m-%dT%H:%M:%SZ")

    deleted: dict[int, datetime | None] = {}
    for record in api.iter_records("/api/v1/deleted_items/", params=params):
        run.seen()
        object_id = record.get("object_id")
        if object_id is None:
            run.dropped(reason="deleted_items row with no object_id")
            continue
        deleted[int(object_id)] = _parse_dt(record.get("deleted_date"))

    delisted = already = 0
    if deleted:
        with session_scope() as db:
            rows = db.scalars(
                select(Product).where(Product.omnitron_pk.in_(list(deleted)))
            ).all()
            for row in rows:
                if row.delisted_at is not None:
                    already += 1
                    continue
                row.delisted_at = deleted.get(row.omnitron_pk or -1) or utcnow()
                delisted += 1
                run.imported()
                log.info(
                    "product_delisted",
                    sku=row.sku, model_color=row.model_color,
                    omnitron_pk=row.omnitron_pk,
                )

    stats = {
        "deletions_in_feed": len(deleted),
        "ours_delisted": delisted,
        "ours_already_delisted": already,
    }
    log.info("delist_pass", **stats)
    return stats
