"""Coverage endpoints — the first router split out by domain.

`TEMPLATE.md` settled this shape before the first commit: the price app's
equivalent grew to 3,431 lines in one file, and the decision was not to inherit
that. `app.py` keeps auth, products and the ops endpoints for now; anything new
arrives as a router.

The endpoints, and the division between them is the interesting part:

    GET  /api/coverage             the extract, served as a file
    GET  /api/coverage/profiles    the preset definitions of complete
    POST /api/coverage/resolve     a file of codes → the options they mean
    POST /api/coverage/resolve/text  the same, for codes pasted as JSON
    POST /api/coverage/summary     a scope and a definition → the numbers
    POST /api/coverage/report      the same → an .xlsx
    POST /api/coverage/queue       the gaps in a scope, grouped by who fixes them

The dashboard computes its own numbers in the browser, so no page load touches
the three POST endpoints — they exist because a spreadsheet cannot be built in a
browser tab, and a file upload has to be parsed somewhere.

`GET /api/coverage` is not what the page reads either, today. Like
`/api/products`, it is the seam: the panel fetches the extract from the static
`/demo/` mount, and this endpoint is the authenticated URL it moves to when the
extract stops being a file. Both are served here so the move is a one-line
change in `demoData.ts` rather than a new endpoint to design under time
pressure.

**The workbook's numbers are built from the extract, not from the database.**
That is deliberate and it is the reason a download can be trusted: the page and
the file read the same bytes, so they cannot drift. Re-querying the mirror for a
population or a fill rate would produce a file that disagrees with the screen it
was downloaded from, usually by a few options, always unexplainably.

The `Values` sheet is the one exception, and it is bounded rather than excused.
A reader who wants to review the descriptions themselves needs the text, the
extract carries presence and never text, so the text can come from nowhere but
the mirror. What the extract still decides is *which options the sheet is
about*: `load_values` is handed the rows the report already resolved and can
neither add an option nor drop one. So the file can never disagree with the
screen about how many options there are — only about what one of them says, and
the sheet prints both read times and says so in as many words.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db import get_session
from ..models import User
from ..reports.coverage import (
    PRESENCE_ANY,
    Scope,
    build_report,
    from_extract,
    gap_queue,
    profile_from_payload,
)
from ..reports.uploads import parse_pasted, parse_upload
from ..reports.values import TooManyValues, ValueBlock, ValueSelection, load_values
from ..reports.workbook import build_workbook, filename_for
from ..rules.coverage import ISSUES_BY_KEY, MARKETS, PRESETS, Owner
from . import audit
from .auth import ROLE_DEVELOPER, current_user, require_role

router = APIRouter(prefix="/api/coverage", tags=["coverage"])

#: Declared once, the way the other routers do it (`images_api`, `users_api`).
Db = Annotated[Session, Depends(get_session)]

EXTRACT_NAME = "coverage.json"

#: An upload is a list of product codes, not a catalogue. Ten megabytes is far
#: more than the largest such list anyone has sent — the whole Check_list
#: export is under that — and it bounds what a single request can make the
#: server parse.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _demo_dir() -> Path:
    # Imported lazily: `app.py` imports this module, so a module-level import
    # the other way would be circular.
    from .app import DEMO_DIR

    return DEMO_DIR


def _extract_path() -> Path:
    path = _demo_dir() / EXTRACT_NAME
    if not path.exists():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{EXTRACT_NAME} has not been generated yet — run "
            "scripts/build_demo_data_from_db.py",
        )
    return path


def _load_extract() -> dict[str, Any]:
    try:
        return json.loads(_extract_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"{EXTRACT_NAME} is not readable JSON — regenerate it.",
        ) from exc


def _known_model_colors(payload: dict[str, Any]) -> list[str]:
    return [entry["mc"] for entry in payload.get("products", [])]


@router.get("")
def coverage_extract(user: Annotated[User, Depends(current_user)]) -> Response:
    """The extract, streamed rather than parsed.

    Several megabytes at real scale, and nothing here needs to look inside it —
    the same reasoning as `/api/products`.
    """
    del user
    return FileResponse(_extract_path(), media_type="application/json")


@router.get("/profiles")
def profiles(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
    """The preset definitions of complete.

    Also in the extract, which is what the page reads. Here as well because a
    caller scripting the report endpoint needs to know what it may ask for
    without downloading several megabytes to find out.
    """
    del user
    return {
        "profiles": [
            {
                "key": preset.key,
                "label": preset.label,
                "blurb": preset.blurb,
                "requirements": [
                    {"attribute": r.attribute, "markets": list(r.markets)}
                    for r in preset.requirements
                ],
            }
            for preset in PRESETS
        ]
    }


class ResolveBody(BaseModel):
    """Codes typed or pasted rather than uploaded."""

    text: str = Field(default="", max_length=2_000_000)


def _resolution_payload(parsed, known_count: int) -> dict[str, Any]:
    return {
        "modelColors": parsed.model_colors,
        "notFound": parsed.not_found,
        "matched": parsed.matched,
        "rowsRead": parsed.rows_read,
        "column": parsed.column,
        "sheet": parsed.sheet,
        "problem": parsed.problem,
        "catalogueSize": known_count,
    }


@router.post("/resolve")
async def resolve(
    user: Annotated[User, Depends(current_user)],
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Turn an uploaded file or a pasted block into a list of ModelColors.

    Resolution is a separate step from reporting on purpose. The panel shows
    what it found — which column it read, how many codes matched, and every one
    that did not — and the user confirms before anything is computed. An upload
    that silently reported on 380 of 500 codes would be indistinguishable from
    one that worked.
    """
    del user
    payload = _load_extract()
    known = _known_model_colors(payload)

    if file is not None and file.filename:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{file.filename} is larger than "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Upload just the "
                "column of codes rather than the whole export.",
            )
        try:
            # Off the event loop. This is the only `async def` here, and the
            # parse is pure CPU on a user-supplied file — a 20,000-row workbook
            # takes ~1.4s, and uvicorn runs one worker, so parsing inline
            # stalls every other request on the container for that long. The
            # sync endpoints below are already threadpooled by FastAPI; this
            # one has to ask.
            parsed = await run_in_threadpool(parse_upload, file.filename, data, known)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - any parser failure is the user's file
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Could not read {file.filename}: {exc}",
            ) from exc
        return _resolution_payload(parsed, len(known))

    if text:
        return _resolution_payload(parse_pasted(text, known), len(known))

    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        "Upload a file or paste a list of ModelColors.",
    )


