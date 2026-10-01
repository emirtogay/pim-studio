"""Configuration, loaded from environment variables and an optional `.env.local`.

All paths default to project-relative locations so the app runs without any env
setup. Override via `.env.local` (gitignored) or OS env vars when needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/pim_studio/config.py → parents[2] is the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: What demo mode fills in when the environment leaves a setting unset or
#: blank. Every external system is a mock in demo mode (`integrations.py`), so
#: these are placeholders that let the panel's "is this configured?" checks
#: pass — none of them reaches a network, and none of them is a credential.
#:
#: The write gates are switched on for the same reason: a push, a takedown and
#: an image publish all land in the mocks, and a demo in which every button
#: answers "switched off on this server" demonstrates nothing. A gate the
#: environment sets explicitly (to anything, including off) is left alone.
DEMO_DEFAULTS: dict[str, Any] = {
    "omnitron_base_url": "https://pim.example.test",
    "omnitron_username": "demo",
    "omnitron_password": "demo-placeholder",
    "omnitron_writes_enabled": True,
    "omnitron_write_mode": "merge",
    "omnitron_takedown_enabled": True,
    "brand_library_client_id": "demo-client",
    "brand_library_client_secret": "demo-placeholder",
    "ftp_host": "ftp.example.test",
    "ftp_user": "demo",
    "ftp_pass": "demo-placeholder",
    "ftp_base_url": "https://photos.example.test",
    # The demo catalogue is a few hundred options, so each mock shop lists
    # far fewer than the 2,000 a real market's listing is held to.
    "storefront_listing_floor": 25,
}


class Settings(BaseSettings):
    """App settings, read from env vars with an optional `.env.local` fallback."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Demo mode ------------------------------------------------------------
    #
    # `DEMO_MODE=1` replaces every external system — the PIM API, the brand's
    # asset library, the FTP photo server and the four public storefronts —
    # with a mock fed from one deterministic synthetic catalogue
    # (`pim_studio.demo`). `pim_studio.integrations` is the only place that
    # reads this flag; nothing else branches on it. Seed a database for it
    # with `python -m pim_studio.demo.seed`.
    demo_mode: bool = Field(default=False)

    # Database — SQLAlchemy URL. Default is a local SQLite file so development
    # works without a server install. Production points at the Postgres 18
    # instance that also hosts the price-comparison app, but at its **own
    # database** — a shared schema would put two Alembic version tables in the
    # same namespace and they would fight.
    #   postgresql+psycopg://northwind:<pw>@localhost:5433/pim_studio
    database_url: str = Field(
        default=f"sqlite:///{PROJECT_ROOT / 'pim_studio.db'}",
    )

    # Log file root.
    logs_root: Path = Field(default=PROJECT_ROOT / "logs")

    # Where the admin panel's import page lands backoffice exports before
    # reading them. Persistent so each import is auditable — filename,
    # uploaded_by and outcome are recorded against the sync_runs row.
    uploads_root: Path = Field(default=PROJECT_ROOT / "data" / "uploads")

    # ---- Snapshots ----------------------------------------------------------
    #
    # Nightly `pg_dump`, kept grandfather-father-son. Settings rather than
    # constants because nobody has measured a real dump yet: the tiers below
    # reach back two years for forty-six files, and the right answer once the
    # first one lands is to widen them against its actual size.
    #
    # On the server this wants to be a path outside the repo — the deploy
    # script rewrites the checkout — so it is set in the environment there.
    snapshots_root: Path = Field(default=PROJECT_ROOT / "data" / "snapshots")
    snapshot_keep_daily: int = Field(default=14)
    snapshot_keep_weekly: int = Field(default=8)
    snapshot_keep_monthly: int = Field(default=24)

    # ---- Omnitron -----------------------------------------------------------
    #
    # Left unset until API access is granted. Everything ingests from uploaded
    # backoffice exports in the meantime, behind the same normaliser, so the
    # API becomes a second adapter rather than a rewrite.
    #
    # Auth is username + password exchanged for a token, not a static key, so
    # all three fields are needed. Point `omnitron_base_url` at the staging
    # instance first if one exists.
    omnitron_base_url: str | None = Field(default=None)
    omnitron_username: str | None = Field(default=None)
    omnitron_password: str | None = Field(default=None)
    # Writes are off unless both of these say otherwise. The client refuses
    # every non-GET while `omnitron_writes_enabled` is false, and the push
    # refuses to start while `omnitron_write_mode` is `unknown` — it becomes
    # `merge` only after `scripts/probe_omnitron_write.py` has shown, on one
    # product, that a POST touches the keys it names and nothing else, and that
    # a locale-headed write leaves the Turkish and default copies alone.
    omnitron_writes_enabled: bool = Field(default=False)
    omnitron_write_mode: str = Field(default="unknown")  # unknown|merge
    # Deleting catalogue items — taking a product off a market's website — has
    # its own switch, and needs `omnitron_writes_enabled` as well. Turning on
    # attribute pushes is a decision about text; this is a decision about
    # whether the panel may stop a product selling. Granting the first should
    # not quietly grant the second.
    omnitron_takedown_enabled: bool = Field(default=False)

    # ---- failure-alert destinations -----------------------------------------
    #
    # Every field below is optional. If a given destination isn't configured,
    # the notifier silently skips it (no error, no log spam). Set what you
    # need in `.env.local`:
    #   NOTIFIER_SMTP_HOST, NOTIFIER_SMTP_PORT, NOTIFIER_SMTP_USER,
    #   NOTIFIER_SMTP_PASS, NOTIFIER_SMTP_FROM, NOTIFIER_SMTP_TO,
    #   NOTIFIER_TEAMS_WEBHOOK_URL

    notifier_smtp_host: str | None = Field(default=None)
    notifier_smtp_port: int = Field(default=587)
    notifier_smtp_user: str | None = Field(default=None)
    notifier_smtp_pass: str | None = Field(default=None)
    notifier_smtp_from: str | None = Field(default=None)
    notifier_smtp_to: str | None = Field(default=None)  # comma-separated list

    notifier_teams_webhook_url: str | None = Field(default=None)

    # ---- scheduler ----------------------------------------------------------
    #
    # When True, the FastAPI lifespan starts an APScheduler BackgroundScheduler
    # in-process for the Omnitron sync and the daily readiness snapshot.
    #
    # Default True, matching the price app's hard-won lesson: a False default
    # there caused a three-week silent outage because no environment overrode
    # it. Local dev that doesn't want a sync on `uvicorn --reload` should set
    # SCHEDULER_ENABLED=false in `.env.local` explicitly.
    #
    # Whether this stays on in production depends on whether the app container
    # can reach the PIM API at all. Where the container's outbound traffic is
    # restricted, run the jobs from host cron instead. Verify reachability
    # before trusting the in-process path.
    scheduler_enabled: bool = Field(default=True)

    # Hours a sync can go without succeeding before the staleness check alerts.
    # Set against a daily cadence with room for one missed run.
    stale_sync_alert_hours: int = Field(default=36)

    # ---- sibling modules ----------------------------------------------------
    #
    # One account reaches both panels (05 - Decisions Log, 2026-08-18), and the
    # chooser needs to link across. There is no reverse proxy, so the two are
    # separate origins distinguished by port — which is fine for the session
    # cookie (cookies are scoped by host, not port) but means the other
    # module's address cannot be inferred from this one.
    #
    # Runtime settings rather than build-time constants: CI builds the image
    # without knowing where it will be served, and a hostname may replace the
    # IP later without a rebuild. Blank hides that card in the chooser rather
    # than rendering a link that goes nowhere.
    price_app_url: str = Field(default="http://localhost:8002")
    product_management_url: str = Field(default="http://localhost:8003")

    # ---- auth ---------------------------------------------------------------
    #
    # Signing key for the Starlette session cookie. Pick a long random value
    # (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`) and put
    # it in `.env.local` as SESSION_SECRET_KEY. The default below is a hard-
    # coded placeholder so local dev boots without setup; it is NOT safe for
    # any shared or production deploy.
    #
    # If this module is served from the same host as the price-comparison
    # panel and single sign-on across the two is wanted, both apps must share
    # this exact value — the cookie is only portable if the signature verifies
    # on both sides.
    session_secret_key: str = Field(
        default="dev-insecure-change-me-in-env-local-" + ("0" * 24),
    )
    # Cookie lifetime in seconds. Default ~7 days. Each authenticated request
    # rolls the cookie forward so active users aren't kicked out.
    session_max_age_seconds: int = Field(default=7 * 24 * 3600)

    # ---- BrandLibrary (Bynder) ----------------------------------------------
    #
    # The brand's asset library, and the only place a photo exists for a
    # product nobody has shot for us. Read-only: we take originals and the
    # Zalando editions and publish them ourselves.
    #
    # **Two ways in, and only one of them is unattended.**
    #
    # OAuth client credentials is the one to have: Bynder issues an id and
    # secret, the run exchanges them for an hour-long bearer token every time,
    # and nobody touches it. Ask whoever administers the library for a pair
    # scoped `asset:read`. Set both and this is the mode used.
    #
    # A browser session cookie is the fallback, and is what the original
    # downloader app used. It works and it expires within hours, so it cannot
    # sit in this file and be relied on by a scheduled run — the live one is
    # kept in the database where the Images page replaces it. See
    # `brandlibrary/session.py` and migration f2b6d3e08a41.
    #
    # `brand_library_cookie` here is a bootstrap only: it gets a fresh server
    # going and it is what a developer uses locally. A pasted cookie wins over
    # it, and OAuth wins over both.
    brand_library_domain: str = Field(default="assets.example.test")
    brand_library_client_id: str | None = Field(default=None)
    brand_library_client_secret: str | None = Field(default=None)
    brand_library_cookie: str | None = Field(default=None)

    # The session is tied to the browser that created it, so a user agent that
    # does not match can be refused. Normally taken from the same paste as the
    # cookie; this is the fallback.
    brand_library_user_agent: str | None = Field(default=None)

    # ---- Service-account sign-in (the session keeper) ------------------------
    #
    # The cookie above expires within hours, so on a cookie-only instance the
    # weekly run needs a fresh one most weeks. These are the credentials of the
    # integration's own library account, for a host-side sign-in job (not part
    # of this repository) that stores the resulting session exactly as a paste
    # would.
    #
    # The app image and the API client never read them, and they are never
    # passed into the app container. OAuth is the better answer; this covers
    # instances that do not have a client pair yet.
    brand_library_username: str | None = Field(default=None)
    brand_library_password: str | None = Field(default=None)

    # Which years of the library to scan. One pass over these costs ~1,200
    # paged requests; adding a year adds its own pages to every run, so this is
    # a real cost knob rather than a filter.
    brand_library_years: str = Field(default="2024,2025,2026")

    # The metaproperty value that means product photography. Everything else in
    # the library is campaign and lookbook material — dropping this filter
    # roughly triples the scan and pulls in assets that would publish onto
    # product pages. "any" disables it, which is for investigating, not running.
    brand_library_category: str = Field(default="ECOMMERCE")

    # ---- The photo server ---------------------------------------------------
    #
    # Our own folder on the photo server. Publishing is the point: the URL is
    # what eventually populates an image field, so an upload nobody can
    # reference is work that has to be redone.
    ftp_host: str | None = Field(default=None)
    ftp_port: int = Field(default=21)
    ftp_user: str | None = Field(default=None)
    ftp_pass: str | None = Field(default=None)
    #: Public origin the uploaded folder is served from, no trailing slash.
    ftp_base_url: str | None = Field(default=None)
    #: Directory under the FTP root that this module owns.
    #:
    #: Not the same as the downloader app's `FTP_REMOTE_DIR`, which is blank —
    #: that app uploads to the account root. Ours publishes into its own
    #: folder, which is where the 1,603 already-published files live and what
    #: every URL in the manifest contains. Copying the app's blank value across
    #: would scatter photos into the root and break every published address, so
    #: `PhotoServer` treats an empty value as this default rather than as "the
    #: root".
    ftp_remote_dir: str = Field(default="pim-studio")

    #: `auto` tries FTPS and accepts plain FTP if the server will not;
    #: `required` refuses to fall back; `off` never tries. Matches the
    #: downloader app's setting of the same name.
    ftp_tls: str = Field(default="auto")
    #: Verify the server certificate. **On by default, and the downloader app
    #: has always run this way** — its own `.env` sets it true. Turn it off
    #: only if a TLS-inspecting proxy genuinely breaks the connection.
    ftp_tls_verify: bool = Field(default=True)

    # Where handoff bundles (the ZIP and the Excel) are built. Served through
    # the API, so it has to be somewhere the app container can read.
    bundles_root: Path = Field(default=PROJECT_ROOT / "data" / "bundles")
    #: Bundles kept on disk. The `handoff_bundles` row outlives its files —
    #: what was handed over stays answerable after the ZIP is cleared.
    bundles_kept: int = Field(default=10)

    # ---- The storefront crawl ---------------------------------------------
    #
    # Below this many options, a storefront read is treated as broken rather
    # than as news — see `sync/storefront._LISTING_FLOOR`, which is what an
    # unset value means. A setting only so the demo shops, which sell a few
    # hundred options, can be read at all.
    storefront_listing_floor: int | None = Field(default=None)

    @model_validator(mode="after")
    def _demo_defaults(self) -> Settings:
        if self.demo_mode:
            self.enable_demo_mode()
        return self

    def enable_demo_mode(self, *, force: bool = False) -> None:
        """Switch this settings object to demo mode, filling the placeholders.

        Called by the validator when `DEMO_MODE` is set. A value the
        environment gave explicitly is then kept unless it is blank — so a
        read-only demo is one `OMNITRON_WRITES_ENABLED=false` away, while
        docker-compose's habit of passing unset variables through as empty
        strings does not count as a choice.

        `force` is for the demo seed, which is demo-only by definition: it
        needs the write gates open to take a product offline in the mocks, and
        must not depend on what a developer's `.env.local` happens to say.
        """
        self.demo_mode = True
        explicit = self.model_fields_set
        for name, value in DEMO_DEFAULTS.items():
            current = getattr(self, name)
            if not force and name in explicit and current not in (None, ""):
                continue
            setattr(self, name, value)


settings = Settings()
