"""Fill a database so every page of the panel has something true to show.

    python -m pim_studio.demo.seed [--size N] [--out DIR] [--skip-migrations]

Demo mode is switched on for this process whatever the environment says: a
demo seed must never reach a real system. It refuses to touch a database that
already holds data it did not write, and on a database it already seeded it
only regenerates the static files (the container's `static/demo/` does not
survive a rebuild; the database volume does).

**The data comes from the application's own code.** Nothing here inserts a
product, a value, a membership or a listing. The seed runs the real sync stages
against the mocks, twice — the catalogue as it stood yesterday, then today — so
departures, sell-outs, renames, arrivals, deletions and value conflicts are
what the sync concluded rather than what somebody typed. People's actions go
through the panel's own API with a test client: signing in, deciding
conflicts, accepting proposals, setting values, taking a product offline. The
push and the image publish run the same functions the panel's background
threads run.

Only the past is synthesised (`demo/history.py`): thirty days of nightly runs
with a few failures among them, and the sign-ins and housekeeping that fill an
activity log. Timestamps of the first read are moved back a day so "yesterday"
reads as yesterday.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import logging
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select

from .. import integrations
from ..config import PROJECT_ROOT, settings
from ..db import session_scope
from ..models import (
    Attribute,
    AttributeValue,
    CatalogueMembership,
    HandoffBundle,
    Product,
    StorefrontListing,
    User,
    ValueConflict,
    utcnow,
)
from . import catalog as cat
from . import history
from .placeholder import render
from .world import World, journal_path, record_size, recorded_size, reset, use

#: The account a visitor signs in with. Not an email address on purpose: the
#: login form takes a plain username, and "demo / demo" is what a README says.
DEMO_LOGIN = "demo"
DEMO_PASSWORD = "demo"

#: Invented people. Emails on the reserved `example.com` domain.
PEOPLE: tuple[tuple[str, str, str], ...] = (
    ("ada.kestrel@example.com", "Ada Kestrel", "developer"),
    ("bruno.halloway@example.com", "Bruno Halloway", "planning"),
    ("mira.novak@example.com", "Mira Novak", "planning"),
    ("tibor.fenyvesi@example.com", "Tibor Fenyvesi", "manager"),
    ("lena.ostrow@example.com", "Lena Ostrow", "manager"),
)
ADA, BRUNO, MIRA, TIBOR, LENA = (p[0] for p in PEOPLE)

#: Where the panel serves `/demo/*` from — `admin/app.py`'s `DEMO_DIR`.
STATIC_DEMO_DIR = PROJECT_ROOT / "src" / "pim_studio" / "admin" / "static" / "demo"
GENERATOR = PROJECT_ROOT / "scripts" / "build_demo_data_from_db.py"

TAKEDOWN_GROUP = "PL+CZ+HU+SK"


class SeedRefused(RuntimeError):
    """The database holds something this seed did not write."""


@dataclass
class SeedReport:
    """What a seed did, for the summary it prints and for the tests."""

    seeded: bool = False
    stages: dict[str, list[str]] = field(default_factory=dict)
    published_images: int = 0
    takedowns: int = 0
    pushed_items: int = 0
    files: dict[str, int] = field(default_factory=dict)


# ── entry points ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pim_studio.demo.seed", description=__doc__)
    parser.add_argument("--size", type=int, default=cat.DEFAULT_SIZE,
                        help=f"options in the synthetic catalogue (default {cat.DEFAULT_SIZE})")
    parser.add_argument("--out", type=Path, default=None,
                        help="where the panel's /demo files go (default: admin/static/demo)")
    parser.add_argument("--skip-migrations", action="store_true",
                        help="the schema is already at head (the container entrypoint)")
    args = parser.parse_args(argv)

    settings.enable_demo_mode(force=True)
    _quiet_console()
    if not args.skip_migrations:
        _migrate()
    try:
        report = run(size=args.size, out_dir=args.out)
    except SeedRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    _print(report)
    return 0


def run(size: int = cat.DEFAULT_SIZE, out_dir: Path | None = None) -> SeedReport:
    """Seed an empty database, or refresh the files of a seeded one.

    The schema must already exist. Demo mode is forced on here as well as in
    `main` — placeholders and write gates included, whatever the environment
    says — so a caller that forgot cannot seed through a real client.
    """
    from .. import seed as registry_seed

    settings.enable_demo_mode(force=True)
    # Decided before anything is written: a refusal must leave the database
    # exactly as it found it.
    state = _state()
    if state == "foreign":
        raise SeedRefused(
            "this database already holds products or accounts the demo seed did "
            "not create. Point DATABASE_URL at an empty database (the default "
            "SQLite file is fine) and run it again."
        )
    _ensure_users_table()
    registry_seed.run()

    report = SeedReport()
    target = out_dir or STATIC_DEMO_DIR
    # Before the panel is imported below: it mounts `/demo` only if the folder
    # is already there when the app module loads.
    target.mkdir(parents=True, exist_ok=True)
    if state == "empty":
        _build(size, report)
        report.seeded = True
    else:
        # Refresh with the catalogue the database was seeded from, whatever
        # this invocation was given.
        size = recorded_size()
    report.files = publish_static(target, size=size)
    if report.seeded:
        from ..admin import audit

        # Once, with the seed: a re-run on a seeded database only refreshes
        # files, and leaves the database exactly as it was.
        audit.record(action=audit.REBUILT_EXTRACT, user_id=_user_id(DEMO_LOGIN),
                     user_email=DEMO_LOGIN, method="CLI",
                     path="python -m pim_studio.demo.seed", status_code=200,
                     detail=f"demo extract written to {target.name}/")
    return report


# ── the build ──────────────────────────────────────────────────────────────


def _build(size: int, report: SeedReport) -> None:
    catalogue = cat.build(size)
    _reset_mock_state()
    record_size(size)
    now = utcnow()

    with _panel() as panel:
        passwords = _accounts(panel)
        ada = panel.signed_in(ADA, passwords[ADA])

        # Yesterday: the first full read, then a photo publish and its bundle.
        yesterday = World(catalogue, day=1)
        with use(yesterday):
            report.stages["day 1"] = _pipeline()
            _baseline_snapshot()
            report.published_images = _publish_images(ADA)
            _bundle(ada)

        # Today, journaled: whatever the panel writes from here on is what the
        # web server's mocks will hold too.
        today_started = utcnow()
        reset()
        today = World(catalogue, day=cat.CURRENT_DAY, journal=journal_path())
        with use(today):
            report.takedowns = _takedowns(ada, catalogue)
            report.stages["day 2"] = _pipeline()
            report.pushed_items = _reviews_and_push(panel, passwords)

    history.backdate_first_read(before=today_started)
    history.synthesise(now=now)
    reset()


def _pipeline() -> list[str]:
    """Every scheduled stage, in the pipeline's own order, against the mocks.

    The stages print the same tables they print from cron; that is captured and
    only shown if something fails.
    """
    from ..sync import pipeline
    from ..sync.__main__ import STAGES

    captured = io.StringIO()
    with integrations.pim_client() as api, contextlib.redirect_stdout(captured):
        results = pipeline.run(api, {name: fn for name, (fn, _) in STAGES.items()}, full=True)
    failed = [r for r in results if r.status != "ok"]
    if failed:
        raise RuntimeError(
            "a sync stage failed against the demo mocks — "
            + "; ".join(f"{r.name}: {r.detail}" for r in failed)
            + "\n" + captured.getvalue()[-4000:]
        )
    return [r.name for r in results]


def _baseline_snapshot() -> None:
    """The funnel as yesterday's read left it, filed a week back.

    The Overview page compares against a snapshot at least a week old and
    says "no baseline yet" without one. Recording the first read's numbers
    under that date is what gives a fresh demo deltas to show.
    """
    from ..reports.overview import build_overview, load_funnel, record_snapshot

    with session_scope() as db:
        overview = build_overview(load_funnel(db))
        record_snapshot(db, overview, (datetime.now(UTC) - timedelta(days=7)).date())


def _publish_images(actor: str) -> int:
    """Everything with no photo, through the asset library to the photo server.

    `_publish_in_background` is the function the Images page starts in a
    thread; called directly here so the seed waits for it.
    """
    from ..admin import audit, images_api
    from ..brandlibrary.targets import find_targets
    from ..models import PublishedImage, SyncRun

    with session_scope() as db:
        codes = [t.model_color for t in find_targets(db).targets]
    audit.record(
        action="started an image publish", user_id=_user_id(actor), user_email=actor,
        method="POST", path="/api/images/publish", status_code=202,
        detail=f"{len(codes)} options",
    )
    # The background function logs rather than raises — a thread must not die
    # silently — so the run row is what says whether it worked.
    images_api._publish_in_background(codes, actor, dry_run=False)
    with session_scope() as db:
        status = db.scalar(
            select(SyncRun.status)
            .where(SyncRun.kind == images_api.RUN_KIND)
            .order_by(SyncRun.id.desc())
            .limit(1)
        )
        if status not in ("success", "partial"):
            raise RuntimeError(f"the demo image publish did not complete (run status {status})")
        return db.scalar(select(func.count()).select_from(PublishedImage)) or 0


def _bundle(panel_user: _Client) -> None:
    """Build the handoff bundle and record it as sent, as the weekly job does."""
    from ..brandlibrary import bundle as bundle_module

    with session_scope() as db:
        built = bundle_module.build(db, Path(settings.bundles_root), built_by=ADA)
    if built is not None:
        panel_user.post(f"/api/images/bundles/{built.bundle_id}/sent")


def _takedowns(ada: _Client, catalogue: cat.Catalog) -> int:
    """One option taken offline, and another taken offline and put back.

    Picked from options every shop listed yesterday and the catalogue still
    holds today, so the takedown has rows to remove and a visible effect.
    """
    steady = {
        o.model_color for o in catalogue.options
        if o.catalogs[cat.CURRENT_DAY] and len(o.sizes) >= 4
        and not any(s.deleted_day2 or s.sold_out_day2 for s in o.sizes)
    }
    with session_scope() as db:
        everywhere = [
            mc for (mc,) in db.execute(
                select(StorefrontListing.model_color)
                .where(StorefrontListing.left_at.is_(None))
                .group_by(StorefrontListing.model_color)
                .having(func.count() == len(cat.MARKETS))
                .order_by(StorefrontListing.model_color)
            )
            if mc in steady
        ]
    picks: list[str] = []
    for model_color in everywhere:
        if all(p.split(".")[0] != model_color.split(".")[0] for p in picks):
            picks.append(model_color)
        if len(picks) == 2:
            break
    for model_color, reason in zip(
        picks,
        ("Wrong product photo on the listing — offline until the reshoot",
         "Price error on the shared catalogue"),
        strict=False,
    ):
        ada.get(f"/api/takedown/{model_color}/plan", params={"group": TAKEDOWN_GROUP})
        ada.post(
            f"/api/takedown/{model_color}/offline",
            json={"group": TAKEDOWN_GROUP, "confirm": "take offline", "reason": reason},
        )
    if len(picks) == 2:
        from ..models import CatalogueTakedown

        with session_scope() as db:
            second = db.scalar(
                select(CatalogueTakedown.id).where(CatalogueTakedown.model_color == picks[1])
            )
        ada.post(f"/api/takedown/restore/{second}")
    return len(picks)


def _reviews_and_push(panel: _Panel, passwords: dict[str, str]) -> int:
    """A day's review work: proposals, corrections, conflicts, and one push."""
    from ..sync import push
    from ..sync.runs import sync_run

    bruno = panel.signed_in(BRUNO, passwords[BRUNO])
    mira = panel.signed_in(MIRA, passwords[MIRA])
    panel.signed_in(TIBOR, passwords[TIBOR])
    catalogue = _catalogue_for_today()

    pushed: set[str] = set()

    # The proposal queue: accept two, reject one.
    proposals = bruno.get("/api/imports/proposals", params={"limit": 50}).json()["rows"]
    for row in proposals[:2]:
        bruno.post("/api/imports/proposals/accept", json={
            "modelColor": row["modelColor"], "attribute": row["attribute"],
            "market": row["market"],
        })
        pushed.add(row["modelColor"])
    for row in proposals[2:3]:
        bruno.post("/api/imports/proposals/reject", json={
            "modelColor": row["modelColor"], "attribute": row["attribute"],
            "market": row["market"],
        })

    # English placeholders in a Polish name, corrected by hand.
    flagged = _flagged_polish_names(exclude=pushed | {r["modelColor"] for r in proposals})
    for model_color in flagged[:2]:
        option = catalogue.get(model_color)
        if option is None:
            continue
        bruno.post("/api/imports/values", json={
            "modelColor": model_color, "attribute": "product_name", "market": "PL",
            "value": option.style_name["pl"],
        })
        pushed.add(model_color)

    # Missing Czech names, filled in one go.
    missing_cz = _missing_names("CZ", exclude=pushed)
    edits = [
        {"modelColor": mc, "attribute": "product_name", "market": "CZ",
         "value": catalogue[mc].style_name["cs"]}
        for mc in missing_cz[:3] if mc in catalogue
    ]
    if edits:
        bruno.post("/api/imports/apply", json={"edits": edits})
        pushed.update(e["modelColor"] for e in edits)

    # One push, from the panel's own code path, for what was reviewed so far.
    items = 0
    if pushed:
        with session_scope() as db:
            batch = push.build_batch(
                db, created_by=ADA, source="ui", model_colors=sorted(pushed),
                note="Names corrected from the review queue",
            )
            db.commit()
            batch_id, sizes = batch.id, batch.sku_count
        with integrations.pim_client() as api, sync_run(push.RUN_KIND, triggered_by=ADA) as run:
            result = push.run_batch(api, batch_id, run)
        items = int(result.get("items", 0))
        from ..admin import audit

        audit.record(
            action=audit.STARTED_PUSH, user_id=_user_id(ADA), user_email=ADA,
            method="POST", path="/api/imports/push", status_code=202,
            detail=f"batch #{batch_id}: {items} writes across {sizes} sizes",
        )
        # The shops are asked whether they serve it — the `publication` stage.
        from ..sync.__main__ import STAGES

        publication, _ = STAGES["publication"]
        with integrations.pim_client() as api, contextlib.redirect_stdout(io.StringIO()):
            if publication(api, True) != 0:
                raise RuntimeError("the publication check failed against the demo shops")

    # After the push: conflicts decided and two more corrections, still pending.
    conflicts = mira.get("/api/conflicts", params={"limit": 20}).json()["rows"]
    for row in conflicts[:2]:
        option = catalogue.get(row["modelColor"])
        local = option.names.get(row["market"]) if option else None
        candidates = list(row["candidates"])
        choice = local if local in candidates else min(candidates, key=len)
        mira.post("/api/conflicts/decide", json={
            "modelColor": row["modelColor"], "attribute": row["attribute"],
            "market": row["market"], "value": choice,
        })
    for model_color in _missing_names("SK", exclude=pushed)[:2]:
        if model_color in catalogue:
            bruno.post("/api/imports/values", json={
                "modelColor": model_color, "attribute": "product_name", "market": "SK",
                "value": catalogue[model_color].style_name["sk"],
            })
    return items


