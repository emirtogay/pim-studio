"""What may be done to an account, and what may not.

The rules live here rather than in the endpoint because there are two callers,
not one: the panel's Users page and `python -m pim_studio.admin.users`. A rule
enforced in only one of them is not a rule — it is a habit the other caller
does not have.

Three things this module exists to get right.

**Email is normalised on write, because it is normalised on read.**
`authenticate()` looks the account up as `email.strip().lower()`. An account
stored as `Emir@example.com` therefore cannot sign in, ever, and the failure is
"wrong email or password" — indistinguishable from a typo. The CLI has been
able to create exactly that account since the day it shipped.

**`users` is not this module's table.** It is shared with the price-comparison
app (migration `c4a1e77b21f0`), so everything here reaches into Northwind Storefront
too: a role change changes it there, a deactivation signs them out of both.
That is the intended shape — one login, pick a module — but it is the reason
there is no delete. A removed row cannot be undone from the other app's side,
it takes the price panel's account with it, and it orphans every audit row
that referenced the id. `is_active = false` is the off switch: reversible,
and it leaves the history resolvable.

**The panel must stay openable.** Two guards, both of which have exactly one
job — to keep the last way in from being closed by accident:

- Nobody may demote or deactivate their own account. Self-inflicted lockout is
  the single most likely way this panel becomes unreachable, and the person
  doing it is by definition mid-way through something else.
- The last active developer may not be demoted or deactivated by anyone. Roles
  below developer cannot reach this page, so a database client would be the
  only way back.

The CLI is the escape hatch from both, deliberately: it needs a shell on the
server, which is the correct amount of friction for "let me back in".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import User
from .auth import ROLE_DEVELOPER, ROLES, hash_password

#: Matches the CLI's long-standing refusal. An API that accepted six characters
#: would turn the CLI's rule into a suggestion, and the weakest path is the one
#: that decides how strong the policy actually is.
MIN_PASSWORD_LENGTH = 10


class AccountError(ValueError):
    """A rule refused the change. Carries the sentence shown to the user.

    `status` is the HTTP code the router maps it to — 409 for "this would lock
    everyone out", 422 for "this input is not usable" — so the rules keep
    deciding what kind of refusal each one is, rather than the endpoint
    guessing from the message.
    """

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def normalise_email(raw: str) -> str:
    """Lower-case and strip, or refuse. The form `authenticate()` looks up."""
    email = (raw or "").strip().lower()
    if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise AccountError(f"{raw!r} is not an email address.")
    if len(email) > 255:
        raise AccountError("That email address is too long for the column.")
    return email


def validate_password(password: str) -> str:
    """Return the password, or refuse it. The only policy: length."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccountError(
            f"A password needs at least {MIN_PASSWORD_LENGTH} characters."
        )
    return password


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise AccountError(f"{role!r} is not a role. Pick one of {', '.join(ROLES)}.")
    return role


