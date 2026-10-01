"""The activity log — who did what, to this module, and when.

    GET /api/audit?user_email=&action=&days=&limit=&offset=

Developer-only. One endpoint, because the page is one question asked with
different filters, and splitting the facet counts into a second call would
let the chips and the rows disagree about what window they describe.

**What this log does and does not cover, stated on the page.** It holds
requests that changed something, sign-ins, and the account CLI's writes. It
does not hold the nightly sync — that has its own page, with row counts and a
staleness verdict a generic audit row could not carry — and it does not hold
reads. A page that looks like a complete record of everything that touched the
data, while quietly being a record of one kind of thing, is the same failure
this codebase has hit four times with absent sources.

**The facet counts ignore the filters they offer.** Count over the filtered
rows and picking `sign-in failed` leaves one chip standing, with no way back
except reloading — the filter bar would delete its own alternatives. So the
chips describe the time window, the rows describe the window plus the filters,
and `total` is what the pager needs: how many rows the current filters match.

**`firstAt` is the honest edge of the record.** Empty results from a filter and
empty results because the log started on Tuesday look identical, and the second
one is not evidence about anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AuditLog, User
from . import audit
from .auth import ROLE_DEVELOPER, require_role

router = APIRouter(prefix="/api/audit", tags=["activity"])

Actor = Annotated[User, Depends(require_role(ROLE_DEVELOPER))]
Db = Annotated[Session, Depends(get_session)]

#: One screenful at a time. The cap is what a single request may ask for, not
#: what the table holds — `total` always describes the whole match.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _window_start(days: int) -> datetime | None:
    """`days <= 0` means everything, which is the default: this log is young
    and a window that hides its early rows would be hiding most of it."""
    if days <= 0:
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _in_window(stmt: Select, since: datetime | None) -> Select:
    return stmt if since is None else stmt.where(AuditLog.created_at >= since)


@router.get("")
def activity(
    actor: Actor,
    db: Db,
    user_email: Annotated[str, Query()] = "",
    action: Annotated[str, Query()] = "",
    days: Annotated[int, Query(ge=0, le=3650)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    del actor
    since = _window_start(days)

    filtered = _in_window(select(AuditLog), since)
    if user_email:
        filtered = filtered.where(AuditLog.user_email == user_email.strip().lower())
    if action:
        filtered = filtered.where(AuditLog.action == action)

    total = (
        db.scalar(
            select(func.count()).select_from(filtered.subquery())
        )
        or 0
    )

    rows = db.scalars(
        filtered.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )

    items = [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "user_email": r.user_email,
            "action": r.action,
            "method": r.method,
            "path": r.path,
            "status_code": r.status_code,
            "ip": r.ip,
            "detail": r.detail,
        }
        for r in rows
    ]

    # Facets over the window alone — see the module docstring.
    actions = [
        {"action": name, "count": n}
        for name, n in db.execute(
            _in_window(select(AuditLog.action, func.count()), since)
            .group_by(AuditLog.action)
            .order_by(func.count().desc(), AuditLog.action)
        )
    ]
    users = [
        {"email": email, "count": n}
        for email, n in db.execute(
            _in_window(
                select(AuditLog.user_email, func.count()).where(
                    AuditLog.user_email.is_not(None)
                ),
                since,
            )
            .group_by(AuditLog.user_email)
            .order_by(func.count().desc(), AuditLog.user_email)
        )
    ]

    first_at = db.scalar(select(func.min(AuditLog.created_at)))

    return {
        "total": total,
        "items": items,
        "actions": actions,
        "users": users,
        "windowDays": days,
        # The oldest row in the table, not in the window — "the log starts
        # here" is a fact about the record, and a window is a question about it.
        "firstAt": first_at.isoformat() if first_at else None,
        # Which of these mean somebody got in, or tried to. The page groups
        # them; the names live in `audit.py` so the two cannot drift.
        "accessActions": sorted(audit.ACCESS_ACTIONS),
    }