# ── the panel, through its own API ─────────────────────────────────────────


class _Client:
    """One signed-in person: a test client with their session cookie."""

    def __init__(self, client: Any, email: str) -> None:
        self.client = client
        self.email = email

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._checked(self.client.get(path, **kwargs), "GET", path)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._checked(self.client.post(path, **kwargs), "POST", path)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self._checked(self.client.patch(path, **kwargs), "PATCH", path)

    def _checked(self, response: Any, method: str, path: str) -> Any:
        if response.status_code >= 400:
            raise RuntimeError(
                f"the panel refused {method} {path} for {self.email}: "
                f"{response.status_code} {response.text[:400]}"
            )
        return response


class _Panel:
    """The FastAPI app under a test client, one cookie jar per person."""

    def __init__(self) -> None:
        from fastapi.testclient import TestClient

        from ..admin.app import app

        self._app = app
        self._factory = TestClient
        self._clients: dict[str, _Client] = {}

    def signed_in(self, login: str, password: str) -> _Client:
        if login not in self._clients:
            client = self._factory(self._app)
            client.__enter__()
            response = client.post("/api/auth/login", json={"email": login, "password": password})
            if response.status_code != 200:
                raise RuntimeError(f"could not sign in as {login}: {response.text[:200]}")
            self._clients[login] = _Client(client, login)
        return self._clients[login]

    def close(self) -> None:
        for wrapped in self._clients.values():
            wrapped.client.__exit__(None, None, None)
        self._clients.clear()