@router.post("/resolve/text")
def resolve_text(
    user: Annotated[User, Depends(current_user)],
    body: ResolveBody,
) -> dict[str, Any]:
    """The pasted-codes case as JSON, for callers not sending a form."""
    del user
    payload = _load_extract()
    known = _known_model_colors(payload)
    return _resolution_payload(parse_pasted(body.text, known), len(known))


class ValueRequest(BaseModel):
    """One attribute whose values to export, and the markets to export them in.

    Empty `markets` on a per-market attribute means all four, matching what
    ticking nothing means in the picker.
    """

    attribute: str = Field(max_length=64)
    markets: list[str] = Field(default_factory=list, max_length=4)


class ScopeBody(BaseModel):
    """Which options, and what counts as complete for them."""

    #: A preset key. Ignored when `requirements` is given.
    profile: str = ""
    #: An ad-hoc definition — `[{"attribute": "description", "markets": ["PL"]}]`.
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    label: str = ""
    modelColors: list[str] = Field(default_factory=list)
    season: list[str] = Field(default_factory=list)
    subCategory: list[str] = Field(default_factory=list)
    gender: list[str] = Field(default_factory=list)
    inCatalogue: str = ""
    #: `""`, `"any"`, or a market code — see `Scope.priced`.
    priced: str = ""
    #: `""` or `"any"` — see `Scope.stocked`. A market code is rejected rather
    #: than ignored: silently widening a filter somebody asked for is how a
    #: workbook comes to disagree with the screen it was named after.
    stocked: str = ""
    #: Issue keys, OR-ed. Sent by the page since these stopped being a
    #: browser-only filter.
    flags: list[str] = Field(default_factory=list)
    #: Attributes whose *values* go on the Values sheet. Deliberately the same
    #: shape as `requirements` and chosen with the same control, but a
    #: different question: a requirement asks "must this be present", this asks
    #: "show me what it says". Empty means no Values sheet at all.
    values: list[ValueRequest] = Field(default_factory=list, max_length=12)
    #: One row per SKU rather than per option.
    #:
    #: Its own field, not a magic attribute key in `values`. It was briefly the
    #: latter — `base_code_format`, on the reasoning that it names a `Product`
    #: column — and that was simply wrong: `base_code_format` is a registered
    #: attribute (`seed.py`, labelled "Article (MCS)", sku grain). Ticking it in
    #: the grid therefore switched the grain *and* swallowed the column the
    #: reader asked for. A request that changes the shape of a sheet is not an
    #: attribute selection and must not be spelled like one.
    bySize: bool = False

    def selections(self) -> tuple[ValueSelection, ...]:
        return tuple(
            ValueSelection(attribute=v.attribute, markets=tuple(v.markets))
            for v in self.values
            if v.attribute
        )

    def by_size(self) -> bool:
        """Whether the reader asked for the sheet at size grain."""
        return self.bySize

    def to_scope(self) -> Scope:
        return Scope(
            model_colors=tuple(self.modelColors),
            season=tuple(self.season),
            sub_category=tuple(self.subCategory),
            gender=tuple(self.gender),
            in_catalogue=self.inCatalogue,
            priced=self.priced,
            stocked=self.stocked,
            flags=tuple(self.flags),
        )

    @field_validator("priced")
    @classmethod
    def _known_price_market(cls, value: str) -> str:
        if value and value != PRESENCE_ANY and value not in MARKETS:
            raise ValueError(
                f"priced must be empty, {PRESENCE_ANY!r} or one of "
                f"{', '.join(MARKETS)} — got {value!r}"
            )
        return value

    @field_validator("stocked")
    @classmethod
    def _stock_is_not_per_market(cls, value: str) -> str:
        if value and value != PRESENCE_ANY:
            raise ValueError(
                f"stocked takes {PRESENCE_ANY!r} or nothing: there is one pool "
                "of stock behind all four markets, so it cannot be filtered "
                "per market"
            )
        return value

    @field_validator("flags")
    @classmethod
    def _known_flags(cls, value: list[str]) -> list[str]:
        unknown = [f for f in value if f not in ISSUES_BY_KEY]
        if unknown:
            raise ValueError(
                f"unknown issue key(s): {', '.join(unknown)} — "
                f"expected any of {', '.join(ISSUES_BY_KEY)}"
            )
        return value


