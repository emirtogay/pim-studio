"""CLI for the image module — `python -m pim_studio.brandlibrary <command>`.

Deliberately thin, and shaped like `pim_studio.sync`: resolve the command, open
one client for the run, get out of the way.

The commands are separate because the expensive one is in the middle. Reading
the catalogue is eight minutes; building a bundle from what is already
published is seconds. Folding them into one command would mean re-scanning the
library to rebuild an Excel, which is how the original script kept losing an
afternoon to a step that needed no network at all.

    targets     who has no photo                      no network
    publish     find them, send them to the server    the long one
    bundle      ZIP + Excel for Turkey                from what is published
    backfill    absorb the first run's CSV manifest   once, ever
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import integrations
from ..config import settings
from ..db import session_scope
from ..locks import PUBLISH, held
from ..logging import configure_logging, log
from ..models import PublishedImage
from ..sync.runs import sync_run
from . import bundle as bundle_module
from . import session as session_store
from .client import AuthFailed, BrandLibraryClient, BrandLibraryError, parse_cookie
from .photoserver import Uploader
from .publish import publish as publish_assets
from .targets import ImageSourceMissing, find_targets

#: The run kind these land under on the Sync Runs page.
RUN_KIND = "brandlibrary_publish"

#: The session keeper's own run kind.
#:
#: It exists because the keeper was invisible. It renews the library cookie
#: every four hours and, until now, recorded nothing anywhere — so when it
#: stopped working the first symptom was the *weekly* publish failing days
#: later, on the runs page, against the wrong job. That is not hypothetical:
#: 0.27.2 records the keeper having renewed nothing since the day it was
#: installed, and nothing noticed.
#:
#: A separate kind rather than folding it into `brandlibrary_publish`, because
#: they answer different questions on different clocks. "Can we still see the
#: library" is asked every four hours; "did we publish" is asked weekly. One
#: row for both would make a healthy keeper hide a broken publish, or a broken
#: keeper look like a publish that has not run yet.
RUN_KIND_SESSION = "brandlibrary_session"


def _years() -> list[str]:
    return [y.strip() for y in (settings.brand_library_years or "").split(",") if y.strip()]


def _client(db: Session) -> BrandLibraryClient:
    """The client, with whichever credential is best available.

    `session.build` owns the precedence — OAuth, then a pasted cookie, then the
    environment — so the CLI and the panel cannot disagree about which one a
    run used. Demo mode swaps in the mock library (`integrations`).
    """
    return integrations.brand_library_client(db)


def _server() -> Uploader:
    missing = [
        name
        for name, value in (
            ("FTP_HOST", settings.ftp_host),
            ("FTP_USER", settings.ftp_user),
            ("FTP_PASS", settings.ftp_pass),
            ("FTP_BASE_URL", settings.ftp_base_url),
        )
        if not value
    ]
    if missing:
        # FTP_BASE_URL is in that list on purpose. Publishing without it
        # produces files nobody can reference, which is the one failure this
        # module is least able to detect after the fact.
        raise BrandLibraryError(f"photo server not configured: {', '.join(missing)} unset")
    return integrations.photo_server(
        host=settings.ftp_host or "",
        user=settings.ftp_user or "",
        password=settings.ftp_pass or "",
        port=settings.ftp_port,
        remote_dir=settings.ftp_remote_dir,
        tls=settings.ftp_tls,
        verify=settings.ftp_tls_verify,
    )


def cmd_targets(args: argparse.Namespace) -> int:
    """Who needs a photo. Reads the database and nothing else."""
    with session_scope() as db:
        try:
            found = find_targets(db, limit=args.limit)
        except ImageSourceMissing as exc:
            print(f"cannot answer: {exc}", file=sys.stderr)
            return 1

    print(f"{'image data read':22} {found.image_source_at:%Y-%m-%d %H:%M} UTC")
    print(f"{'options with no photo':22} {found.missing}")
    print(f"{'  merch removed':22} {found.banned}")
    print(f"{'  already published':22} {found.published}")
    print(f"{'to look for':22} {len(found)}")
    if found.unclassified:
        print(
            f"\n{found.unclassified} of these carry a comment nobody has "
            "classified. They are included — unknown means someone decide, "
            "not skip it."
        )
    if args.list:
        print()
        for target in found.targets:
            seen = f"  (searched {target.attempts}×)" if target.attempts else ""
            print(f"  {target.model_color}{seen}")
    return 0


def cmd_cookie(args: argparse.Namespace) -> int:
    """Store a brand library session cookie, after proving it works.

    Reads a paste from a file or stdin: either a raw Cookie header or a
    browser's "Copy as cURL", because that is what the network tab produces and
    nobody types a cookie by hand.

    Tested before it is stored, exactly as the page does it. A cookie saved
    without being tried is a failure deferred to the next scheduled run, by
    which time nobody connects the two.
    """
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print(
                "Paste the Cookie header or a 'Copy as cURL', then Ctrl-D:",
                file=sys.stderr,
            )
        text = sys.stdin.read()

    cookie, user_agent = parse_cookie(text)
    if not cookie:
        print(
            "no cookie in that. Paste the Cookie header, or right-click a "
            "request in the browser's network tab and copy it as cURL.",
            file=sys.stderr,
        )
        return 1

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

    print(f"{'brand library':22} {message}")
    if not ok:
        return 1

    with session_scope() as db:
        session_store.rotate(db, cookie=cookie, user_agent=user_agent, by=args.by)
        session_store.note_success(db)

    print(f"{'stored':22} yes{f' (by {args.by})' if args.by else ''}")
    print("\nIt expires within hours. Ask whoever administers the brand library")
    print("for OAuth client credentials scoped asset:read and this stops being")
    print("a weekly chore — see deploy/README.md.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Ping the library, optionally filing the result as a run.

    `--record` is for the keeper's final verification, after any re-login. The
    probe it runs first is deliberately *not* recorded: a cookie expiring is
    the normal case the keeper exists to handle, and filing that as a failed
    run would put a red row on the page every time the thing worked.
    """
    if getattr(args, "record", False):
        return _recorded_check()
    return _check()


