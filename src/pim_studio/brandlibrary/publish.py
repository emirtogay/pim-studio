"""Getting the photos onto the photo server, and recording where they landed.

The order of operations is the design, and it is inherited from an original run
that learned each step the expensive way:

1. Read the catalogue once. Everything after this is local.
2. Match, then dedupe by filename — 136 names collided on the first run.
3. Ask the server what it already has. That is the resume state.
4. Per file: download, upload, record. Record *per file*, not at the end,
   because a run that dies at 80% must not lose the 80%.
5. Record what the library did not hold, so an absence is a piece of work
   rather than a silence.

**The manifest is the deliverable, not the upload.** A file on the photo server
that nothing points at is work that has to be done again. Every uploaded file
gets its row in the same transaction-shaped step as the upload itself, so the
two cannot drift.

**Nothing here writes `image_0`.** Our public URL is not Omnitron's image URL,
and writing it into the mirror would report a product as imaged while the site
still shows nothing.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..logging import log
from ..models import BrandLibraryAbsence, PublishedImage, utcnow
from ..sync.runs import SyncRun
from . import catalogue
from .client import EDITIONS, ORIGINAL, Asset, BrandLibraryClient, BrandLibraryError
from .photoserver import Uploader
from .targets import Target, stale_absence


@dataclass
class PublishResult:
    """What a run did, in the terms the page reports it."""

    #: Options we found photos for.
    matched: int = 0
    #: Options searched for and not in the library at all.
    absent: int = 0
    uploaded: int = 0
    #: Files already on the server or already recorded. A normal outcome on a
    #: re-run, and the reason an interrupted run is cheap to restart.
    skipped: int = 0
    failed: int = 0
    bytes_sent: int = 0
    #: Files a dry run would have sent.
    planned: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def publish(
    db: Session,
    client: BrandLibraryClient,
    server: Uploader | None,
    targets: list[Target],
    run: SyncRun,
    base_url: str,
    remote_dir: str,
    years: list[str],
    dry_run: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> PublishResult:
    """Find photos for `targets` and publish them.

    `server` may be None only when `dry_run` is set — resolving and planning
    needs the library but not the photo server, which is what makes a dry run
    a genuine rehearsal of everything except the writing.
    """
    if server is None and not dry_run:
        raise BrandLibraryError("a real run needs a photo server")

    result = PublishResult()
    codes = [t.model_color for t in targets]
    if not codes:
        return result

    # 1. One pass over the library.
    assets = client.catalog_index(
        years,
        on_progress=lambda done, total: on_progress and on_progress("scan", done, total),
    )

    # 2. Match, then dedupe within each option.
    found = catalogue.match(assets, codes)
    for code in list(found):
        found[code] = catalogue.dedupe(found[code])
    result.matched = len(found)
    run.seen(len(assets))

    # 5 (recorded early, so an interrupted run still leaves the absences it
    # proved): every target the library had nothing for.
    result.absent = _record_absences(db, codes, matched=set(found))

    # 3. What is already there.
    already = server.existing() if server is not None else set()
    recorded = {
        name for (name,) in db.execute(select(PublishedImage.remote_name))
    }

    base = base_url.rstrip("/")
    with tempfile.TemporaryDirectory(prefix="brandlibrary-") as tmp:
        staging = Path(tmp)
        for index, code in enumerate(sorted(found), start=1):
            if on_progress:
                on_progress("publish", index, len(found))
            for asset in found[code]:
                for edition in EDITIONS:
                    _publish_one(
                        db=db,
                        client=client,
                        server=server,
                        run=run,
                        result=result,
                        code=code,
                        asset=asset,
                        edition=edition,
                        staging=staging,
                        already=already,
                        recorded=recorded,
                        base=base,
                        remote_dir=remote_dir,
                        dry_run=dry_run,
                    )

    log.info(
        "brandlibrary_published",
        options=result.matched,
        absent=result.absent,
        uploaded=result.uploaded,
        skipped=result.skipped,
        failed=result.failed,
        dry_run=dry_run,
    )
    return result


def _publish_one(
    *,
    db: Session,
    client: BrandLibraryClient,
    server: Uploader | None,
    run: SyncRun,
    result: PublishResult,
    code: str,
    asset: Asset,
    edition: str,
    staging: Path,
    already: set[str],
    recorded: set[str],
    base: str,
    remote_dir: str,
    dry_run: bool,
) -> None:
    """One edition of one asset. Failures are logged and stepped over.

    A single unfetchable asset must not end a run that has hundreds of others
    to do, so the failure is counted and stepped over. The count is what
    keeps that from being the same thing as nothing having gone wrong.
    """
    # An edition the asset does not carry is not a failure; most assets have
    # no Zalando crop.
    if edition != ORIGINAL and asset.edition_url(edition) is None:
        return

    remote = catalogue.remote_name(asset, edition)
    if remote in already or remote in recorded:
        result.skipped += 1
        return

    if dry_run:
        result.planned.append(remote)
        return

    local = staging / remote
    try:
        client.download_edition(asset, edition, local)
        assert server is not None  # guarded in `publish`
        size = server.upload(local, remote)
    except Exception as exc:  # noqa: BLE001 — recorded, then carry on
        result.failed += 1
        run.dropped(1, reason="asset could not be fetched or sent")
        log.warning(
            "brandlibrary_file_failed",
            remote=remote,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return
    finally:
        local.unlink(missing_ok=True)

    db.add(
        PublishedImage(
            model_color=code,
            brandlibrary_key=catalogue.brandlibrary_key(code),
            asset_name=asset.name,
            edition=edition,
            remote_name=remote,
            public_url=f"{base}/{remote_dir}/{remote}",
            bytes=size,
            sync_run_id=getattr(run, "id", None),
        )
    )
    db.commit()

    run.imported(1)
    recorded.add(remote)
    result.uploaded += 1
    result.bytes_sent += size


def _record_absences(db: Session, codes: list[str], matched: set[str]) -> int:
    """Note every option the library had nothing for, and clear the ones it did.

    The clearing half matters as much as the recording: an absence that has
    resolved must disappear, or somebody chases the brand for a photo that
    arrived last week.
    """
    existing = {
        row.model_color: row
        for row in db.scalars(
            select(BrandLibraryAbsence).where(BrandLibraryAbsence.model_color.in_(codes))
        )
    }
    now = utcnow()
    missing = 0

    for code in codes:
        if code in matched:
            continue
        missing += 1
        row = existing.get(code)
        if row is None:
            db.add(BrandLibraryAbsence(model_color=code, attempts=1))
        else:
            row.attempts += 1
            row.last_checked_at = now

    resolved = [code for code in codes if code in matched and code in existing]
    if resolved:
        db.execute(
            delete(BrandLibraryAbsence).where(BrandLibraryAbsence.model_color.in_(resolved))
        )

    # And the ones that stopped mattering for a reason the library cannot see:
    # Omnitron got an image some other way, or the code was only ever a
    # grouping row. Clearing only on a library match left 3,217 such rows on
    # the page as photos the brand had never shot. See `stale_absence`.
    stale = db.execute(
        delete(BrandLibraryAbsence).where(stale_absence()),
        execution_options={"synchronize_session": False},
    ).rowcount
    if stale:
        log.info("brandlibrary_absences_pruned", count=stale)

    db.commit()
    return missing