def _report_for(body: ScopeBody):
    try:
        profile = profile_from_payload(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    try:
        catalogue = from_extract(_load_extract())
    except ValueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return build_report(catalogue, profile, body.to_scope())


def _values_for(body: ScopeBody, built, db: Session) -> ValueBlock | None:
    """The only thing in this router that reads the database.

    Scoped by `built.rows` — the extract's answer — and only ever *fills* from
    the mirror, so the Values sheet's row set is the Detail sheet's row set by
    construction. The drift a live read admits is confined to the contents of a
    cell; it cannot change which options the file is about.

    Asked for nothing, it reads nothing: the session is lazy, so a request that
    ticks no attributes — which is every request the panel sends today — never
    checks a connection out at all.
    """
    selections = body.selections()
    by_size = body.by_size()
    if not selections and not by_size:
        return None
    try:
        return load_values(
            db,
            selections,
            [row.model_color for row in built.rows],
            by_size=by_size,
        )
    except TooManyValues as exc:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, str(exc)
        ) from exc


@router.post("/report")
def report(
    user: Annotated[User, Depends(current_user)],
    body: Annotated[ScopeBody, Body()],
    db: Db,
) -> Response:
    """The workbook, as a download.

    Content-Disposition names the profile and the date, because these end up in
    a shared folder and `coverage (3).xlsx` tells nobody which question it
    answered.

    Stays a sync `def`. FastAPI already runs those in the threadpool — see the
    note on `resolve` above, which is `async` and therefore has to ask. Making
    this one `async` to thread only `build_workbook` would move the multi-megabyte
    extract parse onto the event loop, which is worse than what it fixes.
    """
    del user
    built = _report_for(body)
    values = _values_for(body, built, db)
    headers = {
        "content-disposition": f'attachment; filename="{filename_for(built)}"',
        # The panel reads these back to confirm what it just downloaded,
        # since a file download gives JavaScript nothing else to go on.
        "x-coverage-options": str(built.population),
        "x-coverage-not-found": str(len(built.scope.not_found)),
    }
    if values is not None:
        headers["x-coverage-values-read-at"] = values.read_at.isoformat()
        # Filled cells, not rows × columns. Named for what it counts because
        # the panel treats these headers as confirmation, and a number that
        # does not mean what its name says reads as a bug.
        headers["x-coverage-values-filled"] = str(values.cell_count)
    return Response(
        content=build_workbook(built, values),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers=headers,
    )


