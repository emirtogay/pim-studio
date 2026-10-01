"""Authentication for the admin panel.

Scope: email + password + server-signed session cookie. Three role tiers:

- `developer`: full access.
- `planning`:  everything except Scrape Runs and Schedules.
- `manager`:   read-only data pages; no admin section at all.

Passwords are hashed with argon2 via passlib. The session cookie is the one
Starlette's `SessionMiddleware` installs on the FastAPI app (see app.py) —
we store only the authenticated user id in it, then load the full `User`
row on each request.

This module is intentionally free of FastAPI router registrations so it can
be imported and used from both the HTTP layer and the CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import User

# Role tiers, ordered most-privileged first. Anything not in this set is treated
# as no-access — a defensive default should the DB ever carry an unknown value.
ROLE_DEVELOPER = "developer"
ROLE_PLANNING = "planning"
ROLE_MANAGER = "manager"
ROLES: tuple[str, ...] = (ROLE_DEVELOPER, ROLE_PLANNING, ROLE_MANAGER)

Role = Literal["developer", "planning", "manager"]

# argon2 is the passlib-recommended modern password hash. Slow-enough defaults
# out of the box; no tuning needed for an internal tool.
_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return an argon2 hash for `password`."""
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of `password` against a stored argon2 hash."""
    try:
        return _pwd_context.verify(password, password_hash)
    except Exception:  # noqa: BLE001 — any passlib failure means auth fails
        return False


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the `User` if email + password match and the account is active.

    Returns None on any mismatch (wrong email, wrong password, disabled
    account) so the caller can't distinguish failure modes from the response.

    **Stamps `last_login_at` and commits it.** This function is the only writer
    of that column and therefore owns its durability — it does not flush and
    hope. It used to, and the consequences were both silent:

    - `get_session` never commits, so the stamp was rolled back at the end of
      every request. The column had never held a value in its life, and a page
      reading it would have said "never" about everybody, permanently.
    - A flush takes a write lock without ending the transaction. `audit.record`
      opens its own session, so the sign-in row it writes was a second
      connection waiting on the first — five seconds on SQLite and then a
      dropped row, which is the one thing an audit log must not do.

    Committing here rather than in the endpoint is deliberate: a caller that
    has to remember is a caller that will eventually forget, and this one
    already did. The cost is a precondition — **call this before staging any
    other work on `db`**, because the commit is the session's, not just this
    row's. At the start of a sign-in there is nothing else staged, which is the
    only place authentication happens.
    """
    if not email or not password:
        return None
    user = (
        db.query(User)
        .filter(User.email == email.strip().lower(), User.is_active.is_(True))
        .one_or_none()
    )
    if user is None:
        # Still compute a hash to avoid leaking existence via timing. Cheap
        # compared to the real path, and passlib handles the comparison safely.
        _pwd_context.dummy_verify()
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(UTC)
    db.add(user)
    db.commit()
    return user


# ---- FastAPI dependencies -------------------------------------------------


def current_user(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
) -> User:
    """Resolve the authenticated user from the session cookie, or 401.

    Use this as the minimum gate on endpoints that shouldn't be world-readable.
    Stricter gates (`require_role`) build on top of it.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        # Session references a deleted or disabled account; clear the session
        # so the client gets redirected cleanly.
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session no longer valid",
        )
    # The one place a request becomes a person, so the one place that can tell
    # the audit middleware who to attribute the row to. Imported here rather
    # than at module scope: `audit` imports the models and the session factory,
    # and this module is deliberately importable from the CLI.
    from . import audit

    audit.attribute(request, user.id, user.email)
    return user


def require_role(*allowed_roles: str):
    """Return a dependency that only allows users whose role is in `allowed_roles`.

    Example::

        @app.get("/api/scrape_runs", dependencies=[Depends(require_role("developer"))])

    403s when the session is valid but the role doesn't match; 401s when the
    session is missing or invalid (delegated to `current_user`).
    """

    def dep(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return dep


# Convenience aliases — readable at the call site.
require_developer = require_role(ROLE_DEVELOPER)
require_planning_or_developer = require_role(ROLE_DEVELOPER, ROLE_PLANNING)
require_any_authenticated = current_user
