"""Audit logging — what the developer-only Activity page reads.

`record(...)` writes one `AuditLog` row in its own short-lived session and
**never raises**: auditing must not break the request it is recording. Three
capture points feed it.

- `middleware` in `app.py`, which records every mutating API request.
- Explicit calls where the endpoint knows something the middleware cannot —
  the email behind a failed sign-in, which candidate won a conflict, what a
  user change actually moved.
- The account CLI, which writes the same rows the API does. Without that, the
  page would be a complete-looking record of user administration that silently
  omits every account created the way accounts are actually created.

**Recording is the default and skipping is the exception.** `_SKIP` lists the
POST endpoints that only read — resolving a list of codes, computing a summary
— because a page interaction logged per keystroke buries the row that matters.
Everything else mutating is recorded, labelled if a rule matches and as
`POST /api/whatever` if not. That way a new endpoint lands in the log looking
untidy rather than not landing in it at all, and the untidiness is the prompt
to give it a label.

`friendly_action` turns a (method, path) pair into that label, so the page
reads like "reset a password" rather than "POST /api/users/4/password".
"""

from __future__ import annotations

import re

from fastapi import Request

from ..db import session_scope
from ..logging import log
from ..models import AuditLog, User

# ── The action vocabulary ──────────────────────────────────────────────────
#
# Constants rather than literals at each call site, because the Activity page
# groups by this column: "login failed" and "sign-in failed" written from two
# files are two chips in the filter bar for one event. Past tense throughout —
# an audit row is always a thing that already happened.

SIGNED_IN = "signed in"
SIGN_IN_FAILED = "sign-in failed"
SIGNED_OUT = "signed out"
CREATED_USER = "created an account"
UPDATED_USER = "changed an account"
RESET_PASSWORD = "reset a password"
DECIDED_CONFLICT = "decided a conflict"
SET_VALUE = "set a product value"
STARTED_PUSH = "started a push to Omnitron"
APPLIED_VALUES = "applied a set of values"
DISCARDED_VALUE = "discarded a value"
# "value", not "translation": two of the four proposal sources do not
# translate anything — the grouping row hands down text a person already typed
# on the style, and the shared key fills a market field that is simply empty.
#
# The 11 rows written before 2026-09-23 keep the old wording, because an audit
# row records what happened and rewriting one to match a later vocabulary is
# the one edit an audit log must not make. Activity will show both spellings
# for one action until those age out, which is the cheaper of the two wrongs.
ACCEPTED_PROPOSAL = "accepted a proposed value"
REJECTED_PROPOSAL = "rejected a proposed value"
REBUILT_EXTRACT = "rebuilt the data extract"
TOOK_OFFLINE = "took a product offline"
PUT_BACK_ONLINE = "put a product back online"

#: Actions that mean somebody failed to get in, or was let in. The page pulls
#: these out on their own, because they are the rows read for a different
#: reason than all the others.
ACCESS_ACTIONS: frozenset[str] = frozenset({SIGNED_IN, SIGN_IN_FAILED, SIGNED_OUT})

#: How the CLI marks its rows. It has no HTTP method, and leaving the column
#: null would make a shell action indistinguishable from a middleware row that
#: failed to record one.
CLI_METHOD = "CLI"

# (method, path-pattern) -> friendly label. First match wins.
_ACTION_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("POST", re.compile(r"^/api/users/\d+/password"), RESET_PASSWORD),
    ("POST", re.compile(r"^/api/users"), CREATED_USER),
    ("PATCH", re.compile(r"^/api/users/\d+"), UPDATED_USER),
    ("POST", re.compile(r"^/api/conflicts/decide"), DECIDED_CONFLICT),
    ("POST", re.compile(r"^/api/imports/values"), SET_VALUE),
    ("POST", re.compile(r"^/api/imports/push"), STARTED_PUSH),
    ("POST", re.compile(r"^/api/imports/apply"), APPLIED_VALUES),
    ("POST", re.compile(r"^/api/imports/discard"), DISCARDED_VALUE),
    ("POST", re.compile(r"^/api/imports/proposals/accept"), ACCEPTED_PROPOSAL),
    ("POST", re.compile(r"^/api/imports/proposals/reject"), REJECTED_PROPOSAL),
    ("POST", re.compile(r"^/api/coverage/rebuild"), REBUILT_EXTRACT),
    ("POST", re.compile(r"^/api/coverage/report"), "downloaded a coverage report"),
    ("POST", re.compile(r"^/api/auth/acknowledge_changelog"), "read the release notes"),
    ("POST", re.compile(r"^/api/images/cookie"), "replaced the brand library cookie"),
    ("POST", re.compile(r"^/api/images/publish"), "started an image publish"),
    ("POST", re.compile(r"^/api/images/bundles/\d+/sent"), "sent an image bundle to Turkey"),
    ("POST", re.compile(r"^/api/takedown/restore/\d+"), PUT_BACK_ONLINE),
    ("POST", re.compile(r"^/api/takedown/.+/offline"), TOOK_OFFLINE),
]