@contextlib.contextmanager
def _panel() -> Iterator[_Panel]:
    panel = _Panel()
    try:
        yield panel
    finally:
        panel.close()


def _accounts(panel: _Panel) -> dict[str, str]:
    """The demo login, then everybody else — created through the Users page."""
    from ..admin.auth import ROLE_DEVELOPER, hash_password

    with session_scope() as db:
        db.add(User(email=DEMO_LOGIN, name="Demo Admin", role=ROLE_DEVELOPER,
                    password_hash=hash_password(DEMO_PASSWORD), is_active=True))
    admin = panel.signed_in(DEMO_LOGIN, DEMO_PASSWORD)
    passwords: dict[str, str] = {}
    for email, name, role in PEOPLE:
        # Throwaway: only the demo login is meant to be used. These exist so
        # the seed can act as each person through the panel.
        passwords[email] = secrets.token_urlsafe(18)
        admin.post("/api/users", json={
            "email": email, "password": passwords[email], "role": role, "name": name,
        })
    with session_scope() as db:
        lena = db.scalar(select(User.id).where(User.email == LENA))
    admin.patch(f"/api/users/{lena}", json={"is_active": False})
    return passwords


# ── queries the review step needs ──────────────────────────────────────────


def _user_id(email: str) -> int | None:
    with session_scope() as db:
        return db.scalar(select(User.id).where(User.email == email))


