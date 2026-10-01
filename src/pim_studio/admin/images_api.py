"""Image endpoints — what has no photo, what we published, what Turkey has.

    GET  /api/images/status      configured? connected? how many need a photo?
    GET  /api/images/published   the manifest, newest first
    GET  /api/images/absent      searched and not in the library
    GET  /api/images/bundles     what has been handed over
    GET  /api/images/bundles/{id}/{part}   the ZIP or the Excel
    POST /api/images/bundles/{id}/sent     record that somebody sent it
    POST /api/images/publish     start a run over everything with no photo
    POST /api/images/publish/upload   start a run over a supplied list

**`status` answers the day-one question first.** Everything else on the page is
a number, and a number here is only meaningful once something has read
Omnitron's image column — so the guard's verdict is a field, not an exception,
and the page renders a different screen for it rather than a zero.

**An upload is an instruction, not a suggestion.** `POST /publish` asks the
database who needs a photo and is entitled to set aside options merch removed
or Turkey already has. `POST /publish/upload` is handed a list instead, and
publishes every code on it — reporting afterwards which ones the automatic path
would have declined. A list somebody assembled by hand is evidence about what
they want that this module does not have and should not overrule.

**Publishing is not done in the request.** A run reads ~1,200 pages and moves a
gigabyte; a browser waiting on that has timed out long before it finishes. The
endpoint starts it in a thread and returns immediately, and the page reads
progress from `sync_runs` like every other long job in this panel. That also
means a closed tab does not stop it, which is the property that matters.
"""

from __future__ import annotations

import threading
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import integrations
from ..brandlibrary import bundle as bundle_module
from ..brandlibrary import session as session_store
from ..brandlibrary.client import AuthFailed, BrandLibraryClient, BrandLibraryError, parse_cookie
from ..brandlibrary.targets import (
    ImageSourceMissing,
    SuppliedSet,
    as_model_color,
    find_targets,
    image_source_at,
    stale_absence,
    supplied_targets,
)
from ..config import settings
from ..db import get_session, session_scope
from ..locks import PUBLISH, Advisory
from ..logging import log
from ..models import BrandLibraryAbsence, HandoffBundle, Product, PublishedImage, SyncRun, User
from ..reports.uploads import parse_pasted, parse_upload
from ..rules.identifiers import code_variants
from . import audit
from .auth import ROLE_DEVELOPER, ROLE_PLANNING, current_user, require_role

router = APIRouter(prefix="/api/images", tags=["images"])

Viewer = Annotated[User, Depends(current_user)]
Actor = Annotated[User, Depends(require_role(ROLE_DEVELOPER, ROLE_PLANNING))]
Db = Annotated[Session, Depends(get_session)]

#: The run kind image publishing records itself under.
RUN_KIND = "brandlibrary_publish"

# The lock a publish holds is `locks.PUBLISH`, shared with the host's
# `pim-studio-images.sh`. `_running` below covers this process and that job's
# `flock` covers its own, and neither can see the other — different namespaces
# entirely. Since the panel gained a route to the brand library both can
# genuinely run at once, so the exclusion has to live where they both already
# look: the database.

#: Rows a list endpoint returns without being asked for a page. The manifest
#: runs to thousands and the page shows a preview of it; fetching all of it to
#: render a dozen rows is a megabyte nobody reads.
DEFAULT_LIMIT = 25
MAX_LIMIT = 1000

#: The largest upload accepted, matching the coverage page's cap. A list of
#: ModelColors is kilobytes; anything at this size is somebody's whole export,
#: and the column scoring would rather be told than made to scan it.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: One publish at a time, per process. Two concurrent runs would scan the same
#: catalogue twice and race to upload the same filenames — and the filename is
#: the identity, so the loser's row hits the unique constraint and the run
#: reports failures that are really duplicates.
_running = threading.Lock()


