"""The demo's past: what a month of running would have left behind.

The seed produces two real reads (yesterday's and today's) and a morning's
review work. A panel that has run for a month has more than that — a column of
nightly runs on Sync Runs, some of them red, and an activity log full of
sign-ins — and those pages read as broken without it. This writes that past,
and nothing else: every product, value, membership and listing in the database
came from the sync itself.

All of it is relative to the moment the seed ran and deterministic in count:
the same rows on every fresh database, at times measured back from "now".
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..admin import audit
from ..db import session_scope
from ..models import (
    AuditLog,
    BrandLibraryAbsence,
    CatalogueMembership,
    CatalogueRemoval,
    CatalogueTakedown,
    HandoffBundle,
    Product,
    PublishedImage,
    StorefrontListing,
    SyncRun,
    User,
)
from .catalog import SEED

#: Days of nightly history before yesterday's real read.
DAYS = 30

#: The nightly pipeline as cron runs it: kind, minutes after 01:00, and a
#: fallback duration in seconds for a kind today's read did not produce.
NIGHTLY: tuple[tuple[str, int, int], ...] = (
    ("storefront", 0, 170),
    ("omnitron_registry", 4, 62),
    ("omnitron_labels", 6, 41),
    ("omnitron_catalogue", 8, 1006),
    ("omnitron_products", 26, 281),
    ("omnitron_values", 32, 1490),
    ("omnitron_liveness", 58, 1904),
    ("omnitron_images", 91, 612),
    ("proposals", 102, 33),
    ("publication", 103, 6),
)

#: Daytime storefront pulls, on top of the nightly one.
DAYTIME_STOREFRONT = (10, 16)

#: Nights that went wrong, and how. (days ago, kind) -> (status, summary).
#: A failed stage stops the pipeline, so nothing after it runs that night.
INCIDENTS: dict[tuple[int, str], tuple[str, str]] = {
    (27, "omnitron_liveness"): (
        "failed",
        "Traceback (most recent call last):\n  ...\npim_studio.omnitron.client."
        "OmnitronError: GET https://pim.example.test/api/v1/product_stock/?limit=100"
        "&page=31&sort=id&stock_list=168&stock__gt=0 -> HTTP 502: Bad Gateway",
    ),
    (19, "storefront@10"): (
        "partial",
        "dropped 1 row(s) — HU: storefront read failed: 1",
    ),
    (12, "omnitron_values"): (
        "failed",
        "Traceback (most recent call last):\n  ...\npim_studio.omnitron.client."
        "OmnitronError: GET https://pim.example.test/api/v1/products/?limit=100&page=1"
        "&sort=id -> ReadTimeout after 5 retries: The read operation timed out",
    ),
    (5, "omnitron_catalogue"): (
        "partial",
        "dropped 3 row(s) — catalogue product pk did not resolve: 3",
    ),
    (22, "brandlibrary_publish"): (
        "partial",
        "dropped 2 row(s) — asset could not be fetched or sent: 2",
    ),
}

#: Weekly image publishes, days ago, at 05:00.
WEEKLY_PUBLISH = (8, 15, 22, 29)

#: Kinds whose `rows_imported` counts changes rather than rows read.
_CHANGE_COUNTED = frozenset({"omnitron_catalogue", "omnitron_values", "omnitron_liveness",
                             "proposals", "publication", "brandlibrary_publish"})
#: Kinds whose full pass may retire what upstream no longer holds.
_RETIRING = frozenset({"omnitron_catalogue", "omnitron_liveness"})
_MARKETS = ("PL", "CZ", "SK", "HU")

#: A documentation address per person, so the activity log reads like one.
_IP_BASE = "203.0.113."


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# ── yesterday ──────────────────────────────────────────────────────────────


#: Columns the first read wrote. Moved back a day so it reads as yesterday's.
_FIRST_READ: tuple[tuple[Any, tuple[str, ...]], ...] = (
    (SyncRun, ("started_at", "finished_at")),
    (Product, ("first_seen_at", "last_seen_at")),
    (CatalogueMembership, ("first_seen_at", "last_seen_at", "left_at")),
    (StorefrontListing, ("first_seen_at", "last_seen_at", "left_at")),
    (PublishedImage, ("published_at",)),
    (HandoffBundle, ("built_at", "sent_at")),
    (BrandLibraryAbsence, ("first_missed_at", "last_checked_at")),
    (AuditLog, ("created_at",)),
)


def backdate_first_read(before: datetime, by: timedelta = timedelta(days=1)) -> None:
    """Move every timestamp written before `before` back by `by`."""
    cutoff = _aware(before)
    with session_scope() as db:
        for model, columns in _FIRST_READ:
            for row in db.scalars(select(model)):
                for column in columns:
                    value = getattr(row, column)
                    if value is not None and _aware(value) < cutoff:
                        setattr(row, column, _aware(value) - by)


# ── the month before ───────────────────────────────────────────────────────


def synthesise(now: datetime) -> None:
    """Everything a month of running leaves behind, measured back from `now`."""
    now = _aware(now)
    rng = random.Random(SEED + 1)
    _accounts_and_takedowns(now)
    _nightly_runs(now, rng)
    _activity(now, rng)
    _addresses()


def _accounts_and_takedowns(now: datetime) -> None:
    """People joined five weeks ago; yesterday afternoon a product came down."""
    joined = now - timedelta(days=35)
    with session_scope() as db:
        for user in db.scalars(select(User)):
            user.created_at = joined
        for row in db.scalars(
            select(AuditLog).where(AuditLog.action.in_((audit.CREATED_USER,
                                                        audit.UPDATED_USER)))
        ):
            row.created_at = joined + timedelta(minutes=5 + row.id)

        afternoon = (now - timedelta(days=1)).replace(hour=15, minute=10, second=0,
                                                      microsecond=0)
        takedowns = list(db.scalars(select(CatalogueTakedown).order_by(CatalogueTakedown.id)))
        for n, takedown in enumerate(takedowns):
            created = afternoon - timedelta(hours=4 * n)
            moved = created - _aware(takedown.created_at)
            takedown.created_at = created
            if takedown.finished_at is not None:
                takedown.finished_at = _aware(takedown.finished_at) + moved
            if takedown.restored_at is not None:
                takedown.restored_at = created + timedelta(minutes=35)
            for removal in db.scalars(
                select(CatalogueRemoval).where(CatalogueRemoval.takedown_id == takedown.id)
            ):
                if removal.removed_at is not None:
                    removal.removed_at = _aware(removal.removed_at) + moved
                if removal.restored_at is not None:
                    removal.restored_at = created + timedelta(minutes=35)
            for row in db.scalars(
                select(AuditLog).where(
                    AuditLog.action.in_((audit.TOOK_OFFLINE, audit.PUT_BACK_ONLINE)),
                    AuditLog.detail.like(f"{takedown.model_color}%"),
                )
            ):
                row.created_at = (
                    created + timedelta(minutes=35)
                    if row.action == audit.PUT_BACK_ONLINE
                    else created + timedelta(seconds=4)
                )


def _template(db: Any) -> dict[str, SyncRun]:
    """Today's real run per kind — the numbers the history is modelled on."""
    latest: dict[str, SyncRun] = {}
    for row in db.scalars(select(SyncRun).order_by(SyncRun.started_at)):
        latest[row.kind] = row
    return latest