def _catalogue_for_today() -> dict[str, cat.Option]:
    from .world import current

    return current().catalog.by_model_color()


def _flagged_polish_names(exclude: set[str]) -> list[str]:
    with session_scope() as db:
        name = db.scalar(select(Attribute.id).where(Attribute.key == "product_name"))
        return [
            mc for (mc,) in db.execute(
                select(AttributeValue.entity_key)
                .where(
                    AttributeValue.attribute_id == name,
                    AttributeValue.market == "PL",
                    AttributeValue.state_lane == "current",
                    AttributeValue.state == "live",
                    AttributeValue.suspect_flag.is_not(None),
                )
                .order_by(AttributeValue.entity_key)
            )
            if mc not in exclude
        ]


def _missing_names(market: str, exclude: set[str]) -> list[str]:
    """Options in the market's catalogue with no name in its language."""
    with session_scope() as db:
        name = db.scalar(select(Attribute.id).where(Attribute.key == "product_name"))
        named = set(db.scalars(
            select(AttributeValue.entity_key).where(
                AttributeValue.attribute_id == name,
                AttributeValue.market == market,
                AttributeValue.state_lane == "current",
            )
        ))
        conflicted = set(db.scalars(select(ValueConflict.entity_key)))
        members = db.scalars(
            select(Product.model_color)
            .join(CatalogueMembership, CatalogueMembership.product_id == Product.id)
            .where(CatalogueMembership.market == market, CatalogueMembership.left_at.is_(None),
                   Product.is_size())
            .distinct()
            .order_by(Product.model_color)
        )
        return [mc for mc in members
                if mc not in named and mc not in exclude and mc not in conflicted]