def _recorded_check() -> int:
    """`_check`, filed in `sync_runs` as `brandlibrary_session`.

    A failure has to leave as an exception rather than a return, for the reason
    `publish` documents at length: a `return` exits the context manager
    normally and the run is filed as a success that read nothing — which is
    also the watermark the staleness alert asks about, so a keeper failing
    every four hours would look healthy forever.

    The exit-code contract with `pim-studio-keeper.sh` is preserved on the way
    out: 2 still means "the stored credential does not work".
    """
    from ..sync.runs import sync_run

    try:
        with sync_run(RUN_KIND_SESSION) as run:
            run.seen(1)
            if _check() != 0:
                raise AuthFailed(
                    "the stored credential no longer works, and the keeper did "
                    "not replace it — the library cannot be read until it does"
                )
            run.imported(1)
    except AuthFailed:
        return 2
    return 0


def _check() -> int:
    """Ping the library with the stored credential and record that it worked.

    The keep-alive half of the session keeper. A live session answers, which
    both proves it and resets the idle clock that would otherwise let it lapse —
    a cheap read every few hours is what keeps a cookie alive between the weeks
    it would otherwise die unwatched.

    Exit codes are the contract with `pim-studio-keeper.sh`: 0 means the stored
    credential still works, 2 means it does not and the keeper should log in for
    a fresh one — the same code `publish` uses for an auth failure, so the host
    side can tell "still good" from "log in again" without parsing output.
    """
    with session_scope() as db:
        try:
            client = _client(db)
        except AuthFailed as exc:
            # `build` raises this when there is no credential at all — which for
            # the keeper is simply "log in", not an error to surface loudly.
            print(f"{'brand library':22} {exc}", file=sys.stderr)
            return 2
        try:
            ok, message = client.test()
        finally:
            client.close()

        print(f"{'brand library':22} {message}")
        if not ok:
            return 2
        if client.mode == "cookie":
            session_store.note_success(db)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """The long one: scan the library, publish what it holds."""
    with session_scope() as db:
        try:
            found = find_targets(db, limit=args.limit)
        except ImageSourceMissing as exc:
            print(f"cannot run: {exc}", file=sys.stderr)
            return 1

    if not found.targets:
        print("nothing to do — every option in a catalogue has a photo or is banned")
        return 0

    print(f"{'to look for':22} {len(found)} options")
    print(f"{'years':22} {', '.join(_years())}")
    if args.dry_run:
        print(f"{'mode':22} dry run — resolves everything, uploads nothing")

    def progress(phase: str, done: int, total: int) -> None:
        if phase == "scan" and (done % 100 == 0 or done == total):
            print(f"  scanned {done}/{total} pages", flush=True)
        elif phase == "publish" and (done % 25 == 0 or done == total):
            print(f"  published {done}/{total} options", flush=True)

    # Refuse to run alongside the panel's own publish.
    #
    # `flock` in pim-studio-images.sh already stops two of *these* overlapping,
    # but it cannot see a run started from the Images page — the panel is in a
    # container with its own /tmp. Since the panel gained a route to the brand
    # library that stopped being theoretical: both would scan the library and
    # upload the same files to the same paths, each having read "what is
    # already there" before the other wrote it.
    #
    # Taken outside `sync_run` on purpose. A skipped run is not a failed one and
    # should leave no row at all — recording a failure here would age the
    # staleness watermark for a job that simply deferred to another.
    with held(PUBLISH) as got:
        if not got:
            print(
                "a publish is already running on this server — the panel has "
                "one going, or an earlier run of this job has not finished. "
                "Skipped; nothing recorded.",
                file=sys.stderr,
            )
            return 0

        server = None
        try:
            # One session for the whole run. `publish` commits per file rather
            # than at the end — a run that dies at eighty percent keeps the
            # eighty percent — so the outer scope is here to close the session,
            # not to batch the writes.
            with sync_run(RUN_KIND) as run, session_scope() as db:
                client = _client(db)
                # `check`, not `test`: we are inside the run now, and a `return`
                # here would leave `sync_run` normally — filing a run that never
                # reached the library as a success that read nothing. That status
                # is what `last_successful_run` reports, so every failed week
                # would refresh the watermark the staleness alert waits on and the
                # job could fail forever without anything saying so.
                print(f"{'brand library':22} {client.check()}")
                if client.mode == "cookie":
                    session_store.note_success(db)

                if not args.dry_run:
                    server = _server()

                result = publish_assets(
                    db=db,
                    client=client,
                    server=server,
                    targets=list(found.targets),
                    run=run,
                    base_url=settings.ftp_base_url or "",
                    remote_dir=settings.ftp_remote_dir,
                    years=_years(),
                    dry_run=args.dry_run,
                    on_progress=progress,
                )
        except AuthFailed as exc:
            # Its own exit path: the remedy is a person pasting a cookie, not a
            # retry, and a bare traceback would not say so.
            log.error("brandlibrary_auth_failed", error=str(exc))
            print(f"\n{exc}", file=sys.stderr)
            return 2
        except BrandLibraryError as exc:
            log.error("brandlibrary_publish_failed", error=str(exc))
            print(f"\n{exc}", file=sys.stderr)
            return 1
        finally:
            if server is not None:
                server.close()

        print()
        print(f"{'options matched':22} {result.matched}")
        print(f"{'not in the library':22} {result.absent}")
        if args.dry_run:
            print(f"{'would upload':22} {len(result.planned)} files")
        else:
            print(f"{'uploaded':22} {result.uploaded}")
            print(f"{'already there':22} {result.skipped}")
            print(f"{'failed':22} {result.failed}")
        return 0 if result.ok else 1