def _nightly_runs(now: datetime, rng: random.Random) -> None:
    with session_scope() as db:
        template = _template(db)

        def add(kind: str, started: datetime, status: str = "success",
                summary: str | None = None, triggered_by: str | None = None) -> None:
            model = template.get(kind)
            jitter = 1 + rng.uniform(-0.04, 0.04)
            duration = (
                (_aware(model.finished_at) - _aware(model.started_at)).total_seconds()
                if model is not None and model.finished_at is not None
                else 0
            )
            fallback = next((d for k, _m, d in NIGHTLY if k == kind), 300)
            seconds = max(duration, fallback) * jitter
            seen = int((model.rows_seen if model else 0) * jitter)
            imported = int((model.rows_imported if model else 0) * jitter)
            if kind in _CHANGE_COUNTED:
                # These count what changed, which is a different number every
                # night; the rest count what was read, which barely moves.
                imported = int(imported * rng.uniform(0.2, 1.4))
            dropped = retired = 0
            if status == "partial":
                dropped = int(summary.split()[1]) if summary else 1
            if status == "failed":
                seconds *= rng.uniform(0.2, 0.7)
                seen = int(seen * rng.uniform(0.3, 0.8))
                imported = 0
            elif kind in _RETIRING and rng.random() < 0.6:
                retired = rng.randrange(2, 30)
                reason = ("left the catalogue" if kind == "omnitron_catalogue"
                          else f"{rng.choice(_MARKETS)}: no longer priced or stocked")
                retirement = f"retired {retired} row(s) — {reason}: {retired}"
                summary = f"{summary}; {retirement}" if summary else retirement
            db.add(
                SyncRun(
                    kind=kind,
                    mode="full",
                    started_at=started,
                    finished_at=started + timedelta(seconds=round(seconds)),
                    status=status,
                    rows_seen=seen,
                    rows_imported=imported,
                    rows_dropped=dropped,
                    rows_retired=retired,
                    error_summary=summary,
                    uploaded_by=triggered_by,
                )
            )

        for days_ago in range(DAYS + 1, 1, -1):
            night = (now - timedelta(days=days_ago)).replace(
                hour=1, minute=0, second=0, microsecond=0
            )
            for kind, offset, _seconds in NIGHTLY:
                incident = INCIDENTS.get((days_ago, kind))
                started = night + timedelta(minutes=offset, seconds=rng.randrange(0, 50))
                if incident:
                    add(kind, started, *incident)
                    if incident[0] == "failed":
                        break
                else:
                    add(kind, started)
            for hour in DAYTIME_STOREFRONT:
                started = night.replace(hour=hour, minute=rng.randrange(0, 5))
                incident = INCIDENTS.get((days_ago, f"storefront@{hour}"))
                add("storefront", started, *(incident or ()))
            if days_ago in WEEKLY_PUBLISH:
                started = night.replace(hour=5, minute=0)
                incident = INCIDENTS.get((days_ago, "brandlibrary_publish"))
                add("brandlibrary_publish", started, *(incident or ()), triggered_by="cron")