def _configured(db: Session) -> dict[str, Any]:
    """Which halves of the setup are present.

    Reported separately because they fail separately and have different
    remedies: without a cookie nothing can be found, and without `FTP_BASE_URL`
    files can be uploaded that nobody can reference — which is the failure that
    is invisible afterwards.

    The cookie half carries its own age. The library has no password flow, so
    the credential is a browser session that expires within hours, and "when
    was this last pasted" is a question the page has to be able to answer
    without anyone running a query.
    """
    oauth = session_store.oauth_configured()
    credential = None if oauth else session_store.current(db)
    library = oauth or credential is not None
    server = bool(
        settings.ftp_host and settings.ftp_user and settings.ftp_pass and settings.ftp_base_url
    )
    return {
        "library": library,
        "photoServer": server,
        "ready": library and server,
        # `oauth` means the run signs itself in and nobody touches it weekly;
        # `cookie` means somebody pastes one. The page reads very differently
        # in the two cases, so the difference is a field rather than something
        # inferred from whether `cookie` is null.
        "mode": "oauth" if oauth else ("cookie" if credential else None),
        "cookie": (
            None
            if credential is None
            else {
                "rotatedAt": credential.rotated_at.isoformat() if credential.rotated_at else None,
                "rotatedBy": credential.rotated_by,
                "lastOkAt": credential.last_ok_at.isoformat() if credential.last_ok_at else None,
                # From the deploy rather than from the page — nobody has
                # rotated it here, so it is as old as the server.
                "fromEnv": credential.from_env,
            }
        ),
    }


@router.get("/status")
def images_status(user: Viewer, db: Db) -> dict[str, Any]:
    """Everything the page needs to decide which screen to render."""
    del user
    read_at = image_source_at()
    counts: dict[str, Any] = {
        "missing": None,
        "banned": None,
        "published": None,
        "unclassified": None,
        "targets": None,
    }
    blocked: str | None = None
    if read_at is None:
        blocked = (
            "Nothing has read Omnitron's image column yet, so an option with no "
            "image on record cannot be told apart from one nobody has looked at."
        )
    else:
        found = find_targets(db)
        counts = {
            "missing": found.missing,
            "banned": found.banned,
            "published": found.published,
            "unclassified": found.unclassified,
            "targets": len(found),
        }

    latest = db.scalar(
        select(SyncRun)
        .where(SyncRun.kind == RUN_KIND)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )
    # Absences that still describe an option needing a photo. A flat COUNT(*)
    # read 3,442 when 225 were real: the rest were grouping rows the old target
    # population searched, and options Omnitron has since got a photo for.
    absent = (
        db.scalar(
            select(func.count())
            .select_from(BrandLibraryAbsence)
            .where(~stale_absence())
        )
        or 0
    )
    unbundled = (
        db.scalar(
            select(func.count())
            .select_from(PublishedImage)
            .where(PublishedImage.bundle_id.is_(None))
        )
        or 0
    )

    # Options we have published a photo for and not yet seen in Omnitron.
    #
    # Deliberately not `counts.published`, which is a different number wearing
    # a similar name: that one counts options left out of *this run's targets*
    # because they were already done, so it reads 0 whenever the catalogue and
    # the manifest do not overlap — and a card labelled "with Turkey" showing 0
    # beside a manifest of 1,603 photos is simply wrong.
    with_turkey = (
        db.scalar(
            select(func.count(func.distinct(PublishedImage.model_color))).where(
                PublishedImage.landed_at.is_(None)
            )
        )
        or 0
    )
    photos_published = db.scalar(select(func.count()).select_from(PublishedImage)) or 0

    return {
        "configured": _configured(db),
        "imageSourceAt": read_at.isoformat() if read_at else None,
        "blocked": blocked,
        "counts": counts,
        "absent": absent,
        "awaitingBundle": unbundled,
        "withTurkey": with_turkey,
        "photosPublished": photos_published,
        "lastRun": _run_row(latest),
        "running": latest is not None and latest.status == "running",
    }