# ── state and housekeeping ─────────────────────────────────────────────────


def _state() -> str:
    """`empty`, `seeded` (by this), or `foreign` (somebody's real data).

    Read-only, and tolerant of a `users` table nobody has created yet — which
    is the normal state of a fresh standalone database (`_ensure_users_table`).
    """
    with session_scope() as db:
        has_users = inspect(db.connection()).has_table(User.__tablename__)
        if has_users and db.scalar(
            select(User.id).where(User.email == DEMO_LOGIN)
        ) is not None:
            return "seeded"
        if db.scalar(select(Product.id).limit(1)) is not None or (
            has_users and db.scalar(select(User.id).limit(1)) is not None
        ):
            return "foreign"
    return "empty"


def _ensure_users_table() -> None:
    """Create `users` when no other application has.

    The migrations deliberately do not own it: in production the table is
    shared with a sibling application, which creates it (migration
    c4a1e77b21f0). A standalone demo database has no sibling, so the demo
    creates the table from the same model — the shape both sides agree on.
    """
    with session_scope() as db:
        User.__table__.create(db.connection(), checkfirst=True)


def _reset_mock_state() -> None:
    """A fresh database means fresh mocks: no journal, no uploaded photos."""
    from .web import mock_ftp_root

    journal = journal_path()
    if journal.exists():
        journal.unlink()
    shutil.rmtree(mock_ftp_root(), ignore_errors=True)
    reset()