def _clean_name(name: str | None) -> str | None:
    cleaned = (name or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > 128:
        raise AccountError("That name is too long for the column.")
    return cleaned


def active_developers(db: Session, *, excluding: int | None = None) -> int:
    """How many people could still open this page if `excluding` stopped."""
    stmt = select(func.count()).select_from(User).where(
        User.role == ROLE_DEVELOPER, User.is_active.is_(True)
    )
    if excluding is not None:
        stmt = stmt.where(User.id != excluding)
    return db.scalar(stmt) or 0


def _guard_last_developer(db: Session, target: User) -> None:
    """Refuse a change that would leave nobody able to administer the panel.

    Through the API today this is a backstop that never fires: the caller has
    to be an active developer to reach the router at all, and the self-guard
    stops them being their own target, so there is always at least one
    developer left standing. It is kept because *why it cannot happen* lives in
    two other files — a route gate and a separate rule — and either could
    reasonably change. Relaxing the self-guard, or letting `planning` manage
    accounts, would turn a lockout from impossible into one click, in a diff
    that mentions neither. Tested directly for that reason.
    """
    if target.role != ROLE_DEVELOPER or not target.is_active:
        return
    if active_developers(db, excluding=target.id) == 0:
        raise AccountError(
            f"{target.email} is the last active developer. Promote someone "
            "else first — no other role can reach this page.",
            status=409,
        )


def _guard_self(actor_id: int | None, target: User, what: str) -> None:
    if actor_id is not None and actor_id == target.id:
        raise AccountError(
            f"You cannot {what} your own account. Ask another developer, or "
            "use `python -m pim_studio.admin.users` on the server.",
            status=409,
        )


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise AccountError(f"No account with id {user_id}.", status=404)
    return user


def list_users(db: Session) -> list[User]:
    """Everyone, active first, then alphabetically. Disabled accounts are
    listed rather than hidden — an account nobody can see is an account nobody
    reviews, and the reason to keep the row instead of deleting it is that it
    stays visible."""
    return list(
        db.scalars(select(User).order_by(User.is_active.desc(), User.email))
    )


def create_user(
    db: Session,
    *,
    email: str,
    password: str,
    role: str,
    name: str | None = None,
) -> User:
    """Add an account. Refuses a duplicate email in either casing."""
    address = normalise_email(email)
    validate_password(password)
    validate_role(role)

    if db.scalar(select(User).where(User.email == address)) is not None:
        raise AccountError(
            f"{address} already has an account. Reset its password instead.",
            status=409,
        )

    user = User(
        email=address,
        password_hash=hash_password(password),
        role=role,
        name=_clean_name(name),
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    return user


def update_user(
    db: Session,
    user: User,
    *,
    actor_id: int | None,
    email: str | None = None,
    name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    force: bool = False,
) -> list[str]:
    """Apply a partial change, returning a description of what moved.

    The return value is what the audit row records. "Updated a user" says
    nothing a week later; "role developer → planning" is the entry somebody can
    actually check against what they remember approving.

    `force` lifts both lockout guards, and only the CLI passes it. The guards
    exist so the panel cannot be closed from inside the panel; a guard with no
    way past it would make the lockout permanent instead of merely difficult,
    which is the opposite of the point. Requiring a shell on the server is the
    correct amount of friction for "let me back in".
    """
    changes: list[str] = []

    if role is not None and role != user.role:
        validate_role(role)
        if not force:
            _guard_self(actor_id, user, "change the role of")
            if role != ROLE_DEVELOPER:
                _guard_last_developer(db, user)
        changes.append(f"role {user.role} → {role}")
        user.role = role

    if is_active is not None and is_active != user.is_active:
        if not force:
            _guard_self(actor_id, user, "deactivate" if not is_active else "reactivate")
            if not is_active:
                _guard_last_developer(db, user)
        changes.append("reactivated" if is_active else "deactivated")
        user.is_active = is_active

    if email is not None:
        address = normalise_email(email)
        if address != user.email:
            clash = db.scalar(select(User).where(User.email == address))
            if clash is not None:
                raise AccountError(f"{address} already has an account.", status=409)
            changes.append(f"email {user.email} → {address}")
            user.email = address

    if name is not None:
        cleaned = _clean_name(name)
        if cleaned != user.name:
            changes.append(f"name {user.name or '—'} → {cleaned or '—'}")
            user.name = cleaned

    if changes:
        db.add(user)
        db.flush()
    return changes


def set_password(db: Session, user: User, password: str) -> None:
    """Replace a password. Deliberately not part of `update_user`: everything
    there is describable in an audit row, and this is the one field whose value
    must never appear in one."""
    validate_password(password)
    user.password_hash = hash_password(password)
    db.add(user)
    db.flush()


def serialise(user: User) -> dict[str, Any]:
    """The wire shape. snake_case because `AdminUser` extends `AuthUser` in
    `api.ts`, and `/api/auth/me` has emitted `changelog_unread` since the first
    release — one of the two had to bend and it is not the one already shipped.

    No `password_hash`, obviously, and no field is added here without asking
    whether a manager reading over a developer's shoulder should see it.
    """
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_active": bool(user.is_active),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