def cmd_bundle(args: argparse.Namespace) -> int:
    """Build the ZIP and the Excel from everything published and not yet sent."""
    with session_scope() as db:
        built = bundle_module.build(
            db,
            root=settings.bundles_root,
            built_by=args.by,
            with_zip=not args.no_zip,
        )
        if built is None:
            print("nothing to hand over — every published photo is already in a bundle")
            return 0
        pruned = bundle_module.prune(db, settings.bundles_root, keep=settings.bundles_kept)

    print(f"{'bundle':22} #{built.bundle_id}")
    print(f"{'options':22} {built.options}")
    print(f"{'photos':22} {built.files}")
    print(f"{'excel':22} {built.xlsx}")
    if built.zip:
        print(f"{'zip':22} {built.zip}")
    if pruned:
        print(f"\ncleared the files of {pruned} older bundle(s); their records stay")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Absorb the CSV manifest the original script wrote.

    Run once. Without it the first run's 1,603 published files are invisible to
    this module, so every one of their options reads as still needing a photo
    and the next run re-publishes all of them.

    Two things the CSV cannot tell us, both reported rather than papered over:
    when each file was actually published — the manifest carries no timestamp,
    so `published_at` becomes the moment it was absorbed — and whether anybody
    ever handed the files to Turkey. Nothing recorded that, so the rows arrive
    unbundled and the next `bundle` picks them up. If they were already sent,
    build the bundle and mark it sent rather than deleting the rows.
    """
    path = Path(args.manifest)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    added = skipped = 0
    with session_scope() as db:
        known = {name for (name,) in db.execute(select(PublishedImage.remote_name))}
        for row in rows:
            remote = (row.get("remote") or "").strip()
            if not remote or remote in known:
                skipped += 1
                continue
            db.add(
                PublishedImage(
                    model_color=(row.get("model_color") or "").strip(),
                    brandlibrary_key=(row.get("brandlibrary_key") or "").strip(),
                    asset_name=(row.get("asset") or "").strip(),
                    edition=(row.get("edition") or "original").strip(),
                    remote_name=remote,
                    public_url=(row.get("public_url") or "").strip(),
                    bytes=int(row.get("bytes") or 0),
                )
            )
            known.add(remote)
            added += 1

    print(f"{'read':22} {len(rows)} rows")
    print(f"{'recorded':22} {added}")
    print(f"{'already known':22} {skipped}")
    if added:
        print(
            f"\nThe manifest carries no publication date, so these {added} read "
            "as published just now.\nNothing recorded whether they ever reached "
            "Turkey either, so the next `bundle`\nwill include them. If they were "
            "already sent, build it and mark it sent."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pim_studio.brandlibrary",
        description="Find photos for options that have none, and hand them to Turkey.",
    )
    sub = parser.add_subparsers(dest="command")

    p_targets = sub.add_parser("targets", help="who has no photo (no network)")
    p_targets.add_argument("--limit", type=int, default=None)
    p_targets.add_argument("--list", action="store_true", help="print every ModelColor")
    p_targets.set_defaults(fn=cmd_targets)

    p_cookie = sub.add_parser(
        "cookie",
        help="store a brand library cookie (the panel cannot — it has no egress)",
    )
    p_cookie.add_argument("--file", default=None, help="read the paste from a file")
    p_cookie.add_argument("--by", default=None, help="who pasted it, for the record")
    p_cookie.set_defaults(fn=cmd_cookie)

    p_check = sub.add_parser(
        "check",
        help="ping the library with the stored cookie; exit 2 if it needs a fresh one",
    )
    p_check.add_argument(
        "--record",
        action="store_true",
        help=(
            "File the result as a `brandlibrary_session` run, so a dead "
            "session is visible on Sync Runs within hours instead of surfacing "
            "days later as a publish failure. For the keeper's final check."
        ),
    )
    p_check.set_defaults(fn=cmd_check)

    p_publish = sub.add_parser("publish", help="find photos and send them to the photo server")
    p_publish.add_argument("--limit", type=int, default=None, help="stop after N options")
    p_publish.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and plan, upload nothing — proves auth, the scan and the matching",
    )
    p_publish.set_defaults(fn=cmd_publish)

    p_bundle = sub.add_parser("bundle", help="build the ZIP and Excel for Turkey")
    p_bundle.add_argument("--no-zip", action="store_true", help="the Excel only")
    p_bundle.add_argument("--by", default=None, help="who built it, for the record")
    p_bundle.set_defaults(fn=cmd_bundle)

    p_backfill = sub.add_parser("backfill", help="absorb the original script's CSV manifest")
    p_backfill.add_argument("manifest", help="path to brandlibrary_manifest.csv")
    p_backfill.set_defaults(fn=cmd_backfill)

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 2

    configure_logging()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
