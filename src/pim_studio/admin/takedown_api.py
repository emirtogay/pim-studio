"""Taking a product off a market's website from the panel, and putting it back.

Three endpoints, and the first one is the important one. `GET /plan` asks
Omnitron what a takedown would actually delete and hands back the answer for
the confirmation dialog to render: which markets, how many sizes, which
catalogues, and which sizes cannot be acted on. Nobody should be shown a
"take offline" button whose consequences are computed after they press it.

**Every route is aimed at a market group, never a market.** `sync/takedown.py`
derives the groups from `sync/scope.py`, and today there is one: `PL+CZ+HU+SK`.
All four markets are served by catalogue 601, so none can be taken down without
the others (PL and CZ since 2026-09-07, HU and SK since 2026-09-16). Asking for
`PL` alone is refused with the reason rather than quietly widened, because a
caller asking for it holds a belief about the tenant that is wrong and worth
correcting.

Developer-only. This is the one place in the panel that can stop a product
selling, and `require_role` is a cheaper guard than a conversation about who
pressed what.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import integrations
from ..config import settings
from ..db import get_session
from ..logging import log
from ..models import CatalogueRemoval, CatalogueTakedown, User
from ..omnitron.client import OmnitronClient, OmnitronError, ReadOnlyViolation
from ..sync import takedown as domain
from ..sync.scope import MARKETS, markets_for_catalog
from . import audit
from .auth import ROLE_DEVELOPER, require_role

router = APIRouter(prefix="/api/takedown", tags=["takedown"])

Db = Annotated[Session, Depends(get_session)]
Actor = Annotated[User, Depends(require_role(ROLE_DEVELOPER))]

#: What the confirmation box makes somebody type. Not a decoration: the same
#: dialog serves a group of two markets and a group of one, and typing the
#: option code is the step that fails when the wrong row's button was clicked.
CONFIRM_PHRASE = "take offline"


def _client() -> OmnitronClient:
    try:
        return integrations.pim_client()
    except OmnitronError as exc:  # missing base url or credentials
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _require_group(group: str) -> None:
    """Reject a group that cannot exist before an Omnitron client is built.

    Checked first so a request that is wrong on its face gets its 400 on any
    machine. Building the client first answered 503 wherever Omnitron was not
    configured — CI among them — and hid the real problem behind a missing
    credential.
    """
    try:
        domain.catalogs_for_group(domain.group_for(group))
    except domain.TakedownError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _group_payload() -> list[dict[str, Any]]:
    """The groups, with the markets each one covers, for the UI to render.

    Sent rather than assumed on the client, so when the tenant regroups the
    markets — as it did on 2026-09-16, when HU and SK joined PL and CZ on 601 —
    the panel's buttons follow `scope.py` without a frontend release.
    """
    payload = []
    for key, codes in domain.GROUP_BY_KEY.items():
        catalogs = domain.catalogs_for_group(codes)
        payload.append(
            {
                "key": key,
                "markets": list(codes),
                "label": " + ".join(codes),
                "shared": len(codes) > 1,
                "catalogs": list(catalogs),
                # The catalogues more than one of these markets reads — which
                # is a different list from the group's catalogues, and the only
                # one that explains *why* the markets are stuck together. PL
                # and CZ do not share 4 and 40; those are each market's own.
                # Saying they share all three is the kind of wrong that reads
                # as authoritative.
                "sharedCatalogs": [
                    pk for pk in catalogs if len(markets_for_catalog(pk)) > 1
                ],
                "names": [MARKETS[c].name for c in codes],
            }
        )
    return payload


@router.get("/groups")
def groups(user: Actor) -> dict[str, Any]:
    """Which markets can be taken offline together, and which alone."""
    return {
        "groups": _group_payload(),
        "enabled": bool(
            settings.omnitron_writes_enabled and settings.omnitron_takedown_enabled
        ),
        "confirmPhrase": CONFIRM_PHRASE,
    }


@router.get("/{model_color}/plan")
def plan(model_color: str, group: str, user: Actor, db: Db) -> dict[str, Any]:
    """What taking this option offline in this group would delete.

    Read live from Omnitron, not from `catalogue_memberships`: the mirror is
    as old as the last sync, and this is the one operation where acting on a
    stale answer deletes something.
    """
    _require_group(group)
    try:
        with _client() as api:
            proposal = domain.plan(db, api, model_color, group)
    except domain.TakedownError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OmnitronError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "modelColor": proposal.model_color,
        "group": group,
        "markets": list(proposal.markets),
        "shared": len(proposal.markets) > 1,
        "catalogs": list(proposal.catalogs),
        "catalogsHit": list(proposal.catalogs_hit),
        "rowCount": len(proposal.removals),
        "sizeCount": proposal.size_count,
        "unresolvedSkus": proposal.unresolved_skus,
        "isEmpty": proposal.is_empty,
        "enabled": bool(
            settings.omnitron_writes_enabled and settings.omnitron_takedown_enabled
        ),
        "confirmPhrase": CONFIRM_PHRASE,
    }


class TakeOfflineBody(BaseModel):
    group: str
    #: Typed by the person, checked against the option they are on. The click
    #: is not the confirmation; this is.
    confirm: str
    reason: str | None = Field(default=None, max_length=255)


@router.post("/{model_color}/offline")
def take_offline(
    model_color: str,
    body: Annotated[TakeOfflineBody, Body()],
    user: Actor,
    db: Db,
    request: Request,
) -> dict[str, Any]:
    """Delete this option's catalogue rows in one market group.

    The plan is recomputed here rather than taken from the caller. A body that
    carried the rows to delete would be a body that could be edited to delete
    others, and the seconds between the dialog opening and the button being
    pressed are seconds in which the catalogue can change.
    """
    if body.confirm.strip().lower() != CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f"Type {CONFIRM_PHRASE!r} to confirm.",
        )
    _require_group(body.group)
    try:
        with _client() as api:
            proposal = domain.plan(db, api, model_color, body.group)
            if proposal.is_empty:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{proposal.model_color} is not in the "
                        f"{', '.join(proposal.markets)} catalogues — there is "
                        "nothing to remove."
                    ),
                )
            row = domain.record_plan(
                db,
                proposal,
                created_by=user.email,
                created_by_user_id=user.id,
                reason=body.reason,
            )
            db.commit()
            domain.execute(db, api, row)
            db.commit()
    except domain.TakedownError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReadOnlyViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OmnitronError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    audit.record(
        action=audit.TOOK_OFFLINE,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path=str(request.url.path),
        status_code=200,
        ip=audit.client_ip(request),
        detail=(
            f"{row.model_color} in {row.markets} — {row.removed_count} of "
            f"{row.planned_count} catalogue rows removed ({row.status})"
            + (f"; {body.reason}" if body.reason else "")
        ),
    )
    audit.mark_recorded(request)
    log.info(
        "takedown_api_offline",
        takedown=row.id, model_color=row.model_color, markets=row.markets,
        status=row.status, by=user.email,
    )
    return _takedown_payload(db, row)


@router.post("/restore/{takedown_id}")
def restore(
    takedown_id: int, user: Actor, db: Db, request: Request
) -> dict[str, Any]:
    """Put back everything one takedown removed."""
    row = db.get(CatalogueTakedown, takedown_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such takedown.")
    try:
        with _client() as api:
            domain.restore(db, api, row)
            row.restored_by = user.email
            db.commit()
    except domain.TakedownError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReadOnlyViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OmnitronError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    audit.record(
        action=audit.PUT_BACK_ONLINE,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path=str(request.url.path),
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"{row.model_color} in {row.markets} — takedown {row.id} ({row.status})",
    )
    audit.mark_recorded(request)
    return _takedown_payload(db, row)


@router.get("/{model_color}/history")
def history(model_color: str, user: Actor, db: Db) -> dict[str, Any]:
    """Every takedown recorded against this option, newest first."""
    rows = db.scalars(
        select(CatalogueTakedown)
        .where(CatalogueTakedown.model_color == model_color.strip().upper())
        .order_by(CatalogueTakedown.created_at.desc())
    ).all()
    return {"takedowns": [_takedown_payload(db, r) for r in rows]}


def _takedown_payload(db: Session, row: CatalogueTakedown) -> dict[str, Any]:
    removals = db.scalars(
        select(CatalogueRemoval)
        .where(CatalogueRemoval.takedown_id == row.id)
        .order_by(CatalogueRemoval.sku, CatalogueRemoval.catalog_pk)
    ).all()
    return {
        "id": row.id,
        "modelColor": row.model_color,
        "markets": row.markets.split(","),
        "catalogs": [int(c) for c in row.catalogs.split(",") if c],
        "status": row.status,
        "reason": row.reason,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "createdBy": row.created_by,
        "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
        "restoredAt": row.restored_at.isoformat() if row.restored_at else None,
        "restoredBy": row.restored_by,
        "plannedCount": row.planned_count,
        "removedCount": row.removed_count,
        "failedCount": row.failed_count,
        "errorSummary": row.error_summary,
        "canRestore": any(r.state == "removed" for r in removals),
        "sizes": sorted({r.sku for r in removals}),
        "failures": [
            {"sku": r.sku, "catalog": r.catalog_pk, "error": r.error}
            for r in removals
            if r.state == "failed"
        ],
    }
