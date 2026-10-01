"""Discovering which products are actually ours.

Until now the product set came from `Check_list_2026-04-16.xls` — one PL check
list, 26,813 SKUs, frozen in April. PL's catalogue holds 51,914. Everything
absent from that file was invisible to us, and re-syncing could never find it:
refreshing a frozen list perfectly leaves it frozen.

So the set is now **catalogue membership**: the union of `catalog_items` across
catalogs 4 (PL), 40 (CZ), 41 (HU), 172 (SK) and the shared 601. That is the authoritative
answer to "what is in our markets", and it self-corrects as Omnitron changes.

Three things learned the hard way and encoded here:

**`catalog__in` is silently ignored** — it returns the unfiltered 495,871 —
so this reads one catalogue per pass. `catalog=N` singular *is* honoured.

**Membership is per SKU, not per style.** `sku=4677500` is absent from the PL
and CZ catalogues while its sibling `sku=2001212`, same base code, is in both.
Resolving at style level would silently include products no market sells.

**A market can read more than one catalogue.** All four markets also read 601
(`Northwind Global`) — PL and CZ since 2026-09-07, HU and SK since 2026-09-16; see
`scope.Market.shared_catalog_pks`. Of 45 sampled options example.com was selling
that this stage had never discovered, 44 were in 601 and one was in catalogue
4. Reading only `catalog_pk` is why 283 options the PL storefront sells were
unaccounted for, 57 of them not held at all.

**Leaving a catalogue is not deletion.** A product dropping out of PL is a
commercial event; `Product.delisted_at` means Omnitron deleted the record.
A product can leave PL, remain in CZ, and be deleted in neither, so this stage
sets `left_at` on the membership and never touches `delisted_at`.

**A catalogue item is not always a size.** Omnitron's product_meta rows — the
grouping objects behind listing cards — are catalogue items too, and this stage
minted a `Product` for each of them for as long as it existed. They are skipped
now; `models.PRODUCT_META` records what they cost.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ..db import session_scope
from ..logging import log
from ..models import CatalogueMembership, Product, is_grouping_row, utcnow
from ..omnitron.client import OmnitronClient
from ..rules.identifiers import model_color as derive_model_color
from .batching import chunked
from .registry import _parse_dt
from .runs import SyncMode, SyncRun
from .scope import MARKETS, markets_for_catalog


def _as_utc_param(moment: datetime) -> str:
    """Format a watermark for `modified_date__gte`, explicitly in UTC.

    Values are stored UTC, but SQLite hands them back naive. Sending one
    without the `Z` leaves the server free to read it in its own timezone,
    which shifts the window by hours and drops whatever fell in the gap —
    silently, since a short read looks exactly like a quiet night.
    """
    stamped = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return stamped.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

#: Product pks per resolution call. `pk__in` is honoured (verified) and behaves
#: like `sku__in` — comma-joined string, never a list.
_PK_BATCH = 100

#: The attribute carrying MODEL.COLOR.SIZE. Confirmed 2026-08-14 that
#: `base_code_format` is always that shape; `integration_model_renk` carries
#: MODEL.COLOR and is deliberately not used here.
_MCS_KEY = "integration_base_code_format"


def _membership_pass(
    api: OmnitronClient, run: SyncRun, mode: SyncMode
) -> dict[int, dict[str, int]]:
    """Read the catalogues and return product pk -> market -> catalogue seen in.

    The catalogue is carried rather than the bare market code so a membership
    row records where it actually came from. With PL reading both 4 and 601,
    "which catalogue is holding this up" stops being derivable from the market
    and becomes the thing you need when one of them empties.

    In full mode this is every membership upstream holds. In incremental mode
    it is only those modified since the watermark — which is emphatically **not
    the same set**, and the return value must not be treated as if it were. It
    is safe to add and refresh from; it is never safe to subtract from.

    `modified_date__gte` is honoured here (verified 2026-08-20: a 2099 probe
    returns 0 against 51,914, so it is applied rather than silently dropped).

    Each catalogue is read once, however many markets read it: every market's
    own catalogue first, then the shared ones. Since 2026-09-16 all four
    markets read 601 — 60,003 items — and reading it per market would fetch
    the same pages four times a night. Own-first keeps the provenance rule
    below exactly as it was.
    """
    order = [market.catalog_pk for market in MARKETS.values()]
    for market in MARKETS.values():
        order.extend(pk for pk in market.shared_catalog_pks if pk not in order)

    holders: dict[int, dict[str, int]] = {}
    for catalog in order:
        readers = markets_for_catalog(catalog)
        seen = 0
        params: dict[str, Any] = {"catalog": catalog}
        if mode.since is not None:
            params["modified_date__gte"] = _as_utc_param(mode.since)
        for record in api.iter_records(
            "/api/v1/catalog_items/", params=params, page_size=100
        ):
            product_pk = record.get("product")
            if product_pk is None:
                run.dropped(reason="catalog_item with no product")
                continue
            # First catalogue wins, and every market's own is read before any
            # shared one, so a product in both keeps the more specific
            # provenance.
            held = holders.setdefault(int(product_pk), {})
            for market in readers:
                held.setdefault(market.code, catalog)
            seen += 1
        run.seen(seen)
        log.info(
            "catalogue_read",
            markets=[market.code for market in readers], catalog=catalog,
            memberships=seen, shared=len(readers) > 1, mode=mode.label,
        )
    return holders


def _resolve(api: OmnitronClient, pks: list[int]) -> dict[int, dict[str, Any]]:
    """Fetch product records for a batch of pks."""
    found: dict[int, dict[str, Any]] = {}
    for batch in chunked([str(p) for p in pks], _PK_BATCH):
        body_params = {"pk__in": ",".join(batch), "limit": _PK_BATCH}
        for record in api.iter_records(
            "/api/v1/products/", params=body_params, page_size=_PK_BATCH
        ):
            pk = record.get("pk")
            if pk is not None:
                found[int(pk)] = record
    return found


def discover_products(
    api: OmnitronClient, run: SyncRun, mode: SyncMode | None = None
) -> dict[str, int]:
    """Reconcile our product table against catalogue membership.

    Full mode is a genuine reconciliation: everything upstream holds is read,
    so anything missing from the result has left and the departure sweep runs.
    Incremental mode reads a window and can only add and refresh.
    """
    mode = mode or SyncMode(full=True)
    holders = _membership_pass(api, run, mode)
    log.info("catalogue_union", products=len(holders), mode=mode.label)

    with session_scope() as db:
        known = {
            row[0]: row[1]
            for row in db.execute(select(Product.omnitron_pk, Product.id)).all()
            if row[0] is not None
        }

    unknown = [pk for pk in holders if pk not in known]
    log.info("catalogue_new_products", count=len(unknown))

    created = skipped = metas = 0
    now = utcnow()

    for chunk in chunked([str(p) for p in unknown], 1000):
        resolved = _resolve(api, [int(p) for p in chunk])
        rows: list[dict[str, Any]] = []
        for pk_str in chunk:
            pk = int(pk_str)
            record = resolved.get(pk)
            if record is None:
                skipped += 1
                run.dropped(reason="catalogue product pk did not resolve")
                continue
            # Before anything is derived from it. A meta's `sku` is the bare
            # base code and its `integration_base_code_format` names one
            # arbitrary colour and size, so every field below would come out
            # looking like a real size of a real ModelColor.
            #
            # Through `models.is_grouping_row`, not `== PRODUCT_META`. This read
            # comes from `_resolve`, which is `/api/v1/products/`, and that
            # endpoint returns `product_type` as the string `"1"` — so the
            # direct comparison against the integer was False for every
            # grouping row in the tenant and this guard had never once fired.
            # Silently: the drop it records is absent from every recorded run
            # rather than wrong in any of them, which is the shape of failure
            # this codebase keeps naming.
            if is_grouping_row(record):
                metas += 1
                run.dropped(reason="product_meta is a grouping row, not a size")
                continue
            sku = str(record.get("sku") or "").strip()
            mcs = str((record.get("attributes") or {}).get(_MCS_KEY) or "").strip().upper()
            if not sku or not mcs:
                skipped += 1
                run.dropped(
                    reason="product has no sku or no integration_base_code_format"
                )
                continue
            rows.append(
                {
                    "sku": sku,
                    "base_code_format": mcs,
                    "model_color": derive_model_color(mcs),
                    "base_code": mcs.split(".")[0],
                    "size": mcs.split(".")[2] if mcs.count(".") >= 2 else None,
                    "omnitron_pk": pk,
                    "omnitron_is_active": record.get("is_active"),
                    "omnitron_product_type": record.get("product_type"),
                    "omnitron_modified_date": _parse_dt(record.get("modified_date")),
                    "first_seen_at": now,
                    "last_seen_at": now,
                }
            )
        if rows:
            with session_scope() as db:
                # A SKU we already hold under a different pk would collide on
                # the unique index; filter rather than let the insert fail.
                existing = set(
                    db.scalars(
                        select(Product.sku).where(
                            Product.sku.in_([r["sku"] for r in rows])
                        )
                    ).all()
                )
                fresh = [r for r in rows if r["sku"] not in existing]
                if fresh:
                    db.bulk_insert_mappings(Product, fresh)
                created += len(fresh)
                skipped += len(rows) - len(fresh)

    memberships = _write_memberships(holders, now, run, mode)
    run.imported(created + memberships["added"])

    stats = {
        "catalogue_union": len(holders),
        "already_known": len(holders) - len(unknown),
        "products_created": created,
        "product_metas_skipped": metas,
        "unresolvable": skipped,
        **memberships,
    }
    log.info("catalogue_discovery", mode=mode.label, **stats)
    return stats


def _write_memberships(
    holders: dict[int, dict[str, int]], now: Any, run: SyncRun, mode: SyncMode
) -> dict[str, int]:
    """Record which markets hold each product, and which have let it go.

    The departure sweep at the bottom is the dangerous half. It works by
    subtraction — anything in the table that this pass did not see is marked as
    having left — which is correct only when "this pass" means "everything
    upstream holds". Against an incremental read it would mark 258,000
    memberships as departed on a night when four of them changed, and it would
    do it quietly, because marking `left_at` is a normal thing for this code to
    do. Hence the guard rather than a comment.
    """
    added = refreshed = departed = 0
    with session_scope() as db:
        by_pk = {
            row[0]: row[1]
            for row in db.execute(select(Product.omnitron_pk, Product.id)).all()
            if row[0] is not None
        }
        current = {
            (m.product_id, m.market): m
            for m in db.scalars(select(CatalogueMembership)).all()
        }

        wanted: set[tuple[int, str]] = set()
        for pk, markets in holders.items():
            product_id = by_pk.get(pk)
            if product_id is None:
                continue
            for code, catalog in markets.items():
                wanted.add((product_id, code))
                row = current.get((product_id, code))
                if row is None:
                    db.add(
                        CatalogueMembership(
                            product_id=product_id,
                            market=code,
                            catalog_pk=catalog,
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                    added += 1
                else:
                    row.last_seen_at = now
                    if row.catalog_pk != catalog:
                        # A product moving between a market's catalogues is the
                        # migration happening one row at a time. Said out loud
                        # because the aggregate is invisible: membership stays
                        # true throughout and no count moves.
                        log.info(
                            "membership_catalogue_moved",
                            product_id=product_id, market=code,
                            was=row.catalog_pk, now=catalog,
                        )
                        row.catalog_pk = catalog
                    if row.left_at is not None:
                        # Back in the catalogue after leaving. Worth saying out
                        # loud rather than silently clearing.
                        log.info(
                            "membership_rejoined",
                            product_id=product_id, market=code,
                            left_at=str(row.left_at),
                        )
                        row.left_at = None
                    refreshed += 1

        if mode.full:
            mode.require_full("the catalogue departure sweep")
            for (product_id, code), row in current.items():
                if (product_id, code) not in wanted and row.left_at is None:
                    row.left_at = now
                    departed += 1
                    run.retired(reason="left the catalogue")

    if departed:
        log.info("memberships_departed", count=departed)
    elif not mode.full:
        # Said out loud every time. A run that skipped the sweep and a run that
        # swept and found nothing produce the same zero, and only one of them
        # means the catalogue is reconciled.
        log.info("memberships_departure_sweep_skipped", reason="incremental pass")
    return {"added": added, "refreshed": refreshed, "departed": departed}