def _egress_hint(message: str) -> str:
    """Why the library was out of reach, in the terms that pick the remedy.

    A certificate error and a refused connection both mean "not from this
    container", and the two used to be described with the same sentence. They
    are not the same fault: something answering the handshake with its own
    certificate is interception, and nothing answering at all is a block. The
    first is fixed by trusting the intercepting CA, the second only by moving
    the work to a host that has egress — so a reader told the wrong one looks
    in the wrong place.
    """
    if "CERTIFICATE_VERIFY_FAILED" in message or "SSL:" in message:
        return (
            "its outbound HTTPS is intercepted — something on the path answered "
            "with its own certificate instead of the library's"
        )
    return "its outbound network is blocked"


def _run_row(run: SyncRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
        "assetsRead": run.rows_seen,
        "published": run.rows_imported,
        "failed": run.rows_dropped,
        "error": run.error_summary,
        "triggeredBy": run.uploaded_by,
    }


@router.get("/published")
def published(user: Viewer, db: Db, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
    """The manifest. The URL is the point of it — see `PublishedImage`."""
    del user
    limit = max(1, min(limit, MAX_LIMIT))
    total = db.scalar(select(func.count()).select_from(PublishedImage)) or 0
    rows = db.scalars(
        select(PublishedImage)
        .order_by(PublishedImage.published_at.desc(), PublishedImage.id.desc())
        .offset(max(0, offset))
        .limit(limit)
    )
    return {
        "total": total,
        "rows": [
            {
                "modelColor": r.model_color,
                "asset": r.asset_name,
                "edition": r.edition,
                "file": r.remote_name,
                "url": r.public_url,
                "bytes": r.bytes,
                "publishedAt": r.published_at.isoformat() if r.published_at else None,
                "bundleId": r.bundle_id,
                "landedAt": r.landed_at.isoformat() if r.landed_at else None,
            }
            for r in rows
        ],
    }


@router.get("/absent")
def absent(user: Viewer, db: Db, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Options the library does not hold.

    The only list here that needs a person: no amount of re-running produces a
    photo the brand has never shot.

    **Every row says which spellings were searched.** That is not decoration.
    Our footwear codes carry a leading `7` the brand library drops — drop it and
    you have the code the way Bynder stores it — and if that rule
    is ever wrong, a shoe does not fail loudly: it lands here, looking exactly
    like a photo nobody ever took, and somebody emails the brand about it.
    Showing both spellings is what lets a reader tell "we looked properly and it
    is not there" from "we looked under the wrong name".
    """
    del user
    # Same scope as the card on `/status`, or the tab and its count disagree.
    total = (
        db.scalar(
            select(func.count())
            .select_from(BrandLibraryAbsence)
            .where(~stale_absence())
        )
        or 0
    )
    rows = db.scalars(
        select(BrandLibraryAbsence)
        .where(~stale_absence())
        .order_by(BrandLibraryAbsence.attempts.desc(), BrandLibraryAbsence.model_color)
        .limit(max(1, min(limit, MAX_LIMIT)))
    )
    return {
        "total": total,
        "rows": [
            {
                "modelColor": r.model_color,
                "attempts": r.attempts,
                "firstMissedAt": r.first_missed_at.isoformat() if r.first_missed_at else None,
                "lastCheckedAt": r.last_checked_at.isoformat() if r.last_checked_at else None,
                # Derived, not stored: the rule can change, and a stored copy
                # would then describe a search nobody would run today.
                "searchedAs": code_variants(r.model_color),
            }
            for r in rows
        ],
    }


@router.get("/bundles")
def bundles(user: Viewer, db: Db, limit: int = 20) -> dict[str, Any]:
    del user
    rows = db.scalars(
        select(HandoffBundle)
        .order_by(HandoffBundle.built_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    return {
        "rows": [
            {
                "id": b.id,
                "builtAt": b.built_at.isoformat() if b.built_at else None,
                "builtBy": b.built_by,
                "options": b.option_count,
                "files": b.file_count,
                "bytes": b.bytes,
                "status": b.status,
                "sentAt": b.sent_at.isoformat() if b.sent_at else None,
                # A cleared bundle keeps its counts and loses its files. The
                # page must not offer a download that 404s.
                "hasZip": bool(b.zip_path) and b.status == "ready",
                "hasXlsx": bool(b.xlsx_path) and b.status == "ready",
            }
            for b in rows
        ]
    }


@router.get("/bundles/{bundle_id}/{part}")
def bundle_file(user: Viewer, db: Db, bundle_id: int, part: str) -> FileResponse:
    del user
    if part not in ("zip", "xlsx"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such part")
    bundle = db.get(HandoffBundle, bundle_id)
    if bundle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such bundle")
    path = bundle.zip_path if part == "zip" else bundle.xlsx_path
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"this bundle has no {part}")

    from pathlib import Path as _Path

    resolved = _Path(path)
    if not resolved.exists():
        # Pruned. Say which, because "gone" and "never built" are different
        # things and only one of them is somebody's mistake.
        raise HTTPException(
            status.HTTP_410_GONE,
            "the files for this bundle have been cleared; its record remains",
        )
    return FileResponse(resolved, filename=resolved.name)


@router.post("/bundles/{bundle_id}/sent")
def mark_sent(request: Request, user: Actor, db: Db, bundle_id: int) -> dict[str, Any]:
    """Record that somebody sent a bundle on.

    Null means built and not yet sent, which is a real and common state — the
    run builds it at 06:00 and a person emails it when they get in.
    """
    bundle = db.get(HandoffBundle, bundle_id)
    if bundle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such bundle")

    bundle_module.mark_sent(db, bundle_id)
    audit.record(
        action="sent an image bundle to Turkey",
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path=str(request.url.path),
        status_code=200,
        ip=audit.client_ip(request),
        detail=f"bundle #{bundle_id} — {bundle.option_count} options, {bundle.file_count} photos",
    )
    audit.mark_recorded(request)
    return {"ok": True, "bundleId": bundle_id}


@router.post("/cookie")
def paste_cookie(
    request: Request,
    user: Actor,
    db: Db,
    text: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    """Replace the brand library cookie, and prove it works before accepting it.

    The library has no password flow and no API token, so this is the whole of
    authentication and it expires within hours. It is a page control rather
    than a config file for exactly that reason: a credential that needs
    replacing weekly cannot need a deploy.

    **Tested before it is stored.** A cookie that does not work, saved
    silently, turns the next scheduled run into a failure nobody connects to
    this paste — and a dead cookie does not answer 401, it answers an HTML
    login page that parses as an empty library. Proving it here is what keeps
    "I pasted it" and "it works" the same statement.

    The paste may be a raw Cookie header or a browser's "Copy as cURL", because
    that is what the network tab's right-click menu produces and nobody types a
    cookie by hand.
    """
    if session_store.oauth_configured():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this server signs itself in with OAuth client credentials — there "
            "is no cookie to replace, and pasting one would change nothing.",
        )
    cookie, user_agent = parse_cookie(text)
    if not cookie:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no cookie in that. Paste the Cookie header, or right-click a "
            "request in the browser's network tab and copy it as cURL.",
        )

    client = integrations.brand_library_client(
        cookie=cookie,
        domain=settings.brand_library_domain,
        user_agent=user_agent or settings.brand_library_user_agent,
        category=settings.brand_library_category,
    )
    try:
        ok, message = client.test()
    finally:
        client.close()
    if not ok:
        # Distinguish "your cookie is wrong" from "this container cannot reach
        # the library to find out". On the server it is nearly always the
        # second: where the app container's outbound traffic is restricted
        # (the same reason the PIM sync can run from host cron), this test can
        # never pass here and the paste box can never store anything.
        #
        # Without saying so, a correct cookie is rejected by a panel that looks
        # broken, and the only working route — the CLI on the host — is one
        # nobody would guess.
        unreachable = "could not reach" in message or "login page" not in message
        detail = message
        if unreachable:
            detail = (
                f"{message} — if this panel is on the server, "
                f"{_egress_hint(message)}, so it cannot check a cookie from "
                "here. Store it from the host instead: "
                "`pim-studio-images.sh` runs host-networked, and "
                "`python -m pim_studio.brandlibrary cookie` in a "
                "host-networked container of the same image does exactly what "
                "this box does. See deploy/README.md."
            )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)

    session_store.rotate(db, cookie=cookie, user_agent=user_agent, by=user.email)
    session_store.note_success(db)

    audit.record(
        action="replaced the brand library cookie",
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path=str(request.url.path),
        status_code=200,
        ip=audit.client_ip(request),
        detail=message,
    )
    audit.mark_recorded(request)
    return {"ok": True, "message": message}


@router.post("/publish")
def start_publish(request: Request, user: Actor, db: Db, dry_run: bool = False) -> dict[str, Any]:
    """Start a run and return. See the module docstring for why it is a thread."""
    config = _configured(db)
    if not config["library"] or (not dry_run and not config["photoServer"]):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the image module is not fully configured on this server",
        )
    try:
        found = find_targets(db)
    except ImageSourceMissing as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not found.targets:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "nothing to do — every option in a catalogue has a photo or is banned",
        )
    if _running.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "a run is already in progress")

    # Prove we can reach the library *before* spawning the thread.
    #
    # A container with restricted outbound traffic cannot, and there this
    # check is what turns "the button did nothing" into a sentence naming the
    # reason. A run
    # that fails inside a background thread leaves only a failed row on Sync
    # Runs, minutes later, for somebody who has already walked away.
    #
    # Costs one request. A run costs eight minutes.
    client = _preflight(db)
    if client.mode == "cookie":
        session_store.note_success(db)
    client.close()

    codes = [t.model_color for t in found.targets]
    actor = user.email
    lock = _claim_publish()
    try:
        threading.Thread(
            target=_publish_in_background,
            args=(codes, actor, dry_run),
            kwargs={"lock": lock},
            name="brandlibrary-publish",
            daemon=True,
        ).start()
    except BaseException:
        lock.release()
        raise

    audit.record(
        action="started an image publish",
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path=str(request.url.path),
        status_code=202,
        ip=audit.client_ip(request),
        detail=f"{len(codes)} options{' (dry run)' if dry_run else ''}",
    )
    audit.mark_recorded(request)
    return {"started": True, "options": len(codes), "dryRun": dry_run}


def _preflight(db: Session) -> BrandLibraryClient:
    """Prove we can reach the library, or say why not. Costs one request.

    Shared by both publish endpoints because the reason is the same: a run that
    fails inside a background thread leaves only a failed row on Sync Runs,
    minutes later, for somebody who has already walked away. One request now
    buys a sentence on screen instead.
    """
    try:
        client = integrations.brand_library_client(db)
    except AuthFailed as exc:
        # `_configured` said the module was ready and the credential is gone —
        # a cookie deleted between the page loading and the button being
        # pressed, say. A 500 here would read as a broken panel rather than as
        # a credential to replace.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    try:
        reachable, message = client.test()
    except Exception:
        client.close()
        raise
    if not reachable:
        client.close()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"{message} — if this panel is on the server, {_egress_hint(message)}, "
            "so publishing has to run from the host: `pim-studio-images.sh`.",
        )
    return client


def _claim_publish() -> Advisory:
    """Take the cross-process publish lock, or refuse the request.

    Taken last, after the cheap checks and the preflight, so a request that was
    going to be refused for another reason never holds it. Handed to the
    background thread, which releases it when the run ends — the acquire and
    the release are deliberately in different scopes, which is why this is not
    a context manager.
    """
    lock = Advisory(PUBLISH)
    if not lock.acquire():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a publish is already running on this server — either from this "
            "panel or from the scheduled job on the host. Wait for it to finish.",
        )
    return lock


def _supplied_payload(
    supplied: SuppliedSet,
    parsed: Any,
    not_on_record: list[str],
    unreadable: list[str],
) -> dict[str, Any]:
    """What the page shows about an upload it has just accepted.

    `column` comes back for the same reason the coverage page returns it: an
    upload that quietly read the wrong column looks exactly like one that
    worked. `unreadable` is the only thing dropped, and it is listed rather
    than counted so somebody can see it really was a heading and not a code the
    shape test failed to recognise.
    """
    return {
        "options": len(supplied),
        "rowsRead": parsed.rows_read,
        "column": parsed.column,
        "sheet": parsed.sheet,
        "notOnRecord": not_on_record,
        "unreadable": unreadable,
        # Both stages, because both can collapse a repeat: the parser when
        # two rows resolve to one code, and `supplied_targets` if a caller
        # hands it a list it did not parse. In practice the second is zero
        # here, and summing keeps that an implementation detail.
        "duplicates": parsed.duplicates + supplied.duplicates,
        "overrides": {
            "banned": list(supplied.banned),
            "published": list(supplied.published),
            # Minus the ones reported as not on record at all. Every code with
            # no product row is trivially in no live catalogue, so leaving them
            # here listed the same codes twice under two headings — and the
            # weaker of the two. What survives is the interesting case: a
            # product we hold that no market currently offers.
            "outOfCatalogue": [
                code for code in supplied.out_of_catalogue if code not in set(not_on_record)
            ],
        },
    }


@router.post("/publish/upload")
def publish_from_upload(
    request: Request,
    user: Actor,
    db: Db,
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str | None, Form()] = None,
    dry_run: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    """Publish photos for a list of ModelColors somebody uploaded.

    Sync rather than async, so FastAPI runs it in a threadpool: the parse is
    pure CPU on a user-supplied workbook and the session is the ordinary
    blocking one, and a single uvicorn worker cannot afford either on the event
    loop.

    The catalogue is the oracle for reading the file — the column holding the
    codes is found by how many of its values are products we hold, not by its
    header — so `known` is every ModelColor on record rather than only the ones
    a live catalogue carries. An option PL has never listed still resolves,
    which is frequently the whole point of an upload.
    """
    if _running.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "a run is already in progress")
    config = _configured(db)
    if not config["library"] or (not dry_run and not config["photoServer"]):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the image module is not fully configured on this server",
        )

    known = [row[0] for row in db.execute(select(Product.model_color).distinct()) if row[0]]
    if not known:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no products on record to read a list against. Run "
            "`python -m pim_studio.sync products` first.",
        )

    source = "pasted"
    if file is not None and file.filename:
        source = file.filename
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{file.filename} is larger than "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Upload just the column "
                "of codes rather than the whole export.",
            )
        try:
            parsed = parse_upload(file.filename, data, known)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — any parser failure is the user's file
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Could not read {file.filename}: {exc}"
            ) from exc
    elif text and text.strip():
        parsed = parse_pasted(text, known)
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Upload a file or paste a list of ModelColors."
        )

    # Everything in the column, not only what matched a product.
    #
    # The parser resolves against the ModelColors Omnitron has given us, which
    # is the right oracle for finding *which* column holds the codes and the
    # wrong one for deciding what to look for. The brand library holds photos
    # for options this catalogue has never carried — that is frequently why
    # somebody uploads a list at all — and it is the library, not our product
    # table, that decides whether a code turns up a photo. So an unresolved
    # token is searched too, as long as it is shaped like a code rather than a
    # heading or a note somebody left in the column.
    on_record = list(parsed.model_colors)
    not_on_record: list[str] = []
    unreadable: list[str] = []
    for token in parsed.not_found:
        code = as_model_color(token)
        if code is None:
            unreadable.append(token)
        else:
            not_on_record.append(code)

    if not on_record and not not_on_record:
        # Checked against everything we would search, not only what matched a
        # product — a list made entirely of options Omnitron has never carried
        # is a real upload, and refusing it here was the same mistake as
        # dropping those codes individually. `problem` explains what was looked
        # at, which distinguishes "your file is empty" from "no column in it
        # looks like codes".
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            parsed.problem or f"No ModelColors found in {source}.",
        )

    supplied = supplied_targets(db, [*on_record, *not_on_record])
    client = _preflight(db)
    if client.mode == "cookie":
        session_store.note_success(db)
    client.close()

    codes = [t.model_color for t in supplied.targets]
    lock = _claim_publish()
    try:
        threading.Thread(
            target=_publish_in_background,
            args=(codes, user.email, dry_run),
            kwargs={"source_file": source, "lock": lock},
            name="brandlibrary-publish",
            daemon=True,
        ).start()
    except BaseException:
        # A thread that never started will never reach the release in its
        # `finally`, and the lock would then be held by nothing until the
        # process restarts.
        lock.release()
        raise

    audit.record(
        action="started an image publish from a list",
        user_id=user.id,
        user_email=user.email,
        method="POST",
        path=str(request.url.path),
        status_code=202,
        ip=audit.client_ip(request),
        detail=f"{len(codes)} options from {source}{' (dry run)' if dry_run else ''}",
    )
    audit.mark_recorded(request)
    return {
        "started": True,
        "dryRun": dry_run,
        "source": source,
        **_supplied_payload(supplied, parsed, not_on_record, unreadable),
    }


def _publish_in_background(
    codes: list[str],
    actor: str,
    dry_run: bool,
    source_file: str | None = None,
    lock: Advisory | None = None,
) -> None:
    """The run itself. Its own session, because the request's is long gone."""
    from ..brandlibrary.photoserver import Uploader
    from ..brandlibrary.publish import publish as publish_assets
    from ..brandlibrary.targets import Target
    from ..sync.runs import sync_run

    if not _running.acquire(blocking=False):
        log.warning("brandlibrary_publish_already_running")
        if lock is not None:
            lock.release()
        return

    server: Uploader | None = None
    try:
        with session_scope() as db:
            client = integrations.brand_library_client(db)
        if not dry_run:
            server = integrations.photo_server(
                host=settings.ftp_host or "",
                user=settings.ftp_user or "",
                password=settings.ftp_pass or "",
                port=settings.ftp_port,
                remote_dir=settings.ftp_remote_dir,
                tls=settings.ftp_tls,
                verify=settings.ftp_tls_verify,
            )
        years = [
            y.strip() for y in (settings.brand_library_years or "").split(",") if y.strip()
        ]
        with (
            sync_run(RUN_KIND, triggered_by=actor, source_file=source_file) as run,
            session_scope() as db,
        ):
            publish_assets(
                db=db,
                client=client,
                server=server,
                targets=[Target(model_color=c) for c in codes],
                run=run,
                base_url=settings.ftp_base_url or "",
                remote_dir=settings.ftp_remote_dir,
                years=years,
                dry_run=dry_run,
            )
    except AuthFailed as exc:
        # The cookie died. Logged distinctly because the remedy is a person
        # pasting a fresh one, and the page reads the run's status to say so.
        log.error("brandlibrary_publish_auth_failed", error=str(exc))
    except BrandLibraryError as exc:
        log.error("brandlibrary_publish_failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — a background thread must not die silently
        log.error("brandlibrary_publish_crashed", error=f"{type(exc).__name__}: {exc}")
    finally:
        if server is not None:
            server.close()
        _running.release()
        if lock is not None:
            lock.release()
