"""Getting into the brand library — which credential, and where it lives.

One small module because the precedence is easy to get wrong in three places
instead of one:

    OAuth client credentials  >  a cookie pasted on the page  >  BRAND_LIBRARY_COOKIE

**OAuth wins outright.** When an id and secret are configured the run signs
itself in and no cookie is consulted at all — that is the whole point of having
them, and quietly preferring a stale pasted cookie would keep a weekly chore
alive for no reason.

**A pasted cookie beats the environment.** `BRAND_LIBRARY_COOKIE` is a
bootstrap: it gets a fresh server going and it is what a developer uses
locally. Once somebody has pasted one on the Images page, falling back to a
months-old `.env` value would send the run at the library with a credential
nobody has looked at since the deploy.

`build` is the one function callers should use. `current` is about the stored
cookie specifically, and is what the page reports on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..models import BrandLibrarySession, utcnow
from .client import BrandLibraryClient

#: The one row. See the model docstring for why there is only ever one.
ROW_ID = 1


@dataclass(frozen=True, slots=True)
class Credential:
    cookie: str
    user_agent: str | None = None
    rotated_at: datetime | None = None
    rotated_by: str | None = None
    last_ok_at: datetime | None = None
    #: True when this came from the environment rather than the page — which
    #: means nobody has rotated it here and it is as old as the deploy.
    from_env: bool = False


def current(db: Session) -> Credential | None:
    """The cookie to use, or None when there is none anywhere."""
    row = db.get(BrandLibrarySession, ROW_ID)
    if row is not None and row.cookie:
        return Credential(
            cookie=row.cookie,
            user_agent=row.user_agent,
            rotated_at=row.rotated_at,
            rotated_by=row.rotated_by,
            last_ok_at=row.last_ok_at,
        )
    if settings.brand_library_cookie:
        return Credential(cookie=settings.brand_library_cookie, from_env=True)
    return None


def rotate(db: Session, cookie: str, user_agent: str | None, by: str | None) -> Credential:
    """Replace the cookie. Upsert, because there is exactly one row."""
    row = db.get(BrandLibrarySession, ROW_ID)
    if row is None:
        row = BrandLibrarySession(id=ROW_ID, cookie=cookie)
        db.add(row)
    row.cookie = cookie
    # Only overwrite the user agent when the paste carried one. A raw cookie
    # header has none, and clearing a working agent because this paste did not
    # mention it would break the session the paste was meant to fix.
    if user_agent:
        row.user_agent = user_agent
    row.rotated_at = utcnow()
    row.rotated_by = by
    # Not yet proven — `note_success` sets this when the library accepts it.
    row.last_ok_at = None
    db.commit()
    return Credential(
        cookie=row.cookie,
        user_agent=row.user_agent,
        rotated_at=row.rotated_at,
        rotated_by=row.rotated_by,
    )


def note_success(db: Session) -> None:
    """Record that the library just accepted the stored cookie.

    Called after a successful read, not after a paste: "pasted this morning"
    and "worked an hour ago" diverge within the day, and the second is the
    question somebody opening the page actually has.
    """
    row = db.get(BrandLibrarySession, ROW_ID)
    if row is None:
        return
    row.last_ok_at = utcnow()
    db.commit()


def oauth_configured() -> bool:
    return bool(settings.brand_library_client_id and settings.brand_library_client_secret)


def build(db: Session) -> BrandLibraryClient:
    """The client to use, with whichever credential is best available.

    Raises `AuthFailed` (from the client) when there is nothing to use, which
    is the one failure here with a human remedy rather than a retry.
    """
    credential = None if oauth_configured() else current(db)
    return BrandLibraryClient(
        cookie=credential.cookie if credential else None,
        client_id=settings.brand_library_client_id,
        client_secret=settings.brand_library_client_secret,
        domain=settings.brand_library_domain,
        user_agent=(credential.user_agent if credential else None)
        or settings.brand_library_user_agent,
        category=settings.brand_library_category,
    )
