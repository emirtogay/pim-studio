"""Account endpoints — who can open the panel, and as what.

    GET   /api/users                 everyone, active first
    POST  /api/users                 add an account
    PATCH /api/users/{id}            name, email, role, active
    POST  /api/users/{id}/password   set a new password

Developer-only, all four. `planning` cannot reach this router at all, which is
the same line the sidebar draws — a page that hides a control the API would
have honoured is a page that teaches people the wrong thing about their own
access.

The rules are in `accounts.py`, not here, because the CLI enforces the same
ones. This module is the HTTP shape around them: parse, call, audit, serialise.
`AccountError` carries its own status code, so a refusal keeps the distinction
between "that input is unusable" (422) and "that would lock everyone out"
(409) rather than flattening both into 400.

**There is no delete.** `users` is shared with the price-comparison app, so a
removed row takes that account with it and orphans every audit row pointing at
the id. Deactivation is the off switch: same effect on sign-in, reversible from
either module, and the history stays readable. See `accounts.py`.

Every write records its own audit row and marks the request, so the middleware
in `app.py` stands down rather than adding a second, vaguer one. The detail is
the point — "changed an account" a week later is worth much less than
"role developer → planning".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import User
from . import accounts, audit
from .auth import ROLE_DEVELOPER, ROLES, require_role

router = APIRouter(prefix="/api/users", tags=["users"])

Actor = Annotated[User, Depends(require_role(ROLE_DEVELOPER))]
Db = Annotated[Session, Depends(get_session)]


def _fail(exc: accounts.AccountError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


class CreateBody(BaseModel):
    email: str
    password: str
    role: str = "planning"
    name: str | None = None


class PatchBody(BaseModel):
    """Every field optional — absent means "leave it alone".

    `None` and absent have to stay distinguishable for `name`, which is
    genuinely nullable: `{"name": null}` clears it, `{}` does not touch it.
    Pydantic gives that for free only if the sentinel is not None, hence the
    `model_fields_set` check in the handler rather than a truthiness test.
    """

    email: str | None = None
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None


class PasswordBody(BaseModel):
    password: str = Field(min_length=1)


@router.get("")
def list_users(actor: Actor, db: Db) -> list[dict[str, Any]]:
    """Everyone with an account, disabled ones included and marked.

    Not paginated: this is a handful of named people, and it is supposed to
    stay that way. If it ever needs a page control, something has gone wrong
    that a page control would hide.
    """
    del actor
    return [accounts.serialise(u) for u in accounts.list_users(db)]


@router.get("/roles")
def roles(actor: Actor) -> dict[str, Any]:
    """What each tier can actually do, from the same tuple `auth.py` gates on.

    The panel needs to explain the choice at the point somebody makes it, and
    a description typed into the frontend is a description that goes stale the
    first time a route is gated differently.
    """
    del actor
    blurbs = {
        "developer": "Everything, including this page, sync runs and the activity log.",
        "planning": "Products, coverage, imports and the conflict queue. No admin tools.",
        "manager": "Read-only: products and coverage. No admin section at all.",
    }
    return {"roles": [{"key": r, "blurb": blurbs.get(r, "")} for r in ROLES]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(body: CreateBody, request: Request, actor: Actor, db: Db) -> dict[str, Any]:
    try:
        user = accounts.create_user(
            db,
            email=body.email,
            password=body.password,
            role=body.role,
            name=body.name,
        )
    except accounts.AccountError as exc:
        raise _fail(exc) from exc

    db.commit()
    audit.mark_recorded(request)
    audit.record(
        action=audit.CREATED_USER,
        user_id=actor.id,
        user_email=actor.email,
        method="POST",
        path="/api/users",
        status_code=201,
        ip=audit.client_ip(request),
        detail=f"{user.email} as {user.role}",
    )
    return accounts.serialise(user)


@router.patch("/{user_id}")
def update_user(
    user_id: int, body: PatchBody, request: Request, actor: Actor, db: Db
) -> dict[str, Any]:
    sent = body.model_fields_set
    try:
        user = accounts.get_user(db, user_id)
        changes = accounts.update_user(
            db,
            user,
            actor_id=actor.id,
            email=body.email if "email" in sent else None,
            # Passed through as-is when sent, so `null` clears the name rather
            # than reading as "not sent" — `accounts._clean_name` maps empty
            # and None alike to None.
            name=(body.name or "") if "name" in sent else None,
            role=body.role if "role" in sent else None,
            is_active=body.is_active if "is_active" in sent else None,
        )
    except accounts.AccountError as exc:
        db.rollback()
        raise _fail(exc) from exc

    if not changes:
        # Nothing moved. No row: an audit log where half the entries record
        # somebody opening a form and closing it is one nobody reads. Claimed
        # all the same — otherwise the middleware writes the row this just
        # decided not to, and the decision has no effect at all.
        audit.mark_recorded(request)
        return accounts.serialise(user)

    db.commit()
    audit.mark_recorded(request)
    audit.record(
        action=audit.UPDATED_USER,
        user_id=actor.id,
        user_email=actor.email,
        method="PATCH",
        path=f"/api/users/{user_id}",
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"{user.email}: {', '.join(changes)}",
    )
    return accounts.serialise(user)


@router.post("/{user_id}/password")
def set_password(
    user_id: int, body: PasswordBody, request: Request, actor: Actor, db: Db
) -> dict[str, bool]:
    """Set someone's password, including your own.

    Not guarded against self like role and active are: setting your own
    password is the one self-directed change that cannot lock you out, because
    you chose the replacement.

    The session is left alone deliberately. Starlette's cookie carries a user
    id and nothing derived from the hash, so a reset does not sign the person
    out — which is right when a developer is helping someone who is standing
    next to them, and worth knowing when the reason for the reset is that the
    old password leaked. That case needs `is_active` off and back on.
    """
    try:
        user = accounts.get_user(db, user_id)
        accounts.set_password(db, user, body.password)
    except accounts.AccountError as exc:
        db.rollback()
        raise _fail(exc) from exc

    db.commit()
    audit.mark_recorded(request)
    audit.record(
        action=audit.RESET_PASSWORD,
        user_id=actor.id,
        user_email=actor.email,
        method="POST",
        path=f"/api/users/{user_id}/password",
        status_code=200,
        ip=audit.client_ip(request),
        # The address, never the password, and never a hint at its shape.
        detail=f"for {user.email}"
        + (" (their own)" if user.id == actor.id else ""),
    )
    return {"ok": True}