@router.post("/summary")
def summary(
    user: Annotated[User, Depends(current_user)],
    body: Annotated[ScopeBody, Body()],
) -> JSONResponse:
    """The same numbers the workbook carries, as JSON.

    Not what the dashboard uses — it computes in the browser so a facet click
    costs nothing. This is for anyone checking the page against the server, and
    for a scheduled job that wants the figure without the spreadsheet.
    """
    del user
    built = _report_for(body)
    return JSONResponse(
        {
            "profile": {
                "key": built.profile.key,
                "label": built.profile.label,
                "blurb": built.profile.blurb,
            },
            "scope": {
                "description": built.scope.describe(),
                "population": built.population,
                "notFound": list(built.scope.not_found),
            },
            "standing": {
                "complete": built.summary.complete,
                "incomplete": built.summary.incomplete,
                "indeterminate": built.summary.indeterminate,
                "judged": built.summary.judged,
                "completionRate": built.summary.completion_rate,
            },
            "requirements": [
                {
                    "attribute": t.attribute,
                    "label": t.label,
                    "market": t.market,
                    "owner": t.owner.value,
                    "met": t.met,
                    "missing": t.missing,
                    "unknown": t.unknown,
                    "na": t.na,
                    "fillRate": t.fill_rate,
                    "action": t.remedy.action,
                }
                for t in built.requirements
            ],
            "issues": [
                {
                    "key": i.key,
                    "label": i.label,
                    "definition": i.definition,
                    "remedy": i.remedy,
                    "count": i.count,
                    "share": i.share(built.population),
                    "stockBacked": i.stock_backed,
                }
                for i in built.issues
            ],
            "buckets": built.buckets,
            "work": {
                owner.value: {
                    "gaps": built.summary.gaps_by_owner.get(owner, 0),
                    "options": built.summary.products_by_owner.get(owner, 0),
                }
                for owner in Owner
            },
            "caveats": built.caveats,
            "generatedAt": built.generated_at.isoformat(),
        }
    )


@router.post("/queue")
def queue(
    user: Annotated[User, Depends(current_user)],
    body: Annotated[ScopeBody, Body()],
) -> dict[str, Any]:
    """Every gap in scope, grouped by who has to fix it.

    The list behind the "where the work lands" number: which options, which
    field, which market. This is the thing that becomes an import file or a
    product-card request, so it is a list of rows rather than a count.
    """
    del user
    built = _report_for(body)
    catalogue = from_extract(_load_extract())
    grouped = gap_queue(
        catalogue, built.profile, [row.model_color for row in built.rows]
    )
    return {
        "profile": built.profile.key,
        "population": built.population,
        "queues": {
            owner.value: [
                {"modelColor": mc, "attribute": label, "scope": scope}
                for mc, label, scope in rows
            ]
            for owner, rows in grouped.items()
        },
    }


# ── rebuilding the extract ─────────────────────────────────────────────────


class RebuildResult(BaseModel):
    ok: bool
    files: dict[str, int] = Field(default_factory=dict)
    seconds: float = 0.0
    detail: str = ""


def _run_generator(out_dir: Path) -> tuple[int, str]:
    """Run the extract generator exactly as cron does.

    A subprocess rather than an import, deliberately. `pim-studio-sync.sh`
    invokes this same script, and the one thing worse than a page that cannot
    rebuild its own data is two rebuild paths that can disagree about what the
    extract contains. Importing it would also run a module that calls
    `sys.path.insert` and `argparse` at import time inside a live web process.
    """
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[3] / "scripts" / "build_demo_data_from_db.py"
    if not script.exists():
        return 1, f"generator not found at {script} — is scripts/ in the image?"

    env = dict(os.environ, DEMO_OUT_DIR=str(out_dir))
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            [sys.executable, str(script)],
            env=env, capture_output=True, text=True, timeout=1800, check=False,
        )
    except subprocess.TimeoutExpired:
        return 1, "generator exceeded 30 minutes and was stopped"
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return done.returncode, "\n".join(tail[-6:])


@router.post("/rebuild", response_model=RebuildResult)
async def rebuild_extract(
    request: Request,
    user: Annotated[User, Depends(require_role(ROLE_DEVELOPER))],
) -> RebuildResult:
    """Regenerate the extract from the database, from the panel.

    This exists because the extract and the running image are two artefacts
    that can disagree, and only one of them is versioned. 0.15.0 shipped a
    Coverage page reading `coverage.json` while the extract on disk had been
    built hours earlier by the previous image and did not contain it — so the
    page told the reader to run a script, on a machine most readers cannot log
    into, and waited for the 03:00 cron.

    Reads the database and nothing else, so it needs no Omnitron credentials
    and cannot fail for want of them.

    Developer-only: it is not destructive, but it can run for minutes and hold
    a database connection while it does, which is not something a coverage
    reviewer should be able to start by accident.
    """
    out_dir = _demo_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    code, detail = await run_in_threadpool(_run_generator, out_dir)
    elapsed = round(time.monotonic() - started, 1)

    written = {
        p.name: p.stat().st_size
        for p in sorted(out_dir.glob("*.json"))
    }
    audit.mark_recorded(request)
    audit.record(
        action=audit.REBUILT_EXTRACT,
        user_id=user.id,
        method="POST",
        path="/api/coverage/rebuild",
        status_code=200 if code == 0 else 500,
        detail=f"rc={code} in {elapsed}s; {len(written)} files",
    )
    if code != 0:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"The generator failed (rc={code}). {detail}",
        )
    return RebuildResult(ok=True, files=written, seconds=elapsed, detail=detail)