def _migrate() -> None:
    """`alembic upgrade head`, as the container entrypoint runs it.

    A subprocess rather than `alembic.command`: Alembic's `env.py` reconfigures
    logging for the whole process, which would silence this one.
    """
    subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT, check=True,
    )


def _quiet_console() -> None:
    """Keep the console to warnings; the log file still gets everything."""
    for handler in logging.getLogger().handlers:
        if type(handler) is logging.StreamHandler:
            handler.setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ── the static files the panel reads ───────────────────────────────────────


def publish_static(out_dir: Path, size: int = cat.DEFAULT_SIZE) -> dict[str, int]:
    """Regenerate `/demo/*`: the extract, the release notes and the thumbnails.

    The extract comes from `scripts/build_demo_data_from_db.py`, run in this
    process so it reads the same database session factory the seed wrote
    through. Returns file name -> bytes for the summary.
    """
    from .. import changelog

    out_dir.mkdir(parents=True, exist_ok=True)
    _run_generator(out_dir)
    changelog.write_static(out_dir / "changelog.json")

    # The PIM's product photos: the mock answers `productimage_set` with
    # `/demo/images/<option>.jpg`, served from here by the panel itself.
    images = out_dir / "images"
    images.mkdir(exist_ok=True)
    for option in cat.build(size).options:
        if option.image.get(cat.CURRENT_DAY):
            (images / f"{option.library_key}.jpg").write_bytes(
                render(option.model_color, option.style_name["en"].upper()[:30],
                       option.colour.rgb, size=(300, 400))
            )
    return {p.name: p.stat().st_size for p in sorted(out_dir.glob("*.json"))} | {

        "images/": sum(p.stat().st_size for p in images.glob("*.jpg"))
    }


def _run_generator(out_dir: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_demo_data_from_db", GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT_DIR = out_dir
    argv = sys.argv
    sys.argv = [str(GENERATOR)]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = module.main()
    finally:
        sys.argv = argv
    if code:
        raise RuntimeError(f"{GENERATOR.name} exited {code}")


def _print(report: SeedReport) -> None:
    if report.seeded:
        for day, stages in report.stages.items():
            print(f"{'sync ' + day:22} {', '.join(stages)}")
        print(f"{'photos published':22} {report.published_images}")
        print(f"{'takedowns':22} {report.takedowns}")
        print(f"{'values pushed':22} {report.pushed_items}")
    else:
        print("database already seeded — regenerated the static files only")
    with session_scope() as db:
        counts = {
            "products": db.scalar(select(func.count()).select_from(Product)),
            "values": db.scalar(select(func.count()).select_from(AttributeValue)),
            "conflicts": db.scalar(select(func.count()).select_from(ValueConflict)),
            "bundles": db.scalar(select(func.count()).select_from(HandoffBundle)),
        }
    for name, n in counts.items():
        print(f"{name:22} {n}")
    for name, size in report.files.items():
        print(f"{'wrote ' + name:22} {size:,} bytes")
    print(f"\nsign in as {DEMO_LOGIN!r} with password {DEMO_PASSWORD!r}")


if __name__ == "__main__":
    raise SystemExit(main())