#: POSTs that mutate nothing. They are POSTs because they carry a body — a
#: scope, an uploaded file, a pasted block of codes — not because they change
#: anything, and each one fires several times while somebody adjusts a filter.
#:
#: `/api/coverage/report` is deliberately **not** here. It computes the same
#: numbers as `/summary` and changes nothing either, but it puts a file on
#: somebody's disk, and where a spreadsheet came from is a question that gets
#: asked weeks later.
_SKIP: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/coverage/resolve"),
    re.compile(r"^/api/coverage/summary"),
    re.compile(r"^/api/coverage/queue"),
    # A preview computes what an apply *would* write and writes nothing; an
    # upload parses a file into that same preview. The apply is recorded.
    re.compile(r"^/api/imports/preview"),
    re.compile(r"^/api/imports/upload"),
)

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Set on `request.state` by an endpoint that wrote its own row.
_MARK = "_audit_recorded"

#: Set on `request.state` when a request authenticates.
_ACTOR = "_audit_actor"


def attribute(request: Request, user_id: int, user_email: str) -> None:
    """Note who this request authenticated as, for the middleware to record.

    Called from `current_user`, which is the one place that resolves a request
    to a person. The middleware could read the session cookie instead, and did
    at first — but that records what the cookie claimed rather than who the
    request was actually served as, and the two stop agreeing the moment
    authentication gains a second mechanism. It also leaves the email to a
    second lookup, on a table this module does not own.
    """
    setattr(request.state, _ACTOR, (user_id, user_email))


def actor(request: Request) -> tuple[int | None, str | None]:
    """Who to attribute a row to: the authenticated user, else whatever the
    session cookie carries, else nobody. The last case is a real row — an
    unauthenticated attempt at a mutating endpoint is worth seeing."""
    known = getattr(request.state, _ACTOR, None)
    if known is not None:
        return known
    return (request.scope.get("session") or {}).get("user_id"), None


def mark_recorded(request: Request) -> None:
    """Claim this request — the endpoint has written a better row than the
    middleware could.

    An endpoint knows the email behind a sign-in that never authenticated, and
    which fields a change actually moved; the middleware knows a method and a
    path. Rather than list those endpoints somewhere they can fall out of date,
    each one says so on the way past, and the middleware stands down.
    """
    request.state.__setattr__(_MARK, True)


def was_recorded(request: Request) -> bool:
    return bool(getattr(request.state, _MARK, False))


def should_record(method: str, path: str) -> bool:
    """Does this request belong in the log?

    Mutating, under `/api/`, and not one of the read-shaped exceptions.
    """
    if method.upper() not in _MUTATING:
        return False
    if not path.startswith("/api/"):
        return False
    return not any(pattern.search(path) for pattern in _SKIP)


def friendly_action(method: str, path: str) -> str:
    """A human label for a mutating request, falling back to "<METHOD> <path>"
    when no rule matches — see the module docstring for why the fallback is a
    visible row rather than no row."""
    for m, pattern, label in _ACTION_RULES:
        if m == method and pattern.search(path):
            return label
    return f"{method} {path}"


def client_ip(request: Request) -> str | None:
    """Best-effort client IP. uvicorn runs with --proxy-headers, but prefer the
    first X-Forwarded-For hop when present (behind the Docker/edge proxy)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.client.host if request.client else None


def record(
    *,
    action: str,
    user_id: int | None = None,
    user_email: str | None = None,
    method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> None:
    """Persist one audit row. Resolves `user_email` from `user_id` when not
    given. Swallows every error — a failed audit write must never surface to
    the user or abort their action."""
    from datetime import UTC, datetime

    try:
        with session_scope() as db:
            if user_id is not None and not user_email:
                u = db.get(User, user_id)
                if u is not None:
                    user_email = u.email
            db.add(
                AuditLog(
                    created_at=datetime.now(UTC),
                    action=action[:64],
                    user_id=user_id,
                    user_email=user_email,
                    method=method,
                    path=path[:255] if path else None,
                    status_code=status_code,
                    ip=ip,
                    detail=detail,
                )
            )
    except Exception as e:  # noqa: BLE001 — auditing must not break the request
        log.warning("audit record failed", action=action, error=str(e))
