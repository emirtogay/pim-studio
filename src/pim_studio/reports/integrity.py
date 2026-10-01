"""Does what we hold match what Omnitron has?

Every sync stage reports its own success, and each one is honest about what it
did. None of them answers the question that actually matters afterwards:
*is the database now a faithful copy?* A stage that read 90% of a resource and
wrote all of it reports success twice over.

So this counts both sides independently and compares. It is the only check
here that talks to the API, because a self-consistency check that never leaves
the database cannot detect the failure mode it exists for.

Three classes of finding, deliberately kept apart:

**Drift** — the counts disagree. Something was missed, or something changed
upstream since the last sync. Expected to be small and non-zero on a live
tenant; large means a stage failed quietly.

**Orphans** — rows referencing something that is not there. A membership whose
product was deleted, a value whose attribute is gone. These are ours to fix and
never resolve themselves.

**Staleness** — a stage that has not succeeded recently. Not an error, but the
reason a number can be wrong while every check passes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from ..config import settings
from ..db import session_scope
from ..models import (
    Attribute,
    AttributeValue,
    CatalogueMembership,
    OmnitronAttribute,
    OmnitronAttributeValue,
    Product,
    SyncRun,
)
from ..omnitron.client import OmnitronClient, OmnitronError
from ..sync.scope import MARKETS


@dataclass
class Check:
    name: str
    ours: int | None
    theirs: int | None
    detail: str = ""

    @property
    def drift(self) -> int | None:
        if self.ours is None or self.theirs is None:
            return None
        return self.ours - self.theirs

    @property
    def ok(self) -> bool:
        return self.drift == 0


@dataclass
class IntegrityReport:
    counts: list[Check] = field(default_factory=list)
    orphans: list[tuple[str, int]] = field(default_factory=list)
    stale: list[tuple[str, str]] = field(default_factory=list)
    unfinished: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return (
            all(c.ok for c in self.counts if c.drift is not None)
            and not self.orphans
            and not self.stale
            and not self.unfinished
        )


def _remote(api: OmnitronClient, path: str, **params: object) -> int | None:
    try:
        return int(api.get(path, params={"limit": 1, **params}).get("count") or 0)
    except (OmnitronError, TypeError, ValueError):
        return None


def _remote_total(api: OmnitronClient, path: str, catalogs: Iterable[int]) -> int | None:
    """Counts across several catalogues added up, or None if any could not be read.

    The `None` has to survive the addition, and `sum()` is where it stopped:
    `_remote` returns `int | None` by design — every consumer of `Check` is
    built for it, `drift` returns None when either side is unknown and `clean`
    skips those rows — but `sum()` raises `TypeError` on the first one it meets.
    So a single 502 on one catalogue took down the whole integrity report
    instead of leaving one line saying it could not be read.

    That mattered more here than at the other two call sites because this is the
    only one that makes several requests: four to six against their one each, so
    it is the likeliest to meet a bad response and the only one that could not
    survive it. And it failed at exactly the moment the report is wanted, since
    the reason to run it is usually that Omnitron is behaving oddly.

    A partial sum is not offered as a fallback. "Three of PL's four catalogues"
    is a number that looks like an answer and is not one — it would print as a
    drift against our full membership and send somebody after a shortfall that
    is only a missing request.
    """
    total = 0
    for pk in catalogs:
        one = _remote(api, path, catalog=pk)
        if one is None:
            return None
        total += one
    return total


def check_integrity(api: OmnitronClient | None = None) -> IntegrityReport:
    """Compare our tables against Omnitron, and against themselves."""
    report = IntegrityReport()

    with session_scope() as db:
        held = {
            "products": db.scalar(select(func.count()).select_from(Product)) or 0,
            "memberships": db.scalar(
                select(func.count()).select_from(CatalogueMembership).where(
                    CatalogueMembership.left_at.is_(None)
                )
            ) or 0,
            "omnitron_attributes": db.scalar(
                select(func.count()).select_from(OmnitronAttribute)
            ) or 0,
            "omnitron_attribute_values": db.scalar(
                select(func.count()).select_from(OmnitronAttributeValue)
            ) or 0,
        }
        per_market = {
            code: db.scalar(
                select(func.count()).select_from(CatalogueMembership).where(
                    CatalogueMembership.market == code,
                    CatalogueMembership.left_at.is_(None),
                )
            ) or 0
            for code in MARKETS
        }

        # Orphans. Referential integrity we own, in tables that deliberately
        # carry no foreign key to Omnitron's ids — a mirror must accept what
        # upstream sends, so the check lives here rather than in the schema.
        orphan_values = db.scalar(
            select(func.count()).select_from(AttributeValue).where(
                AttributeValue.attribute_id.notin_(select(Attribute.id))
            )
        ) or 0
        if orphan_values:
            report.orphans.append(("attribute_values with no attribute", orphan_values))

        orphan_members = db.scalar(
            select(func.count()).select_from(CatalogueMembership).where(
                CatalogueMembership.product_id.notin_(select(Product.id))
            )
        ) or 0
        if orphan_members:
            report.orphans.append(("memberships with no product", orphan_members))

        no_pk = db.scalar(
            select(func.count()).select_from(Product).where(Product.omnitron_pk.is_(None))
        ) or 0
        if no_pk:
            report.orphans.append(("products with no omnitron_pk", no_pk))

        # Runs that never closed. A row still `running` long after the fact is
        # a process that died without its finaliser — the evidence the run row
        # is committed up front precisely to preserve.
        cutoff = datetime.now(UTC) - timedelta(hours=6)
        for row in db.scalars(
            select(SyncRun).where(SyncRun.status.in_(("running", "failed")))
        ):
            started = row.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if row.status == "failed" or (started and started < cutoff):
                report.unfinished.append((row.id, row.kind, row.status))

        # Staleness, per kind, against the configured alert window.
        window = timedelta(hours=settings.stale_sync_alert_hours)
        for kind in ("omnitron_registry", "omnitron_catalogue", "omnitron_values"):
            last = db.scalar(
                select(func.max(SyncRun.started_at)).where(
                    SyncRun.kind == kind,
                    SyncRun.status.in_(("success", "partial")),
                )
            )
            if last is None:
                report.stale.append((kind, "never succeeded"))
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            age = datetime.now(UTC) - last
            if age > window:
                report.stale.append((kind, f"last success {age.days}d ago"))

    if api is None:
        return report

    report.counts.append(
        Check("attributes", held["omnitron_attributes"], _remote(api, "/api/v1/attributes/"))
    )
    report.counts.append(
        Check(
            "attribute values (vocabulary)",
            held["omnitron_attribute_values"],
            _remote(api, "/api/v1/attribute_value/"),
        )
    )
    for code, market in MARKETS.items():
        # Every catalogue the market reads, not just its own. PL and CZ each
        # read a shared one as well, and comparing our membership against a
        # single catalogue's count would report a permanent shortfall that is
        # really the other catalogue — a check that is always red is a check
        # nobody looks at.
        #
        # Summed rather than de-duplicated, so a product in both of a market's
        # catalogues is counted twice upstream while we hold one membership
        # row. That overstates the remote side; it is recorded as a known
        # skew rather than corrected here, because correcting it means pulling
        # both catalogues whole to intersect them, which is minutes of API for
        # a reconciliation line.
        remote = _remote_total(
            api,
            "/api/v1/catalog_items/",
            (market.catalog_pk, *market.shared_catalog_pks),
        )
        report.counts.append(
            Check(f"catalogue membership {code}", per_market[code], remote)
        )
    return report