def _activity(now: datetime, rng: random.Random) -> None:
    """Sign-ins on working days, and the odd bit of housekeeping."""
    with session_scope() as db:
        people = list(db.scalars(select(User).where(User.email.like("%@example.com"))))
        demo = db.scalar(select(User).where(User.email == "demo"))

    rows: list[dict[str, Any]] = []
    last_login: dict[int, datetime] = {}
    for days_ago in range(DAYS, 0, -1):
        day = now - timedelta(days=days_ago)
        if day.weekday() >= 5:
            continue
        for user in people:
            # The inactive account stopped signing in when it was switched off.
            if not user.is_active and days_ago < 12:
                continue
            if rng.random() > (0.8 if user.role != "manager" else 0.35):
                continue
            moment = day.replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(
                minutes=rng.randrange(0, 150), seconds=rng.randrange(0, 60)
            )
            rows.append({"created_at": moment, "user_id": user.id, "user_email": user.email,
                         "action": audit.SIGNED_IN, "method": "POST",
                         "path": "/api/auth/login", "status_code": 200})
            last_login[user.id] = max(last_login.get(user.id, moment), moment)
        if day.weekday() == 0 and demo is not None:
            moment = day.replace(hour=9, minute=40, second=0, microsecond=0)
            rows.append({"created_at": moment, "user_id": demo.id, "user_email": demo.email,
                         "action": audit.REBUILT_EXTRACT, "method": "POST",
                         "path": "/api/coverage/rebuild", "status_code": 200,
                         "detail": "rc=0 in 41.2s; 8 files"})
    # A mistyped address, as every real log has.
    typo = now - timedelta(days=9, hours=2)
    rows.append({"created_at": typo, "user_id": None, "user_email": "bruno.haloway@example.com",
                 "action": audit.SIGN_IN_FAILED, "method": "POST",
                 "path": "/api/auth/login", "status_code": 401})

    with session_scope() as db:
        db.bulk_insert_mappings(AuditLog, rows)  # type: ignore[arg-type]
        for user in db.scalars(select(User)):
            if user.last_login_at is None and user.id in last_login:
                user.last_login_at = last_login[user.id]


def _addresses() -> None:
    """The test client's address replaced by a documentation one per person."""
    with session_scope() as db:
        ids = {u.email: u.id for u in db.scalars(select(User))}
        for row in db.scalars(select(AuditLog)):
            if row.method == "CLI" or row.user_email is None:
                continue
            if row.ip in (None, "testclient"):
                row.ip = f"{_IP_BASE}{10 + (ids.get(row.user_email, 90) % 80)}"
