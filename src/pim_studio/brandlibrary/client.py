"""A read-only client for the brand library (Bynder).

Ported from the downloader app's `brandlib_core`, and reconciled against it —
every constant here is one that app verified live rather than one derived from
Bynder's general documentation, because on this instance the two disagree in
six places.

**Two ways in, and they are not equivalent.**

*OAuth client credentials* is the one to have. Bynder issues a `client_id` and
`client_secret` pair, `POST /v6/authentication/oauth2/token` exchanges them for
a bearer token good for an hour, and the run signs itself in every time. That
is what makes a scheduled weekly job genuinely unattended, and it is the mode
this client prefers whenever the pair is configured.

*A browser session cookie* is the fallback, and it is what the original
downloader app used — presumably because nobody had asked for an OAuth pair. It
works, and it expires within hours, so it cannot live in the server's `.env`
and be relied on by a scheduled run: it would be stale by the second week. It
is stored in the database and replaced from the Images page instead, which
makes the weekly cadence a paste rather than a deploy.

**A dead cookie does not answer 401.** It answers 200 with an HTML login page,
which parses as "no assets" and reads exactly like a library that has nothing
in it. `get_json` checks the content type for that reason, and it is the single
most important line in this file. (OAuth does answer 401, properly, which is
one more reason to prefer it.)

**The catalogue is filtered by two metaproperties**, both verified live:
`property_Year` for the season year and `property_2017_Category=ECOMMERCE` to
exclude everything that is not product photography. Dropping the category
filter roughly triples the scan and pulls in campaign assets that would publish
onto product pages.

**Editions live in `thumbnails` under marketplace keys** — `MKP_ZAL` is
Zalando. The original is a top-level `original` field, not a derivative; the
`/download/` endpoint is its fallback for when that URL has expired, not the
normal path.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any

import httpx

from ..logging import log

DEFAULT_DOMAIN = "assets.example.test"

#: In cookie mode the library's web API expects the requests its own web app
#: sends (user agent, XHR header, referer); the session is refused without them.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ORIGINAL = "original"
ZALANDO = "zalando"
EDITIONS = (ORIGINAL, ZALANDO)

#: Friendly edition name -> the key it has in an asset's `thumbnails`.
#: Verified live by the original app. `MKP_` is Bynder's marketplace prefix;
#: the others are stock sizes we do not publish.
EDITION_KEYS = {
    "zalando": "MKP_ZAL",
    "emea": "MKP_EMEA",
    "apac": "MKP_APAC",
    "outlet": "MKP_OUTLET",
}
ZALANDO_KEY = "MKP_ZAL"

#: The metaproperty carrying the season year, and the category value that means
#: product photography. Both verified live — see the module docstring.
YEAR_PROPERTY = "property_Year"
CATEGORY_PROPERTY = "property_2017_Category"
ECOMMERCE = "ECOMMERCE"

#: Token exchange. Documented at
#: https://api.bynder.com/reference/post_v6-authentication-oauth2-token-client-credentials
TOKEN_PATH = "/v6/authentication/oauth2/token"

#: The only scope this module needs. It reads assets and nothing else — asking
#: for more would be asking the library's administrators to grant a write we
#: have no code path for.
SCOPE = "asset:read"

#: Seconds before expiry at which a token is treated as spent. A scan runs for
#: minutes across a dozen threads, and a token that expires mid-page turns a
#: successful run into a partial one.
TOKEN_LEEWAY = 120

PAGE_SIZE = 200
CONCURRENCY = 12

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 5
_RETRY_BASE = 1.5
_TIMEOUT = httpx.Timeout(300.0, connect=15.0)
_CHUNK = 1 << 16


class BrandLibraryError(RuntimeError):
    """Any non-recoverable failure talking to the brand library."""


class AuthFailed(BrandLibraryError):
    """The cookie is missing, expired, or was refused.

    The one failure here with a human remedy that is not "try again": somebody
    has to paste a fresh cookie. Separated from every other error so the page
    can say that instead of "something went wrong".
    """


class ReadOnlyViolation(BrandLibraryError):
    """Something attempted a write against the brand's library.

    As in `omnitron/client.py`, this makes the read-only decision a property of
    the code rather than of everyone's memory.
    """


def _first(value: Any) -> Any:
    """Bynder returns metaproperty values as one-element lists."""
    return value[0] if isinstance(value, list) and value else value


@dataclass(frozen=True, slots=True)
class Asset:
    """One asset, reduced to what publishing needs.

    `product_id` and `color_id` are the library's own identification of which
    option a photo belongs to. They are the reason matching is reliable: the
    alternative is parsing the filename, which works until a shoot prefix or a
    naming change breaks it silently.
    """

    id: str
    name: str
    #: File extension as the library holds it. Not every asset is a JPEG, and
    #: publishing a PNG under `.jpg` produces a file browsers refuse to render.
    extension: str = "jpg"
    product_id: str | None = None
    color_id: str | None = None
    #: The original's URL — a top-level field, not a derivative.
    original: str | None = None
    #: Derivative key -> URL, verbatim from `thumbnails`.
    thumbnails: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_zalando(self) -> bool:
        return bool(self.thumbnails.get(ZALANDO_KEY))

    def edition_url(self, edition: str) -> str | None:
        if edition == ORIGINAL:
            return self.original
        return self.thumbnails.get(EDITION_KEYS.get(edition, edition))

    @property
    def filename(self) -> str:
        base = self.name or self.id
        suffix = (self.extension or "").lstrip(".")
        if suffix and not base.lower().endswith(f".{suffix.lower()}"):
            return f"{base}.{suffix}"
        return base


def asset_from(record: dict[str, Any]) -> Asset:
    extension = _first(record.get("extension")) or "jpg"
    thumbnails = {
        key: url
        for key, url in (record.get("thumbnails") or {}).items()
        if isinstance(url, str)
    }
    return Asset(
        id=str(record.get("id") or ""),
        name=str(record.get("name") or "").strip(),
        extension=str(extension).lstrip("."),
        product_id=_first(record.get("property_BI_PRODUCT_ID")),
        color_id=_first(record.get("property_BI_COLOR_ID")),
        original=record.get("original"),
        thumbnails=thumbnails,
        raw=record,
    )


class BrandLibraryClient:
    """Read-only access to the brand library, authenticated by a session cookie."""

    def __init__(
        self,
        cookie: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        domain: str = DEFAULT_DOMAIN,
        user_agent: str | None = None,
        category: str | None = ECOMMERCE,
        page_size: int = PAGE_SIZE,
        concurrency: int = CONCURRENCY,
    ) -> None:
        if not (client_id and client_secret) and not cookie:
            raise AuthFailed(
                "no way into the brand library. Either configure an OAuth "
                "client id and secret — which lets the weekly run sign itself "
                "in — or paste a session cookie on the Images page."
            )
        self.domain = (domain or DEFAULT_DOMAIN).strip().strip("/")
        self.base_url = f"https://{self.domain}"
        self.category = category
        self.page_size = page_size
        self.concurrency = concurrency
        self._client_id = client_id
        self._client_secret = client_secret
        self._cookie = cookie
        self._user_agent = user_agent or DEFAULT_USER_AGENT

        # Token state. Guarded because the catalogue scan is concurrent and a
        # dozen threads meeting an expired token at once must mint one, not
        # twelve — the library rate-limits and would refuse the other eleven.
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()
        self._local = threading.local()

    @property
    def mode(self) -> str:
        """`oauth` or `cookie`. What the page reports, and what decides whether
        anybody has to do anything each week."""
        return "oauth" if self._client_id and self._client_secret else "cookie"

    def _access_token(self, force: bool = False) -> str:
        """A bearer token, minted or reused.

        `force` discards a token the library has just rejected — an hour-long
        token can be revoked early, and re-minting once is the difference
        between a run that recovers and one that stops eight minutes in.
        """
        with self._token_lock:
            fresh_enough = (
                self._token
                and not force
                and time.time() < self._token_expires_at - TOKEN_LEEWAY
            )
            if fresh_enough:
                return str(self._token)

            try:
                response = httpx.post(
                    f"{self.base_url}{TOKEN_PATH}",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": SCOPE,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise BrandLibraryError(f"could not reach the token endpoint: {exc}") from exc

            if response.status_code in (400, 401, 403):
                raise AuthFailed(
                    f"the brand library refused the OAuth client credentials "
                    f"({response.status_code}). Check BRAND_LIBRARY_CLIENT_ID "
                    f"and BRAND_LIBRARY_CLIENT_SECRET, and that the pair is "
                    f"granted the {SCOPE} scope."
                )
            if response.status_code >= 400:
                raise BrandLibraryError(f"token endpoint -> HTTP {response.status_code}")

            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise AuthFailed("the token endpoint returned no access_token")
            self._token = str(token)
            # Default to an hour, which is what Bynder documents, rather than
            # to forever — a missing expiry that we treat as permanent is a run
            # that fails at an unpredictable point and blames the network.
            self._token_expires_at = time.time() + float(payload.get("expires_in") or 3600)
            log.info("brandlibrary_token_minted", expires_in=payload.get("expires_in"))
            return str(self._token)

    def _auth_headers(self) -> dict[str, str]:
        if self.mode == "oauth":
            return {"Authorization": f"Bearer {self._access_token()}"}
        return {
            # Load-bearing in cookie mode: the library serves its API to a
            # browser and does not honour the session without them.
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/",
            "Cookie": self._cookie or "",
        }

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._user_agent,
            "Accept": "application/json, text/plain, */*",
            **self._auth_headers(),
        }

    # ── plumbing ───────────────────────────────────────────────────────────

    @property
    def _client(self) -> httpx.Client:
        """One client per thread — the catalogue scan is concurrent.

        Only the static headers are baked in. Authorization is sent per
        request, because in OAuth mode it rotates: a token frozen into a
        thread-local client at construction outlives its hour and every thread
        that built one early starts failing while the ones built late succeed.
        """
        existing = getattr(self._local, "client", None)
        if existing is None:
            existing = httpx.Client(
                base_url=self.base_url,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=_TIMEOUT,
                follow_redirects=True,
            )
            self._local.client = existing
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "client", None)
        if existing is not None:
            existing.close()
            self._local.client = None

    def __enter__(self) -> BrandLibraryClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, *args: object, **kwargs: object) -> None:
        """Refuse anything that is not a read.

        The library belongs to the brand and our business with it is to take
        copies, so there is no write path to misuse.
        """
        raise ReadOnlyViolation(
            f"{method} is not available: this client reads the brand library "
            "and never writes to it"
        )

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One GET, with backoff, and the content-type check that matters.

        **A dead cookie answers 200 with an HTML login page.** Parsed as JSON it
        yields nothing, which is indistinguishable from a library holding no
        assets — so an expired session would be reported as "no photos exist
        for any of these options" and 400 of them would be taken to the brand.
        Checking the content type is what turns that into an auth error.
        """
        last: Exception | None = None
        retried_auth = False
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.get(path, params=params, headers=self._auth_headers())
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(_RETRY_BASE * 2**attempt)
                continue

            if response.status_code in (401, 403):
                # An OAuth token can be revoked before its hour is up. Mint a
                # new one and try once more — the alternative is losing an
                # eight-minute scan to a token that expired a minute early.
                if self.mode == "oauth" and not retried_auth:
                    retried_auth = True
                    self._access_token(force=True)
                    continue
                raise AuthFailed(
                    f"the brand library refused our credentials "
                    f"({response.status_code}). "
                    + (
                        "Check the OAuth client id and secret."
                        if self.mode == "oauth"
                        else "The cookie has expired or been revoked. Paste a fresh one."
                    )
                )
            if response.status_code in _RETRY_STATUSES:
                last = BrandLibraryError(f"HTTP {response.status_code}")
                retry_after = response.headers.get("Retry-After", "")
                wait = (
                    float(retry_after)
                    if retry_after.replace(".", "", 1).isdigit()
                    else _RETRY_BASE * 2**attempt
                )
                time.sleep(wait)
                continue
            if response.status_code >= 400:
                raise BrandLibraryError(f"GET {path} -> HTTP {response.status_code}")

            content_type = response.headers.get("Content-Type", "")
            if "json" not in content_type:
                raise AuthFailed(
                    f"the brand library answered {content_type or 'no content type'} "
                    "instead of JSON"
                    + (
                        "."
                        if self.mode == "oauth"
                        else " — that is its login page, which means the cookie "
                        "has expired. Paste a fresh one."
                    )
                )
            return response.json()
        raise BrandLibraryError(f"GET {path} failed after {_MAX_RETRIES} attempts: {last}")

    # ── the API ────────────────────────────────────────────────────────────

    def _filters(self, year: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"type": "image"}
        if self.category and str(self.category).lower() != "any":
            params[CATEGORY_PROPERTY] = self.category
        if year:
            params[YEAR_PROPERTY] = year
        return params

    def count(self, year: str | None = None) -> int:
        """How many assets match. `total=1` asks for the count; the shape is
        `{"total": {"count": N}}`, which is not the same as Bynder's docs."""
        data = self.get_json(
            "/api/v4/media/", {**self._filters(year), "limit": 1, "total": 1}
        )
        if isinstance(data, dict):
            return int((data.get("total") or {}).get("count") or 0)
        return 0

    def check(self) -> str:
        """Prove the credentials work and say what they can see, or raise.

        The raising form exists for callers inside a `sync_run`. There, a
        failure has to leave as an exception: a `return` exits the context
        manager normally, and the run is then filed as a success that read
        nothing — which is also the watermark the staleness alert asks about,
        so a job failing every week would look healthy and never alert.

        Zero visible assets raises too. An instance that answers with an empty
        library is a permissions problem wearing a success.
        """
        total = self.count()
        if total == 0:
            raise BrandLibraryError(
                "signed in, but no assets are visible to these credentials"
            )
        how = (
            "signs itself in"
            if self.mode == "oauth"
            else "using a pasted cookie, which expires within hours"
        )
        # Space the thousands separator on the number only. Doing it to the
        # whole sentence — which this used to — also ate the comma out of
        # "a pasted cookie, which expires", leaving a double space mid-clause.
        return f"{total:,}".replace(",", " ") + f" photos visible · {how}"

    def test(self) -> tuple[bool, str]:
        """`check`, reported rather than raised.

        For the callers that turn a failure into a sentence on screen — the
        paste box and the publish preflight, neither of which has a run open
        yet. `AuthFailed` is a `BrandLibraryError`, so one clause covers both.
        """
        try:
            return True, self.check()
        except BrandLibraryError as exc:
            return False, str(exc)

    def iter_assets(
        self,
        years: list[str],
        on_progress: Callable[[int, int], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> Iterator[Asset]:
        """Every ecommerce image in the given years, pages fetched concurrently.

        One pass, not a search per option: a keyword search costs ~40s against
        this instance, so four hundred of them would outlive the cookie.
        """
        jobs: list[tuple[str | None, int]] = []
        for year in years or [None]:
            total = self.count(year)
            pages = ceil(total / self.page_size) if total else 0
            jobs.extend((year, page) for page in range(1, pages + 1))
            log.info("brandlibrary_year_planned", year=year, assets=total, pages=pages)

        done = failed = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._page, year, page): (year, page) for year, page in jobs}
            for future in as_completed(futures):
                try:
                    records = future.result()
                except AuthFailed:
                    # The cookie died mid-scan. Never let that be reported as
                    # "nothing found" — it would record hundreds of absences
                    # and send somebody to the brand for photos that exist.
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    records = []
                    if on_error:
                        on_error(exc)
                done += 1
                if on_progress:
                    on_progress(done, len(jobs))
                yield from (asset_from(r) for r in records)

        if jobs and failed == len(jobs):
            raise BrandLibraryError(
                f"every one of the {failed} catalogue pages failed to load"
            )

    def _page(self, year: str | None, page: int) -> list[dict[str, Any]]:
        data = self.get_json(
            "/api/v4/media/",
            {**self._filters(year), "limit": self.page_size, "page": page},
        )
        if isinstance(data, dict):
            return list(data.get("media") or [])
        return list(data or [])

    def catalog_index(
        self,
        years: list[str],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Asset]:
        """The whole filtered catalogue, in memory."""
        started = time.time()
        assets = list(self.iter_assets(years, on_progress))
        log.info(
            "brandlibrary_scanned",
            assets=len(assets),
            years=years,
            seconds=round(time.time() - started),
        )
        return assets

    def download_edition(self, asset: Asset, edition: str, dest: Path) -> int:
        """Fetch one edition to `dest`. Returns bytes written.

        The original's URL is signed and can expire between the scan and the
        download — eight minutes of scanning, then an hour of uploading, is
        long enough. When it does, `/download/` mints a fresh one. That
        fallback only exists for the original; a derivative that refuses is a
        real failure.
        """
        url = asset.edition_url(edition)
        if not url:
            raise BrandLibraryError(f"{asset.name} has no {edition} edition")
        try:
            return self._stream(url, dest)
        except AuthFailed:
            if edition != ORIGINAL:
                raise
            return self._stream(self._fresh_original(asset.id), dest)

    def _fresh_original(self, asset_id: str) -> str:
        data = self.get_json(f"/api/v4/media/{asset_id}/download/")
        url = None
        if isinstance(data, dict):
            url = data.get("s3_file") or data.get("s3file") or data.get("original")
        if not url:
            raise BrandLibraryError(f"no download URL for asset {asset_id}")
        return str(url)

    def _stream(self, url: str, dest: Path) -> int:
        """Write to `.part` and move into place.

        An interrupted download must not leave a truncated file, because the
        resume check treats anything present as complete. A zero-byte result is
        an error for the same reason.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        written = 0
        # The signed URL is on a storage host; our session headers do not
        # belong on it and a browser would not send them either.
        with httpx.stream(
            "GET", url, timeout=_TIMEOUT, follow_redirects=True
        ) as response:
            content_type = response.headers.get("Content-Type", "")
            if response.status_code in (401, 403) or content_type.startswith("text/html"):
                raise AuthFailed(f"download blocked ({response.status_code} {content_type})")
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    written += handle.write(chunk)
        if written == 0:
            partial.unlink(missing_ok=True)
            raise BrandLibraryError("downloaded 0 bytes")
        partial.replace(dest)
        return written


# ── pasted credentials ─────────────────────────────────────────────────────


def parse_cookie(text: str) -> tuple[str | None, str | None]:
    """Accept a raw Cookie header or a browser's "Copy as cURL". (cookie, ua).

    Nobody types a cookie. They right-click a request in the network tab and
    copy it as cURL, which is a wall of shell quoting with the cookie somewhere
    inside — so accepting that verbatim is the difference between a paste that
    works and one that needs explaining. The user agent is taken from the same
    paste when it is there, because the session is tied to it.
    """
    text = (text or "").strip()
    if not text:
        return None, None

    looks_like_curl = text.lower().startswith("curl") or "-H " in text or "--header" in text
    if looks_like_curl:
        cookie = user_agent = None
        header = re.search(r"""-H\s+(['"])cookie:\s*(.*?)\1""", text, re.I | re.S)
        if header:
            cookie = header.group(2)
        else:
            flag = re.search(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", text, re.I | re.S)
            if flag:
                cookie = flag.group(2)
        agent = re.search(r"""-H\s+(['"])user-agent:\s*(.*?)\1""", text, re.I | re.S)
        if agent:
            user_agent = agent.group(2)
        return (cookie.strip() if cookie else None), (
            user_agent.strip() if user_agent else None
        )

    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    # A cookie header is `name=value` pairs. Without this, any stray paste —
    # a URL, a note, the word "hello" — is accepted as a credential and the
    # error surfaces later as a failed run instead of here as a bad paste.
    if "=" not in text:
        return None, None
    return text, None
