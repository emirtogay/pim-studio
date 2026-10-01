"""Price, stock and images — the inputs that decide whether a product is live.

These are not attributes on the product object, they are membership facts about
a catalogue, which is why the value importer skipped them and why they need
their own stage.

The cost of this stage is entirely a question of filters, and the filters are
honoured:

    product_stock?stock_list=2                224,055 rows
    product_stock?stock_list=2&stock__gt=0     16,766 rows

Reading only the rows with stock is not an optimisation, it is the rule:
liveness rule 2 is "stock above 1", so `stock__gt` *is* the rule evaluated by
Omnitron instead of by us. A product absent from that result has no stock,
which is exactly what we would have concluded after reading all 224,055.

`listable_products_only` was tried as a shortcut and rejected — it returns
`product_type=1` (product_meta), one listing entry per style-colour, 19,632 of
them, and cannot be scoped per market. It answers "what appears on a listing
page", not "what is sellable in Poland".
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update

from ..db import session_scope
from ..logging import log
from ..models import (
    Attribute,
    AttributeValue,
    Product,
    PublishedImage,
    ValueRetirement,
    utcnow,
)
from ..omnitron.client import OmnitronClient
from .batching import chunked
from .catalogue import _as_utc_param
from .runs import SyncMode, SyncRun
from .scope import MARKETS
from .writer import upsert_attribute_values

#: The attributes this stage owns end to end. The retirement sweep deletes only
#: these, and only ones it wrote itself, so a human-entered or masterdata value
#: can never be removed by a sync that simply did not read it.
_OWNED_KEYS = ("price", "retail_price", "stock")

#: Rows per DELETE. Same reasoning as `writer._LOOKUP_CHUNK`: one bind variable
#: per id, kept well under SQLite's older 999-parameter ceiling.
_DELETE_CHUNK = 500

#: Products per image lookup. `product__in` accepts the same comma-joined form
#: as `sku__in`; a list silently degrades.
_IMAGE_BATCH = 50


def _attribute_ids() -> dict[str, int]:
    with session_scope() as db:
        return {
            row[0]: row[1]
            for row in db.execute(select(Attribute.key, Attribute.id)).all()
        }


def _pk_to_sku() -> dict[int, str]:
    with session_scope() as db:
        return {
            row[0]: row[1]
            for row in db.execute(
                select(Product.omnitron_pk, Product.sku).where(
                    Product.omnitron_pk.isnot(None), Product.is_size()
                )
            )
        }


def sync_liveness(
    api: OmnitronClient, run: SyncRun, mode: SyncMode | None = None
) -> dict[str, int]:
    """Read price and stock per market and write them as attribute values.

    Two things changed here on 2026-08-20, and they are two halves of one
    problem.

    **The read is windowed.** `modified_date__gte` is honoured on both
    endpoints (verified: a 2099 probe returns 0 against 86,391). PL price had
    zero modifications in twelve days; PL stock had 6,521 of 17,202 in one. So
    the nightly pass reads roughly a fifteenth of what it used to, which is the
    difference between a stage that finishes and one that has never once
    finished in two attempts.

    **The write can now remove.** Until today this stage only ever upserted, so
    a SKU whose price was withdrawn or whose stock reached zero simply stopped
    appearing in the filtered read and kept its last known value forever, still
    marked `live`. Every such row counted towards the live totals. That is one
    of the standing suspects for running ~430 ModelColors per market above the
    Looker control table, and it could only ever grow.

    The sweep is therefore what makes the numbers shrink as well as grow — and
    it is confined to full passes, because on an incremental read "not seen"
    means nothing at all.
    """
    mode = mode or SyncMode(full=True)
    attrs = _attribute_ids()
    owned = [attrs[k] for k in _OWNED_KEYS if k in attrs]
    pk_to_sku = _pk_to_sku()
    source_ref = f"sync_run:{run.id}"
    written = 0
    retired = 0
    per_market: dict[str, int] = {}

    for market in MARKETS.values():
        records: list[dict[str, Any]] = []
        # (sku, attribute_id) actually observed upstream this pass. Only
        # meaningful — and only used — in full mode.
        observed: set[tuple[str, int]] = set()

        price_params: dict[str, Any] = {
            "price_list": market.price_list_pk, "price__gt": 0
        }
        stock_params: dict[str, Any] = {
            "stock_list": market.stock_list_pk, "stock__gt": 0
        }
        if mode.since is not None:
            window = _as_utc_param(mode.since)
            price_params["modified_date__gte"] = window
            stock_params["modified_date__gte"] = window

        for row in api.iter_records(
            "/api/v1/product_price/",
            params=price_params,
            page_size=100,
        ):
            run.seen()
            sku = pk_to_sku.get(int(row.get("product") or -1))
            if sku is None:
                continue
            for key, field in (("price", "price"), ("retail_price", "retail_price")):
                value = row.get(field)
                if value in (None, ""):
                    continue
                observed.add((sku, attrs[key]))
                records.append(
                    {
                        "entity_kind": "sku", "entity_key": sku,
                        "attribute_id": attrs[key], "market": market.code,
                        "value": str(value), "state": "live", "entry_grain": "sku",
                        "source_kind": "omnitron_api", "source_ref": source_ref,
                    }
                )

        for row in api.iter_records(
            "/api/v1/product_stock/",
            params=stock_params,
            page_size=100,
        ):
            run.seen()
            sku = pk_to_sku.get(int(row.get("product") or -1))
            if sku is None:
                continue
            observed.add((sku, attrs["stock"]))
            records.append(
                {
                    "entity_kind": "sku", "entity_key": sku,
                    "attribute_id": attrs["stock"], "market": market.code,
                    "value": str(row.get("stock")), "state": "live",
                    "entry_grain": "sku", "source_kind": "omnitron_api",
                    "source_ref": source_ref,
                }
            )

        # De-duplicate on the table's own key before writing: a product can
        # carry more than one price row on one list, and two rows contending
        # for one key is a question about the data, not something an INSERT
        # should answer.
        deduped: dict[tuple, dict[str, Any]] = {}
        for r in records:
            deduped[(r["entity_key"], r["attribute_id"], r["market"])] = r

        rows = list(deduped.values())
        for start in range(0, len(rows), 2000):
            with session_scope() as db:
                result = upsert_attribute_values(db, rows[start : start + 2000])
            written += result.written

        per_market[market.code] = len(deduped)

        if mode.full:
            retired += _retire_unobserved(market.code, owned, observed, run)

        log.info(
            "liveness_market",
            market=market.code, values=len(deduped), mode=mode.label,
        )

    run.imported(written)
    stats = {
        "written": written,
        "retired": retired,
        **{f"values_{k}": v for k, v in per_market.items()},
    }
    log.info("liveness_synced", mode=mode.label, **stats)
    return stats


def _retire_unobserved(
    market: str,
    owned: list[int],
    observed: set[tuple[str, int]],
    run: SyncRun,
) -> int:
    """Delete price and stock values this full pass proved are gone upstream.

    Deletion rather than a `stale` state, because the model already says an
    absent row *is* the absence: "`missing` and `not applicable` are computed,
    never stored". Writing a tombstone would invent a fourth thing for the
    coverage queries to know about. `stale` is not the right word either — it
    means our published value diverged from Omnitron's, which is a different
    event with a different owner.

    Three filters keep this narrow enough to be safe. Only the three attributes
    this stage writes; only the `current` lane at `state=live`; only rows whose
    `source_kind` says this sync put them there. A price a human approved, or
    one waiting in a batch at `exported`, is not ours to remove — that is the
    same boundary `writer.PROTECTED_STATES` draws on the write path.
    """
    run.mode.require_full(f"the {market} price/stock retirement sweep")
    if not owned:
        return 0

    with session_scope() as db:
        held = db.execute(
            select(
                AttributeValue.id,
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.value,
            )
            .where(
                AttributeValue.entity_kind == "sku",
                AttributeValue.market == market,
                AttributeValue.state_lane == "current",
                AttributeValue.state == "live",
                AttributeValue.source_kind == "omnitron_api",
                AttributeValue.attribute_id.in_(owned),
            )
        ).all()

    doomed = [
        (row_id, sku, attr_id, value)
        for row_id, sku, attr_id, value in held
        if (sku, attr_id) not in observed
    ]
    if not doomed:
        return 0

    # Record the event, then delete the value — in that order and in one
    # transaction per batch. The other order loses the trace if the process
    # dies mid-sweep, and losing the trace is the whole thing this fixes.
    now = utcnow()
    reason = f"{market}: no longer priced or stocked"
    for batch in chunked([str(i) for i, _, _, _ in doomed], _DELETE_CHUNK):
        ids = {int(i) for i in batch}
        with session_scope() as db:
            db.bulk_insert_mappings(
                ValueRetirement,
                [
                    {
                        "entity_kind": "sku", "entity_key": sku,
                        "attribute_id": attr_id, "market": market,
                        "last_value": value, "retired_at": now,
                        "sync_run_id": run.id, "reason": reason,
                    }
                    for row_id, sku, attr_id, value in doomed
                    if row_id in ids
                ],
            )
            db.execute(delete(AttributeValue).where(AttributeValue.id.in_(ids)))

    run.retired(len(doomed), reason=reason)
    log.info(
        "liveness_retired",
        market=market, count=len(doomed), held=len(held), observed=len(observed),
        recorded_in="value_retirements", sync_run_id=run.id,
    )
    return len(doomed)


def sync_images(api: OmnitronClient, run: SyncRun) -> dict[str, int]:
    """Capture each ModelColor's first image URL.

    Market-agnostic on purpose: `productimage_set` hangs off the product, not
    off a catalogue, so one image set serves all four markets.

    Stored at ModelColor grain and first-one-wins, because that is what the
    product grid renders — one thumbnail per ModelColor, not per size. The
    full photo set is a Product Detail concern and a different read.
    """
    attrs = _attribute_ids()
    source_ref = f"sync_run:{run.id}"

    with session_scope() as db:
        pk_to_mc = {
            row[0]: row[1]
            for row in db.execute(
                select(Product.omnitron_pk, Product.model_color).where(
                    Product.omnitron_pk.isnot(None), Product.is_size()
                )
            )
        }
    pks = list(pk_to_mc)

    by_mc: dict[str, str] = {}
    for batch in chunked([str(p) for p in pks], _IMAGE_BATCH):
        for record in api.iter_records(
            "/api/v1/products/",
            params={"pk__in": ",".join(batch)},
            page_size=_IMAGE_BATCH,
        ):
            run.seen()
            images = record.get("productimage_set") or []
            if not images:
                continue
            first = images[0]
            url = first.get("image") if isinstance(first, dict) else first
            if not url:
                continue
            mc = pk_to_mc.get(int(record.get("pk") or -1))
            if mc and mc not in by_mc:
                by_mc[mc] = str(url)

    image_rows: list[dict[str, Any]] = [
        {
            "entity_kind": "model_color", "entity_key": mc,
            "attribute_id": attrs["image_0"], "market": "*",
            "value": url, "state": "live", "entry_grain": "sku",
            "source_kind": "omnitron_api", "source_ref": source_ref,
        }
        for mc, url in by_mc.items()
    ]
    written = 0
    for start in range(0, len(image_rows), 2000):
        with session_scope() as db:
            result = upsert_attribute_values(db, image_rows[start : start + 2000])
        written += result.written

    # The exported -> live hop for photos we published. An option of ours that
    # Omnitron now shows an image for has landed: Turkey uploaded it (or it came
    # some other way, which is equally the end of our part). Until 0.46.0
    # nothing wrote this, so "with Turkey" only ever grew — 149 on a day when
    # 86 of those options already had a photo on the site.
    landed = 0
    now = utcnow()
    for batch in chunked(list(by_mc), _DELETE_CHUNK):
        with session_scope() as db:
            landed += (
                db.execute(
                    update(PublishedImage)
                    .where(
                        PublishedImage.landed_at.is_(None),
                        PublishedImage.model_color.in_(list(batch)),
                    )
                    .values(landed_at=now)
                    .execution_options(synchronize_session=False)
                ).rowcount
                or 0
            )

    run.imported(written)
    log.info("images_synced", model_colors=len(by_mc), written=written, landed=landed)
    return {"model_colors": len(by_mc), "written": written, "landed": landed}
