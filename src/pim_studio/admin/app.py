"""The FastAPI application — API and SPA from one origin.

`docker/entrypoint.sh` runs `alembic upgrade head` then
`uvicorn pim_studio.admin.app:app`, so this module's absence was the single
reason the container could not start.

Deliberately thin, and deliberately split by domain rather than growing into
one file: the price-comparison app's equivalent reached 3,431 lines and the
2026-08-13 decision was explicitly not to inherit that shape.

**Products are read from the static extract, not from the database.** The
generator (`scripts/build_demo_data_from_db.py`) already produces exactly the
JSON the frontend wants, and serving it directly means the beta ships without
a query layer that would need designing, paginating and indexing first. The
endpoints below are the seam: when live queries land, `products.json` stops
being read and the URLs do not move.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .. import changelog
from ..config import settings
from ..db import get_session
from ..logging import configure_logging, log
from ..models import (
    Attribute,
    ModuleUserState,
    Product,
    SyncRun,
    User,
    ValueConflict,
    ValueRetirement,
    utcnow,
)
from ..sync.runs import (
    MODE_FULL,
    RECONCILED_KINDS,
    reconciliation_overdue,
    stale_after_hours,
)
from ..sync.writer import upsert_attribute_values
from . import audit
from .activity_api import router as activity_router
from .auth import authenticate, current_user
from .coverage_api import router as coverage_router
from .history_api import router as history_router
from .images_api import router as images_router
from .imports_api import router as imports_router
from .imports_api import values_router as product_values_router
from .takedown_api import router as takedown_router
from .users_api import router as users_router

STATIC_DIR = Path(__file__).parent / "static"
DEMO_DIR = STATIC_DIR / "demo"

configure_logging()

app = FastAPI(title="PIM Studio", version=changelog.current_version())
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    # The reverse proxy terminates TLS, so the cookie is only ever sent over
    # HTTPS in production — but marking it secure locally would break http
    # development entirely, so it follows the session key instead of a guess.
    https_only=False,
)


# ── the audit middleware ───────────────────────────────────────────────────


@app.middleware("http")
async def record_mutations(request: Request, call_next: Any) -> Response:
    """Record every request that changed something.

    `audit.py` has described this middleware since it was ported and it was
    never written, which made the Activity page a complete-looking record of
    sign-ins and nothing else. That is the failure mode this codebase keeps
    naming: an absent source reading as an empty one.

    Everything is read *after* `call_next`, not before, and it has to be:
    this middleware sits outside `SessionMiddleware` and outside the route, so
    before the call there is no status code, and nothing has yet resolved the
    request to a person. `audit.actor` reads what `current_user` left behind
    (falling back to the session cookie for an endpoint that never
    authenticated), so a row is attributed to whoever the request was actually
    served as rather than to whoever the cookie claimed.

    Endpoints that know more than a method and a path claim the request with
    `audit.mark_recorded` and this stands down. The write goes to a threadpool
    because `audit.record` opens its own synchronous session, and a page that
    stalls the event loop on every mutation to write its own history is a
    page paying for the wrong thing.
    """
    method = request.method
    path = request.url.path

    async def write(status_code: int, detail: str | None = None) -> None:
        if not audit.should_record(method, path) or audit.was_recorded(request):
            return
        user_id, user_email = audit.actor(request)
        await run_in_threadpool(
            audit.record,
            action=audit.friendly_action(method, path),
            user_id=user_id,
            user_email=user_email,
            method=method,
            path=path,
            status_code=status_code,
            ip=audit.client_ip(request),
            detail=detail,
        )

    try:
        response: Response = await call_next(request)
    except Exception:
        # An unhandled error is exactly the request somebody comes here
        # looking for, so it is recorded before it carries on to the 500.
        await write(500, "the request raised and did not complete")
        raise

    await write(response.status_code)
    return response


# ── auth ───────────────────────────────────────────────────────────────────


class LoginBody(BaseModel):
    email: str
    password: str


def _seen_version(db: Session, user_id: int) -> str | None:
    """What release notes this user has read **in this module**.

    Not a column on `users`: that table is shared with the price app, and both
    modules have their own changelog. A shared column means reading one app's
    notes clears the other's unread flag — the bell goes dark on releases
    nobody saw, silently.
    """
    row = db.scalar(select(ModuleUserState).where(ModuleUserState.user_id == user_id))
    return row.last_seen_changelog_version if row else None


def _user_payload(user: User, db: Session) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "changelog_unread": changelog.is_unread(_seen_version(db, user.id)),
    }


@app.post("/api/auth/login", tags=["auth"])
def login(
    body: LoginBody,
    request: Request,
    db: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    # Claimed either way: the middleware sees a POST and a status code, this
    # endpoint sees which address was tried, which is the entire value of a
    # failed sign-in row.
    audit.mark_recorded(request)
    user = authenticate(db, body.email, body.password)
    if user is None:
        audit.record(
            action=audit.SIGN_IN_FAILED,
            # Recorded exactly as typed, not normalised: "emir@example.com" and
            # "emir@examp1e.com" are the same row once normalised, and the
            # difference between them is the reason somebody is reading this.
            user_email=body.email,
            method="POST",
            path="/api/auth/login", status_code=401, ip=audit.client_ip(request),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    request.session["user_id"] = user.id
    # No commit here: `authenticate` owns the `last_login_at` write and has
    # already committed it, which is what leaves this session clean and lets
    # the audit write below open its own without waiting on a lock this
    # request is holding. See `auth.authenticate` for what happened when that
    # was the endpoint's job to remember.
    audit.record(
        action=audit.SIGNED_IN, user_id=user.id, method="POST",
        path="/api/auth/login", status_code=200, ip=audit.client_ip(request),
    )
    return {"user": _user_payload(user, db)}


@app.post("/api/auth/logout", tags=["auth"])
def logout(request: Request) -> dict[str, bool]:
    # Read before the session is cleared — afterwards there is nobody to
    # attribute the row to, and an unattributed sign-out is not worth a row.
    user_id = request.session.get("user_id")
    request.session.clear()
    audit.mark_recorded(request)
    if user_id:
        audit.record(
            action=audit.SIGNED_OUT, user_id=user_id, method="POST",
            path="/api/auth/logout", status_code=200, ip=audit.client_ip(request),
        )
    return {"ok": True}


@app.get("/api/auth/me", tags=["auth"])
def me(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    return {"user": _user_payload(user, db)}


@app.post("/api/auth/acknowledge_changelog", tags=["auth"])
def acknowledge_changelog(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    row = db.scalar(select(ModuleUserState).where(ModuleUserState.user_id == user.id))
    if row is None:
        row = ModuleUserState(user_id=user.id)
        db.add(row)
    row.last_seen_changelog_version = changelog.current_version()
    db.commit()
    return {"user": _user_payload(user, db)}


# ── version and release notes ──────────────────────────────────────────────


@app.get("/api/version", tags=["version"])
def get_version() -> dict[str, str]:
    return {"version": changelog.current_version()}


@app.get("/api/changelog", tags=["version"])
def get_changelog() -> dict[str, Any]:
    return changelog.payload()


# Coverage lives in its own router — the shape TEMPLATE.md settled on before
# the first commit, and the one the price app's 3,431-line app.py is the
# argument for. Registered here rather than defined above so this file stays
# the composition root and stops being where every endpoint lands.
app.include_router(coverage_router)

# Who may open the panel, and what everyone has done to it. Both developer-only,
# both split out for the same reason as coverage — see `users_api.py`.
app.include_router(users_router)
app.include_router(activity_router)
app.include_router(images_router)
app.include_router(history_router)
# Values set from the panel, and the live overlay the product page reads.
app.include_router(imports_router)
app.include_router(product_values_router)
app.include_router(takedown_router)


# ── products ───────────────────────────────────────────────────────────────


def _load(name: str) -> Any:
    path = DEMO_DIR / name
    if not path.exists():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{name} has not been generated yet — run "
            "scripts/build_demo_data_from_db.py",
        )
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/products", tags=["products"])
def list_products(user: Annotated[User, Depends(current_user)]) -> Response:
    # Served as a pre-encoded file rather than parsed and re-serialised: it is
    # several megabytes and nothing here needs to look inside it.
    del user
    path = DEMO_DIR / "products.json"
    if not path.exists():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "not generated yet")
    return FileResponse(path, media_type="application/json")


@app.get("/api/products/{model_color}", tags=["products"])
def product_detail(
    model_color: str, user: Annotated[User, Depends(current_user)]
) -> JSONResponse:
    del user
    details = _load("details.json")
    found = details.get(model_color)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No product {model_color}")
    return JSONResponse(found)


# The two panels share an account but not an origin — there is no reverse
# proxy, so they are told apart by port. Both apps serve this same shape so
# their chooser components stay identical; `current` is what each one calls
# itself, which is how the page knows which card to mark as "you are here".
#
# A blank URL means "not configured on this host": the chooser hides that card
# rather than offering a link that goes nowhere.
MODULES = (
    {
        "key": "price-comparison",
        "name": "Northwind Storefront",
        "blurb": "Competitor prices across the Polish market, and how they compare to ours.",
    },
    {
        "key": "product-management",
        "name": "PIM Studio",
        "blurb": (
            "What every product should have, what it actually has, and what is "
            "missing before it can go live."
        ),
    },
)


@app.get("/api/modules", tags=["modules"])
def modules(user: Annotated[User, Depends(current_user)]) -> dict[str, object]:
    urls = {
        "price-comparison": settings.price_app_url,
        "product-management": settings.product_management_url,
    }
    return {
        "current": "product-management",
        "modules": [
            {**m, "url": urls.get(m["key"], "")}
            for m in MODULES
            if urls.get(m["key"])
        ],
    }


def _styles_without_a_colour(db: Session) -> set[str]:
    """Keys that look like an option and are a whole style.

    `model_color` is parsed from Omnitron's `integration_base_code_format`.
    For 87 styles that field arrives as the bare base code — `TH5038`, no
    colour, no size — so every SKU of every colour parses to the same
    `model_color`, and `size` comes out NULL on all of them. Measured
    2026-09-23: 1,445 SKUs, 1.5% of the catalogue, and the test is exact —
    zero SKUs have `model_color == base_code` while carrying a size.

    It matters here more than anywhere because a conflict *is* a comparison
    across an option's own SKUs. On `TH5038` that compares 192 SKUs spanning
    ten colours, and `color_code` alone offers 28 candidates — which is not a
    product disagreeing with itself, it is ten products being asked to agree.
    """
    return set(
        db.scalars(
            select(Product.model_color)
            .where(Product.model_color == Product.base_code)
            .distinct()
        )
    )


@app.get("/api/conflicts", tags=["conflicts"])
def conflicts(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_session)],
    limit: int = 100,
    offset: int = 0,
) -> dict[str, object]:
    """ModelColors the value importer refused to guess at.

    These are abstentions, not missing data: the product's own SKUs disagreed
    and no majority was trustworthy enough to write. Surfacing them is the
    whole point of recording them - an abstention nobody can list reads
    exactly like an absence.

    **Conflicts on a style rather than an option are counted and not listed.**
    See `_styles_without_a_colour`. They are not reviewable: every candidate
    is the right answer for some colour, so there is no winner to pick, and
    picking one would write one colour's name across all of them. They are
    reported as their own number rather than dropped, because an excluded row
    nobody can count reads exactly like a row that was never detected — the
    failure this endpoint's own docstring is about.
    """
    collapsed = _styles_without_a_colour(db)
    real = ~ValueConflict.entity_key.in_(collapsed) if collapsed else true()

    total = db.scalar(select(func.count()).select_from(ValueConflict).where(real)) or 0
    excluded = db.scalar(
        select(func.count()).select_from(ValueConflict).where(~real)
    ) or 0
    excluded_options = db.scalar(
        select(func.count(func.distinct(ValueConflict.entity_key))).where(~real)
    ) or 0
    by_attribute = [
        {"attribute": key, "count": n}
        for key, n in db.execute(
            select(Attribute.key, func.count())
            .join(ValueConflict, ValueConflict.attribute_id == Attribute.id)
            .where(real)
            .group_by(Attribute.key)
            .order_by(func.count().desc())
        )
    ]
    rows = [
        {
            "modelColor": c.entity_key,
            "attribute": key,
            "market": c.market,
            "skuCount": c.sku_count,
            "sources": c.sources,
            "candidates": json.loads(c.candidates),
            "resolvedValue": c.resolved_value,
            "resolvedRule": c.resolved_rule,
        }
        for c, key in db.execute(
            select(ValueConflict, Attribute.key)
            .join(Attribute, Attribute.id == ValueConflict.attribute_id)
            .where(real)
            .order_by(ValueConflict.entity_key, Attribute.key, ValueConflict.market)
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
    ]
    return {
        "total": total,
        "byAttribute": by_attribute,
        "rows": rows,
        "notAnOption": {"rows": excluded, "styles": excluded_options},
    }


class ConflictDecision(BaseModel):
    modelColor: str
    attribute: str
    market: str
    value: str


@app.post("/api/conflicts/decide", tags=["conflicts"])
def decide_conflict(
    body: ConflictDecision,
    request: Request,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    """Record a person's choice for one conflicted value.

    Writes it at `approved`, which the sync writer is forbidden to overwrite
    (`PROTECTED_STATES`), so tonight's run reports divergence rather than
    reverting the decision. The queue row goes at the same time: the question
    has been answered and re-asking it is how a queue stops being trusted.
    """
    if user.role not in ("developer", "planning"):
        raise HTTPException(status_code=403, detail="Not allowed to decide conflicts")

    # A style carries no decidable conflict, and deciding one here would be
    # worse than leaving it: the write lands against the collapsed key, so the
    # chosen colour's name would be stamped on every colour of the style. The
    # listing already excludes these (`_styles_without_a_colour`); this is the
    # half that matters, because the listing is a convenience and this is the
    # door.
    if body.modelColor in _styles_without_a_colour(db):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{body.modelColor} is a style, not a single colour — Omnitron "
                "supplies no colour in its base code format, so its SKUs span "
                "several colours. There is no one value to choose."
            ),
        )

    attribute = db.scalar(select(Attribute).where(Attribute.key == body.attribute))
    if attribute is None:
        raise HTTPException(status_code=404, detail=f"Unknown attribute {body.attribute}")

    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Value cannot be empty")

    # Through the writer, not a bare UPDATE: a decision is a change to a
    # value and belongs in `value_changes` with the person's name on it, which
    # a direct assignment silently skipped. `protect=False` because the
    # protection is against the sync overwriting a person, and this is one.
    upsert_attribute_values(
        db,
        [
            {
                "entity_kind": "model_color",
                "entity_key": body.modelColor,
                "attribute_id": attribute.id,
                "market": body.market,
                "state_lane": "current",
                "value": value,
                "state": "approved",
                "entry_grain": "model_color",
                "source_kind": "human",
                "source_ref": f"conflict decision:{user.email}",
                "updated_by": user.email,
            }
        ],
        protect=False,
        changed_by=user.email,
    )

    db.query(ValueConflict).filter_by(
        entity_key=body.modelColor,
        attribute_id=attribute.id,
        market=body.market,
    ).delete()
    db.commit()

    audit.mark_recorded(request)
    audit.record(
        action=audit.DECIDED_CONFLICT,
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path="/api/conflicts/decide",
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"{body.modelColor} {body.attribute} [{body.market}] = {value[:120]}",
    )
    return {"ok": True}


# ── sync runs ──────────────────────────────────────────────────────────────


#: A sync is expected nightly, so this is one missed run plus room for a late
#: one. Beyond it, what the panel shows is yesterday's answer to today's
#: question and the page should say so rather than looking healthy.
_SYNC_STALE_HOURS = 36


def _age_hours(moment: Any) -> float | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        from datetime import UTC

        moment = moment.replace(tzinfo=UTC)
    return (utcnow() - moment).total_seconds() / 3600


#: How long a row may sit at `running` before it is called dead rather than
#: busy. See the note in `sync_runs` — this is a floor on a warning, not a
#: timeout on a job, and nothing acts on it but the page.
_RUN_STUCK_HOURS = 3


@app.get("/api/sync/runs", tags=["ops"])
def sync_runs(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_session)],
    limit: int = 50,
) -> dict[str, object]:
    """Every ingest, newest first, plus whether the data is actually current.

    The runs table has been real since 2026-08-14 and there has been no way to
    look at it without a database client. That is the gap this closes: the
    nightly sync could stop for a week and the panel would carry on serving the
    same numbers with the same confidence, because a stale answer and a fresh
    one look identical once they are rendered.

    Two different staleness questions, and the page has to answer both:

    - **When did anything last succeed?** Past `_SYNC_STALE_HOURS` the data is
      simply old.
    - **When did we last reconcile?** A chain of green incremental nights can
      hide months of unrecorded departures, because only a full pass may
      remove anything. `reconciliation_overdue` is the shared answer — the CLI
      prints the same verdict from the same function, so the panel and the
      terminal cannot drift apart.
    """
    del user
    rows = [
        {
            "id": r.id,
            "kind": r.kind,
            "mode": r.mode,
            "status": r.status,
            "startedAt": r.started_at.isoformat() if r.started_at else None,
            "finishedAt": r.finished_at.isoformat() if r.finished_at else None,
            "durationSeconds": (
                round((r.finished_at - r.started_at).total_seconds())
                if r.started_at and r.finished_at
                else None
            ),
            "rowsSeen": r.rows_seen,
            "rowsImported": r.rows_imported,
            "rowsDropped": r.rows_dropped,
            "rowsRetired": r.rows_retired,
            "sourceFile": r.source_file,
            "errorSummary": r.error_summary,
            "triggeredBy": r.uploaded_by,
        }
        for r in db.scalars(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(min(limit, 200))
        )
    ]

    # Per kind: the newest run, and the newest that actually read everything.
    kinds: list[dict[str, Any]] = []
    for (kind,) in db.execute(select(SyncRun.kind).distinct().order_by(SyncRun.kind)):
        latest = db.scalar(
            select(SyncRun)
            .where(SyncRun.kind == kind)
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        )
        newest_ok = db.scalar(
            select(SyncRun.started_at)
            .where(SyncRun.kind == kind, SyncRun.status.in_(("success", "partial")))
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        )
        newest_full = db.scalar(
            select(SyncRun.started_at)
            .where(
                SyncRun.kind == kind,
                SyncRun.mode == MODE_FULL,
                SyncRun.status.in_(("success", "partial")),
            )
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        )
        age = _age_hours(newest_ok)
        # Per kind, not one global number. A weekly job judged against the
        # nightly threshold reports stale six days out of seven, and a badge
        # that is always red teaches people to stop reading it.
        threshold = stale_after_hours(kind)
        kinds.append(
            {
                "kind": kind,
                "lastStatus": latest.status if latest else None,
                "lastMode": latest.mode if latest else None,
                "lastSuccessAt": newest_ok.isoformat() if newest_ok else None,
                "lastFullAt": newest_full.isoformat() if newest_full else None,
                "ageHours": round(age, 1) if age is not None else None,
                # No threshold means nothing schedules this kind (the push), so
                # its age is when somebody last used it and never a fault. A
                # failed push still shows, as the card's status.
                "stale": threshold is not None and (age is None or age > threshold),
                "staleAfterHours": threshold,
                "needsReconcile": kind in RECONCILED_KINDS,
            }
        )

    overdue = [
        {"kind": kind, "days": days} for kind, days in reconciliation_overdue()
    ]
    # A row is only "never finished" once it has been running longer than any
    # stage legitimately takes. Without the floor this listed every run that
    # was in progress at the moment of the request, under a heading that says
    # in as many words "not one still working" — so the page called a healthy
    # crawl a dead process, and did it more often the more often things ran.
    #
    # The longest single stage measured on this tenant is `omnitron_liveness`
    # at ~35 minutes (01:30:22 to 02:05:00, 2026-09-23); the whole nightly is
    # a row per stage, not one long row. Three hours is therefore several
    # times the worst case and still catches a genuinely stuck row the same
    # morning.
    cutoff = utcnow() - timedelta(hours=_RUN_STUCK_HOURS)
    unfinished = [
        {"id": r.id, "kind": r.kind, "startedAt": r.started_at.isoformat()}
        for r in db.scalars(
            select(SyncRun)
            .where(SyncRun.status == "running")
            .where(SyncRun.started_at < cutoff)
        )
    ]

    return {
        "runs": rows,
        "kinds": kinds,
        "reconciliationOverdue": overdue,
        "unfinished": unfinished,
        "staleAfterHours": _SYNC_STALE_HOURS,
    }


@app.get("/api/sync/retirements", tags=["ops"])
def sync_retirements(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_session)],
    limit: int = 200,
) -> dict[str, object]:
    """What went dark — products whose price or stock upstream no longer has.

    The question this answers is "which products stopped being sellable, and
    when". Before `value_retirements` existed the sync could report that it had
    removed 500 rows and nothing could say which 500, so the number was a
    notification rather than a queue.

    Grouped by ModelColor because that is what somebody opens. One product
    losing stock in four markets is one thing to look at, not four rows — but
    the markets and fields are carried so the row says what actually happened.
    """
    del user
    rows = db.execute(
        select(
            ValueRetirement.entity_key,
            Product.model_color,
            Attribute.key,
            ValueRetirement.market,
            ValueRetirement.last_value,
            ValueRetirement.retired_at,
            ValueRetirement.sync_run_id,
        )
        .join(Attribute, Attribute.id == ValueRetirement.attribute_id)
        .outerjoin(Product, Product.sku == ValueRetirement.entity_key)
        .order_by(ValueRetirement.retired_at.desc(), ValueRetirement.id.desc())
        .limit(min(limit, 1000))
    ).all()

    grouped: dict[str, dict[str, Any]] = {}
    for sku, model_color, attr, market, last_value, retired_at, run_id in rows:
        # A retirement whose product we no longer hold still happened; key on
        # the sku so it is visible rather than dropped for want of a join.
        key = model_color or sku
        entry = grouped.setdefault(
            key,
            {
                "modelColor": model_color,
                "sku": sku,
                "markets": set(),
                "fields": set(),
                "lastValues": {},
                "retiredAt": retired_at.isoformat() if retired_at else None,
                "syncRunId": run_id,
                "skuCount": set(),
            },
        )
        entry["markets"].add(market)
        entry["fields"].add(attr)
        entry["skuCount"].add(sku)
        entry["lastValues"].setdefault(f"{market} {attr}", last_value)

    items = [
        {
            **e,
            "markets": sorted(e["markets"]),
            "fields": sorted(e["fields"]),
            "skuCount": len(e["skuCount"]),
        }
        for e in grouped.values()
    ]

    total = db.scalar(select(func.count()).select_from(ValueRetirement)) or 0
    return {"total": total, "shown": len(rows), "products": items}


@app.get("/api/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": changelog.current_version()}


# ── the SPA ────────────────────────────────────────────────────────────────
#
# Mounted last so every /api route above wins. The catch-all returns
# index.html for unknown paths because the router is client-side: a hard
# refresh on /products/L3539.132 must serve the app, not a 404.

#: Store it, but ask before reusing it. Not `no-store` — the etag still turns
#: an unchanged file into a 304, which matters for a multi-megabyte extract.
REVALIDATE = {"cache-control": "no-cache"}


class RevalidatedStatic(StaticFiles):
    """Static files a browser must revalidate before reusing.

    The `/demo` artefacts are regenerated under URLs that never change:
    `changelog.json` on every container boot, `coverage.json` whenever the
    extract is rebuilt. Starlette sets `etag` and `last-modified` but no
    `cache-control`, and with no explicit lifetime a browser may apply
    heuristic freshness — roughly a tenth of the file's age — and serve a copy
    it never revalidated.

    That is how the panel managed to show `v0.24.0` in the sidebar, from the
    API, and "Currently running version 0.23.0" on What's New, from a cached
    file, in the same screenshot. The bell is driven by the image and the page
    by the artefact, so a stale read makes the notification lie.

    `no-cache` is not `no-store`: the copy is still kept, it just has to be
    revalidated first, so the existing etag turns an unchanged file into a 304
    rather than a re-download.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers.update(REVALIDATE)
        return response


