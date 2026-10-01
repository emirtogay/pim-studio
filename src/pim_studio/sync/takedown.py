"""Taking a product off a market's website, and putting it back.

`is_active: false` is not the lever. It is global — it removes a product from
every channel at once, Turkey's included — and the vault records that there is
no documented way to deactivate a product in one market and leave it live in
another (`Omnitron API/Products and attributes`). Per-market availability is a
catalogue question: a product sells in a market because a `catalog_items` row
puts it in that market's catalogue, and deleting that row is how it stops.

## The market group, and why it is not the market

A takedown cannot always be aimed at one market, and the reason is in
`scope.py`: **every market is served by catalogue 601** (`Northwind Global`) —
PL and CZ since it was found on 2026-09-07, HU and SK since 2026-09-16. Of 45
sampled options example.com was selling on 2026-09-07, 44 were in 601 and one
was in PL's own catalogue 4. So deleting from a market's own catalogue mostly
achieves nothing, and deleting from 601 takes the product off all four
storefronts at once. There is no third option, and today there is one group:
`PL+CZ+HU+SK`.

`GROUPS` derives that from `scope.MARKETS` rather than stating it: two markets
are in the same group when they share a catalogue, and the groups are the
connected components. Hardcoding `("PL", "CZ")` was correct for nine days and
became wrong the morning Hungary and Slovakia followed — and because it was
derived, the only change that morning was one line in `scope.py`. That is the
shape of the failure `scope.py` already records for catalogue 4, where a
tenant grew a fifth channel and nothing here noticed for eight months.

## Grain

A person acts on an option (`model_color`); catalogue membership is per SKU,
and `sync/catalogue.py` records why — `sku=4677500` is absent from PL and CZ
while its sibling `sku=2001212`, same base code, is in both. So one click
deletes one row per (size × catalogue that holds it), and a partial failure
leaves the option half-selling. `partial` is a real end state here, reported
rather than smoothed over: six sizes gone and two still on sale is worse than
either end, and nobody finds it by reading a success message.

## What this does not claim

It does not report the product as offline. Omnitron holding no catalogue row
is not the same as the customer no longer seeing it — the same gap
`sync/publication.py` measured for text, where Omnitron held a new Polish name
at 13:47 and example.com served it at 15:52. The storefront crawl is what
confirms a takedown, on its own schedule. This module reports what it deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..logging import log
from ..models import CatalogueRemoval, CatalogueTakedown, Product, utcnow
from ..omnitron.client import OmnitronClient, OmnitronError
from .scope import MARKET_CODES, MARKETS, markets_for_catalog


class TakedownError(RuntimeError):
    """A takedown that cannot be planned or run as asked."""


def _catalogs_of(code: str) -> frozenset[int]:
    market = MARKETS[code]
    return frozenset({market.catalog_pk, *market.shared_catalog_pks})


def _groups() -> tuple[tuple[str, ...], ...]:
    """Markets that cannot be taken offline independently of one another.

    Connected components over "shares a catalogue with". Presented in
    `MARKET_CODES` order so the panel lists PL before CZ before HU before SK
    however the components happen to fall out.
    """
    remaining = list(MARKET_CODES)
    components: list[list[str]] = []
    while remaining:
        head = remaining.pop(0)
        group = [head]
        reach = set(_catalogs_of(head))
        moved = True
        while moved:
            moved = False
            for code in list(remaining):
                if reach & _catalogs_of(code):
                    group.append(code)
                    reach |= _catalogs_of(code)
                    remaining.remove(code)
                    moved = True
        components.append(sorted(group, key=MARKET_CODES.index))
    return tuple(
        tuple(g) for g in sorted(components, key=lambda g: MARKET_CODES.index(g[0]))
    )


#: The units a takedown can actually be aimed at. Since 2026-09-16 there is
#: one — all four markets — and that is the finding, not a convenience.
GROUPS: tuple[tuple[str, ...], ...] = _groups()

#: Group key ("PL+CZ+HU+SK") -> the market codes in it.
GROUP_BY_KEY: dict[str, tuple[str, ...]] = {"+".join(g): g for g in GROUPS}


def group_for(key: str) -> tuple[str, ...]:
    """Resolve a group key, or say which keys exist.

    A bare market code is accepted when that market is a group on its own —
    none is, today — and refused otherwise, the refusal being the point: no
    market can be taken offline without the others that read its shared
    catalogue, and a caller asking to is holding a belief worth correcting
    rather than a typo worth fixing.
    """
    if key in GROUP_BY_KEY:
        return GROUP_BY_KEY[key]
    for group_key, codes in GROUP_BY_KEY.items():
        if key in codes:
            raise TakedownError(
                f"{key} cannot be taken offline on its own: it shares a "
                f"catalogue with {', '.join(c for c in codes if c != key)}. "
                f"Use {group_key!r}, which names every market this would "
                "affect."
            )
    raise TakedownError(
        f"{key!r} is not a market group. The groups are "
        f"{sorted(GROUP_BY_KEY)}."
    )


def catalogs_for_group(codes: tuple[str, ...]) -> tuple[int, ...]:
    """Every catalogue a group's markets read — and nobody else does.

    The second half is checked rather than assumed. A catalogue inside a group
    that some market outside it also reads would mean the components were
    built from a stale map, and the takedown would reach a market the person
    pressing the button was never shown.
    """
    catalogs: set[int] = set()
    for code in codes:
        catalogs |= _catalogs_of(code)
    inside = set(codes)
    for pk in sorted(catalogs):
        readers = {m.code for m in markets_for_catalog(pk)}
        if not readers <= inside:
            raise TakedownError(
                f"catalogue {pk} is also read by "
                f"{sorted(readers - inside)}, which this group does not "
                "cover. The market grouping is out of date — fix it in "
                "sync/scope.py before running a takedown."
            )
    return tuple(sorted(catalogs))


@dataclass
class PlannedRemoval:
    """One `catalog_items` row a takedown would delete."""

    sku: str
    product_id: int
    omnitron_product_pk: int
    catalog_pk: int
    catalog_item_pk: int


@dataclass
class Plan:
    """What a takedown would do, computed against Omnitron and not the mirror.

    Membership is read live rather than from `catalogue_memberships`, because
    the mirror is as old as the last sync and this is the one operation where
    acting on a stale answer deletes something. It also yields the
    `catalog_items` pks, which the mirror does not hold at all.
    """

    model_color: str
    markets: tuple[str, ...]
    catalogs: tuple[int, ...]
    removals: list[PlannedRemoval] = field(default_factory=list)
    #: SKUs we hold that Omnitron has never given us a pk for. They cannot be
    #: acted on and are named rather than counted, because "two sizes stayed
    #: on sale" needs to say which two.
    unresolved_skus: list[str] = field(default_factory=list)

    @property
    def size_count(self) -> int:
        return len({r.sku for r in self.removals})

    @property
    def catalogs_hit(self) -> tuple[int, ...]:
        return tuple(sorted({r.catalog_pk for r in self.removals}))

    @property
    def is_empty(self) -> bool:
        return not self.removals


def plan(
    db: Session, api: OmnitronClient, model_color: str, group_key: str
) -> Plan:
    """Work out exactly which catalogue rows a takedown would delete."""
    codes = group_for(group_key)
    catalogs = catalogs_for_group(codes)
    normalised = model_color.strip().upper()

    products = db.scalars(
        select(Product)
        .where(Product.model_color == normalised, Product.is_size())
        .order_by(Product.sku)
    ).all()
    if not products:
        raise TakedownError(f"{normalised} is not an option this panel holds.")

    result = Plan(model_color=normalised, markets=codes, catalogs=catalogs)
    for product in products:
        if product.omnitron_pk is None:
            result.unresolved_skus.append(product.sku)
            continue
        for item in api.catalog_items_for_product(product.omnitron_pk, catalogs):
            result.removals.append(
                PlannedRemoval(
                    sku=product.sku,
                    product_id=product.id,
                    omnitron_product_pk=product.omnitron_pk,
                    catalog_pk=int(item["catalog"]),
                    catalog_item_pk=int(item["pk"]),
                )
            )

    log.info(
        "takedown_planned",
        model_color=normalised, markets=codes, catalogs=catalogs,
        rows=len(result.removals), sizes=result.size_count,
        unresolved=len(result.unresolved_skus),
    )
    return result


def record_plan(
    db: Session,
    proposal: Plan,
    *,
    created_by: str | None,
    created_by_user_id: int | None,
    reason: str | None = None,
) -> CatalogueTakedown:
    """Persist a plan before anything is deleted.

    Written first and on purpose. After the delete, the `(product, catalog)`
    pairs exist nowhere else — recording them afterwards would mean a crash
    mid-run leaves rows deleted upstream and unrecorded here, which is the one
    state with no way back.
    """
    takedown = CatalogueTakedown(
        model_color=proposal.model_color,
        markets=",".join(proposal.markets),
        catalogs=",".join(str(c) for c in proposal.catalogs),
        reason=(reason or None),
        status="planned",
        created_by=created_by,
        created_by_user_id=created_by_user_id,
        planned_count=len(proposal.removals),
    )
    db.add(takedown)
    db.flush()
    for item in proposal.removals:
        db.add(
            CatalogueRemoval(
                takedown_id=takedown.id,
                product_id=item.product_id,
                sku=item.sku,
                omnitron_product_pk=item.omnitron_product_pk,
                catalog_pk=item.catalog_pk,
                catalog_item_pk=item.catalog_item_pk,
                state="planned",
            )
        )
    db.flush()
    return takedown


def execute(db: Session, api: OmnitronClient, takedown: CatalogueTakedown) -> CatalogueTakedown:
    """Delete the planned rows, marking each as it goes.

    Each removal is committed as it happens rather than at the end: a run that
    dies halfway must leave a record of what it already deleted, and a single
    transaction around the whole loop would roll that record back while leaving
    the upstream deletes in place.
    """
    allowed = [int(c) for c in takedown.catalogs.split(",") if c]
    rows = db.scalars(
        select(CatalogueRemoval)
        .where(
            CatalogueRemoval.takedown_id == takedown.id,
            CatalogueRemoval.state == "planned",
        )
        .order_by(CatalogueRemoval.sku, CatalogueRemoval.catalog_pk)
    ).all()

    takedown.status = "removing"
    db.flush()

    removed = failed = 0
    first_error: str | None = None
    for row in rows:
        try:
            api.delete_catalog_item(row.catalog_item_pk, allowed_catalogs=allowed)
        except OmnitronError as exc:
            row.state = "failed"
            row.error = str(exc)[:1000]
            failed += 1
            first_error = first_error or str(exc)
            log.warning(
                "takedown_item_failed",
                takedown=takedown.id, sku=row.sku, catalog=row.catalog_pk,
                error=str(exc),
            )
        else:
            row.state = "removed"
            row.removed_at = utcnow()
            removed += 1
        db.flush()

    takedown.removed_count = removed
    takedown.failed_count = failed
    takedown.finished_at = utcnow()
    takedown.status = (
        "done" if failed == 0 else ("failed" if removed == 0 else "partial")
    )
    if first_error:
        takedown.error_summary = first_error[:2000]
    db.flush()

    log.info(
        "takedown_finished",
        takedown=takedown.id, model_color=takedown.model_color,
        status=takedown.status, removed=removed, failed=failed,
    )
    return takedown


def restore(db: Session, api: OmnitronClient, takedown: CatalogueTakedown) -> CatalogueTakedown:
    """Put back every row this takedown deleted.

    Safe to retry: `(product, catalog)` is unique upstream, so a row already
    restored is refused rather than duplicated, and rows that never came out
    are not touched.
    """
    allowed = [int(c) for c in takedown.catalogs.split(",") if c]
    rows = db.scalars(
        select(CatalogueRemoval)
        .where(
            CatalogueRemoval.takedown_id == takedown.id,
            CatalogueRemoval.state == "removed",
        )
        .order_by(CatalogueRemoval.sku, CatalogueRemoval.catalog_pk)
    ).all()
    if not rows:
        raise TakedownError(
            f"takedown {takedown.id} has nothing to restore — no row it "
            "recorded is currently removed."
        )

    restored = failed = 0
    first_error: str | None = None
    for row in rows:
        try:
            api.create_catalog_item(
                product_pk=row.omnitron_product_pk,
                catalog_pk=row.catalog_pk,
                allowed_catalogs=allowed,
            )
        except OmnitronError as exc:
            row.error = str(exc)[:1000]
            failed += 1
            first_error = first_error or str(exc)
            log.warning(
                "restore_item_failed",
                takedown=takedown.id, sku=row.sku, catalog=row.catalog_pk,
                error=str(exc),
            )
        else:
            row.state = "restored"
            row.restored_at = utcnow()
            row.error = None
            restored += 1
        db.flush()

    if failed == 0:
        takedown.status = "restored"
        takedown.restored_at = utcnow()
        takedown.error_summary = None
    else:
        takedown.error_summary = first_error[:2000] if first_error else None
    takedown.failed_count = failed
    db.flush()

    log.info(
        "takedown_restored",
        takedown=takedown.id, restored=restored, failed=failed,
    )
    return takedown