class ImmutableStatic(StaticFiles):
    """Content-hashed files, cacheable forever.

    Vite puts a hash of the contents in the filename, so a changed file is a
    changed URL and there is nothing to revalidate. Saying so explicitly is
    what makes `no-cache` on the HTML affordable: the document is re-checked
    on every load and costs a 304, and the megabyte of JavaScript it points at
    is never re-fetched at all.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


if STATIC_DIR.exists():
    # Checked separately: the demo seed writes `static/demo/` into a checkout
    # whose frontend may never have been built (the Vite dev server serves it
    # instead), and `StaticFiles` refuses to mount a directory that is absent.
    if (STATIC_DIR / "assets").exists():
        app.mount(
            "/assets", ImmutableStatic(directory=STATIC_DIR / "assets"), name="assets"
        )
    if DEMO_DIR.exists():
        app.mount("/demo", RevalidatedStatic(directory=DEMO_DIR), name="demo")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # Nothing served here is content-hashed — `index.html` least of all,
        # and it is the file that names which hashed bundle to load. Cached,
        # it pins a browser to an old build, which makes every future frontend
        # fix unreachable for exactly the people who have the bug. So the
        # document always revalidates and the bundle it points at never has
        # to; `ImmutableStatic` above is the other half of that trade.
        #
        # **The path is resolved and confined before it is served.** ASGI hands
        # this handler `scope["path"]` percent-decoded and *not* normalised —
        # uvicorn removes no dot segments — so `path` arrives holding whatever
        # the caller wrote, `../` included. Joining that onto `STATIC_DIR` and
        # trusting `is_file()` served any file the process could read, to
        # anyone who could reach the port: this route is the one place in the
        # app with no authentication, the container runs `WORKDIR /app` under
        # `network_mode: host`, and four levels up from the static directory is
        # the deploy root. `/../../../../.env.local` returned the Omnitron
        # password, and far enough up, `/proc/self/environ` returns every
        # secret passed to the container as an environment variable — including
        # `SESSION_SECRET_KEY`, with which any user's session cookie can be
        # forged. Found 2026-09-09 and confirmed over a socket, not inferred.
        #
        # `resolve()` collapses the dot segments and any symlink; `is_relative_to`
        # is what actually decides. Checking the unresolved path cannot work —
        # that is the bug — and neither can rejecting a literal `..`, because
        # the decoding has already happened by the time this sees it.
        static_root = STATIC_DIR.resolve()
        candidate = (static_root / path).resolve()
        if path and candidate.is_relative_to(static_root) and candidate.is_file():
            return FileResponse(candidate, headers=REVALIDATE)
        index = STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Frontend is not built — run `npm run build` in admin/frontend.",
            )
        return FileResponse(index, headers=REVALIDATE)
else:  # pragma: no cover — only in a source checkout with no build
    log.warning("static_dir_missing", path=str(STATIC_DIR))
