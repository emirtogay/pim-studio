"""An Omnitron API client that reads freely and writes through one narrow door.

Everything here is grounded in the vault's `Omnitron API/` notes, which were
read from Akinon's documentation and their own open-source SDK on 2026-08-14.
Where this client differs from Akinon's `omnisdk`, the difference is deliberate
and commented — their client has no 401 recovery and treats 429 as fatal, both
of which are fine for an attended job and not for a scheduled one.

Verified against `https://pim.example.test` on 2026-08-14: both API
versions answer, `/api/i1/auth/login/` exists and is POST-only, and the
challenge header is `WWW-Authenticate: Token` — so the scheme is
`Authorization: Token <key>`, not Bearer.
"""

from __future__ import annotations

import time
from collections.abc import Collection, Iterator
from typing import Any

import httpx

from ..config import settings
from ..logging import log


class OmnitronError(RuntimeError):
    """Any non-recoverable failure talking to Omnitron."""


class ReadOnlyViolation(OmnitronError):
    """Raised when anything attempts a write the configuration does not allow.

    This exists to make the write policy a property of the code rather than
    of everyone's memory. Reads are free. Writes go through `post` alone,
    and only while `OMNITRON_WRITES_ENABLED` is set — the decision of
    2026-09-07 (vault, 05 - Decisions Log), which replaced the earlier
    read-only line with a scoped one. If you are reading this in a traceback,
    the fix is not to reach for `_request`: it is either to switch writes on
    deliberately, or to accept that this environment must not write.
    """


class WriteScopeViolation(OmnitronError):
    """Raised when a write names something outside the allowed shape.

    A path other than the product upsert, a top-level field that is not part
    of a product's identity or attributes (`is_active` would deactivate a
    product on every channel at once), a `product_type` other than a simple
    product, an attribute key the caller's registry did not allow for this
    market, or a localizable key sent without that market's locale — which
    would write the tenant default copy, the Turkish store's. None of these is
    a request this client will make, whatever it is asked.

    Also raised by the catalogue-item door when the item names a catalogue the
    caller did not allow — a takedown aimed at one market landing in another
    market's catalogue is the failure that door exists to prevent.
    """


#: The verb `_request` sends unless one of the write doors vouches for the
#: call, and the doors themselves. A map rather than a set so a refusal can
#: name the one method that opens each verb, which is the question anyone
#: reading the traceback is about to ask.
_ALLOWED_METHOD = "GET"
_VOUCHED_METHODS: dict[str, str] = {
    "PATCH": "patch_product",
    "POST": "create_catalog_item",
    "DELETE": "delete_catalog_item",
}

#: The locales a localizable key may be written under — ours, and only ours.
WRITE_LOCALES = frozenset({"pl", "cs", "sk", "hu"})

#: Akinon's own `channel_app` never asks for more than this, and no ceiling is
#: documented anywhere. Raise it only after testing against the real instance.
DEFAULT_PAGE_SIZE = 100

#: Their SDK retries 5xx on a flat 10s interval, ten times, and gives up on
#: anything below 499 — which means it treats 429 as fatal. We retry both, with
#: real backoff, because an unattended nightly sync cannot afford their choice.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 5

#: Login guard copied from `channel_app`: at most this many attempts inside the
#: window, then stop. Without it a 401 loop hammers the login endpoint.
_MAX_LOGINS = 3
_LOGIN_WINDOW_S = 360


class OmnitronClient:
    """Reads from Omnitron. Cannot write to it.

    Usage — built through `pim_studio.integrations`, which hands back the
    demo's `MockPim` (this class on a mock transport) in demo mode::

        with integrations.pim_client() as api:
            attr = api.get("/api/v1/attributes/", params={"key": "integration_yaka"})
            for page in api.iter_pages("/api/v1/attribute_value/",
                                       params={"attribute": 158}):
                ...

    The token is fetched on first use and reused. Akinon's own clients never
    expire it — `omnisdk` fetches once and never re-checks, `channel_app` caches
    it in Redis with no TTL — which is strong evidence tokens live until
    revoked. We still re-login once on a 401, because their omission is a latent
    bug rather than a guarantee.
    """

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        # `is None` rather than falsy: omitting an argument means "read the
        # config", passing an empty string means "this is empty and that is an
        # error". Collapsing the two made a test pass or fail depending on
        # whether a .env.local happened to exist on the machine.
        if base_url is None:
            base_url = settings.omnitron_base_url
        if username is None:
            username = settings.omnitron_username
        if password is None:
            password = settings.omnitron_password

        self.base_url = (base_url or "").rstrip("/")
        self._username = username
        self._password = password
        if not self.base_url:
            raise OmnitronError(
                "OMNITRON_BASE_URL is not set. Copy .env.example to .env.local "
                "and fill it in; the host is recorded in the vault under "
                "Omnitron API/Auth and conventions."
            )
        if not (self._username and self._password):
            raise OmnitronError(
                "OMNITRON_USERNAME and OMNITRON_PASSWORD are not set. They live "
                "in .env.local, which is gitignored."
            )
        # `timeout` is set explicitly because omnisdk sets none at all, and a
        # request with no timeout is how a nightly job hangs until someone
        # notices in the morning.
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)
        self._token: str | None = None
        self._login_times: list[float] = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def __enter__(self) -> OmnitronClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ── auth ───────────────────────────────────────────────────────────────

    def _login(self) -> str:
        """Exchange credentials for a token.

        The only POST this client makes. It takes no caller-supplied URL, so it
        cannot be repurposed into a general write path.
        """
        now = time.monotonic()
        self._login_times = [t for t in self._login_times if now - t < _LOGIN_WINDOW_S]
        if len(self._login_times) >= _MAX_LOGINS:
            raise OmnitronError(
                f"Login attempted {_MAX_LOGINS} times in {_LOGIN_WINDOW_S // 60} "
                "minutes. Refusing to retry — check the credentials in .env.local."
            )
        self._login_times.append(now)

        resp = self._http.post(
            f"{self.base_url}/api/i1/auth/login/",
            json={"username": self._username, "password": self._password},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise OmnitronError(
                f"Login failed: HTTP {resp.status_code}. "
                f"{resp.text[:200] if resp.text else ''}"
            )
        key = resp.json().get("key")
        if not key:
            raise OmnitronError("Login returned no 'key' field.")
        log.info("omnitron_login_ok", base_url=self.base_url)
        return key

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self._login()
        return self._token

    # ── requests ───────────────────────────────────────────────────────────

    def _request(
        self, method: str, path: str, *, _vouched: bool = False, **kwargs: Any
    ) -> httpx.Response:
        """The single exit point to the network for resource paths.

        Every call goes through here, and the first thing it does is refuse to
        be anything but a read unless one of the write doors — the methods that
        check the configuration and the payload — has vouched for it. Reaching
        past them with a bare `_request("DELETE", …)` is refused exactly as a
        bare POST always was.
        """
        if method != _ALLOWED_METHOD and not (_vouched and method in _VOUCHED_METHODS):
            opens_it = _VOUCHED_METHODS.get(method)
            raise ReadOnlyViolation(
                f"{method} {path} — writes go through their own door and carry "
                "only what it allows (vault, 05 - Decisions Log, 2026-09-07)."
                + (f" For {method}, that door is `{opens_it}`." if opens_it else "")
            )

        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
            **kwargs.pop("headers", {}),
        }
        while True:
            try:
                resp = self._http.request(
                    method,
                    url,
                    headers={**headers, "Authorization": f"Token {self.token}"},
                    **kwargs,
                )
            except httpx.TransportError as exc:
                # A dropped connection never reaches the status checks below —
                # it raises here — so retrying on status alone left every long
                # read one network blip away from dying. A 2,000-call image
                # pass died on `WinError 10054` after 169,003 rows, which is
                # not a rare event at that length: it is the expected one.
                if attempt >= _MAX_RETRIES:
                    raise OmnitronError(
                        f"GET {url} -> {type(exc).__name__} after "
                        f"{_MAX_RETRIES} retries: {exc}"
                    ) from exc
                wait = float(2**attempt)
                log.warning(
                    "omnitron_transport_retry",
                    path=path, error=type(exc).__name__, wait_s=wait, attempt=attempt,
                )
                time.sleep(wait)
                attempt += 1
                continue
            if resp.status_code == 401 and attempt == 0:
                # Their SDK has no path back from this; ours re-logins once.
                log.warning("omnitron_401_reauth", path=path)
                self._token = None
                attempt += 1
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                # `Retry-After` when the server offers it, exponential otherwise.
                wait = float(resp.headers.get("retry-after") or 2**attempt)
                log.warning(
                    "omnitron_retry",
                    path=path, status=resp.status_code, wait_s=wait, attempt=attempt,
                )
                time.sleep(wait)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise OmnitronError(
                    f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
            return resp

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        """One GET. Returns the decoded JSON body.

        `locale` sets `Accept-Language`, which is how Omnitron localises
        vocabulary labels and attribute display names. It is a header and only
        a header — `?locale=`, `?language=` and `?lang=` are all accepted and
        silently ignored, which is worse than an error because the response
        looks correct.
        """
        headers = {"Accept-Language": locale} if locale else {}
        return self._request("GET", path, params=params or {}, headers=headers).json()

    def iter_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        locale: str | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Walk a paginated list endpoint, yielding each page's `results`.

        Ordered by `id` unless the caller says otherwise. That matters: Omnitron
        paginates by offset, so an unordered walk over a set that changes
        underneath it can skip or repeat rows. Ordering does not make offset
        pagination safe, but it makes it deterministic — the real mitigation is
        a `modified_date__gte` filter that keeps the walk short.
        """
        query = {"limit": page_size, "page": 1, "sort": "id", **(params or {})}
        while True:
            body = self.get(path, params=query, locale=locale)
            results = body.get("results", [])
            if results:
                yield results
            if not body.get("next"):
                return
            query["page"] += 1

    def iter_records(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        locale: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Flatten `iter_pages` to one record at a time."""
        for page in self.iter_pages(path, params, page_size, locale=locale):
            yield from page

    # ── the one write ──────────────────────────────────────────────────────
    #
    # One method, one shape, no caller-supplied path. Added by decision on
    # 2026-09-07 (vault, 05 - Decisions Log) and moved to PATCH on 2026-09-08,
    # when the integration upsert turned out to be the wrong door: it is a
    # *full* upsert and rejects a partial body with
    # `{"name": ["This field is required"], "base_code": [...], ...}` — so
    # every write would have had to submit the product's core name, its
    # base code, its product_type and its attribute set alongside the one
    # attribute we came to change. Submitting `name` is exactly what this
    # design exists not to do: it is Omnitron's own translatable title, and
    # echoing it back under a market locale is how the Turkish master text
    # becomes a Polish "translation".
    #
    # `PATCH /api/v1/products/{pk}/` is a partial update, so the body carries
    # `attributes` and nothing else — there is no field here that could reach
    # `name`, `is_active`, `product_type` or an image. It is also addressed by
    # the product's own primary key rather than by sku, which closes the trap
    # the vault recorded about the upsert: "a typo'd SKU silently creates a
    # product rather than failing". A wrong pk is a 404.

    def patch_product(
        self,
        pk: int,
        attributes: dict[str, str],
        *,
        allowed_keys: Collection[str],
        locale: str | None,
        localizable_keys: Collection[str] = (),
    ) -> dict[str, Any]:
        """Change one product's attributes. The only write this client makes.

        `pk` is Omnitron's own product id, taken from a record this client
        just read — never constructed. `allowed_keys` is the set the caller's
        registry permits for the market being written (`sync.targets`), and
        every key must be among them. `localizable_keys` are the ones Omnitron
        holds per language; if any is present, `locale` must be one of ours,
        because a localizable key written without a header lands in the tenant
        default copy, which is the Turkish store's.

        Retried like a read on 429 and 5xx: a PATCH of the same fields to the
        same pk is the same write, not two.
        """
        if not settings.omnitron_writes_enabled:
            raise ReadOnlyViolation(
                "Writes are switched off (OMNITRON_WRITES_ENABLED is not set). "
                "Switching them on is a deliberate step, after the probe in "
                "scripts/probe_omnitron_write.py has been run."
            )
        if not isinstance(pk, int) or isinstance(pk, bool) or pk <= 0:
            raise WriteScopeViolation(
                f"{pk!r} is not an Omnitron product id. It must come from a "
                "record just read, never be constructed."
            )
        if not isinstance(attributes, dict) or not attributes:
            raise WriteScopeViolation("A write must carry a non-empty `attributes` object.")
        disallowed = sorted(k for k in attributes if k not in set(allowed_keys))
        if disallowed:
            raise WriteScopeViolation(
                f"{disallowed} are not allowed for this write: the registry permits "
                f"{sorted(allowed_keys)}."
            )
        if locale is not None and locale not in WRITE_LOCALES:
            raise WriteScopeViolation(
                f"locale {locale!r} is not one of ours ({sorted(WRITE_LOCALES)})."
            )
        needs_locale = sorted(k for k in attributes if k in set(localizable_keys))
        if needs_locale and locale is None:
            raise WriteScopeViolation(
                f"{needs_locale} are localizable and must be written under a market "
                "locale; without one the tenant default copy is overwritten."
            )
        headers = {"Accept-Language": locale} if locale else {}
        log.info(
            "omnitron_write",
            pk=pk, keys=sorted(attributes), locale=locale,
        )
        return self._request(
            "PATCH",
            f"/api/v1/products/{pk}/",
            _vouched=True,
            json={"attributes": attributes},
            headers=headers,
        ).json()

    # ── the catalogue door ─────────────────────────────────────────────────
    #
    # Taking a product off a market's website is a catalogue question, not a
    # product flag: `is_active: false` removes it from every channel at once,
    # Turkey included, and the vault records that there is no documented way to
    # deactivate a product in one market and leave it in another. What there
    # is, is `catalog_items` — `(product, catalog)`, unique, freely deletable
    # and freely re-creatable. So a takedown deletes rows here and a restore
    # posts them back.
    #
    # Two things make that safe enough to run against the live catalogue.
    #
    # **The item is read before it is deleted.** `allowed_catalogs` is the set
    # the caller believes it is working in, and the check is made against the
    # catalogue Omnitron says the item is in — not against the one the caller
    # passed alongside it. A caller that has muddled its pks is the whole
    # reason this guard exists, so trusting its arithmetic would be trusting
    # exactly the thing in doubt.
    #
    # **It has its own switch.** `OMNITRON_TAKEDOWN_ENABLED` is separate from
    # `OMNITRON_WRITES_ENABLED` and both must be set. Turning on attribute
    # pushes is a decision about text; turning on catalogue deletes is a
    # decision about whether this software may stop a product selling. They
    # are not the same decision and one should not silently grant the other.

    def _require_catalogue_writes(self) -> None:
        if not settings.omnitron_writes_enabled:
            raise ReadOnlyViolation(
                "Writes are switched off (OMNITRON_WRITES_ENABLED is not set)."
            )
        if not settings.omnitron_takedown_enabled:
            raise ReadOnlyViolation(
                "Catalogue writes are switched off "
                "(OMNITRON_TAKEDOWN_ENABLED is not set). It is separate from "
                "OMNITRON_WRITES_ENABLED on purpose: this is the switch that "
                "decides whether this software may stop a product selling."
            )

    @staticmethod
    def _require_pk(value: object, what: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise WriteScopeViolation(f"{value!r} is not {what}.")
        return value

    def delete_catalog_item(
        self, item_pk: int, *, allowed_catalogs: Collection[int]
    ) -> dict[str, Any]:
        """Remove one product from one catalogue. Returns the item deleted.

        `item_pk` is the `catalog_items` row id, taken from a record this
        client just read. The row is read again here, and the delete is only
        sent once Omnitron's own answer says it belongs to a catalogue in
        `allowed_catalogs`.

        The returned record is what makes this reversible: it carries the
        `product` and `catalog` pks that `create_catalog_item` needs to put
        the row back. Callers should persist it before acting on it — after
        the delete, this is the only place those two numbers exist.
        """
        self._require_catalogue_writes()
        self._require_pk(item_pk, "a catalog_items row id")
        permitted = {int(c) for c in allowed_catalogs}
        if not permitted:
            raise WriteScopeViolation(
                "A catalogue delete must name the catalogues it is allowed to "
                "touch. An empty allowlist permits nothing, and silently "
                "permitting everything is how a PL takedown reaches HU."
            )

        item = self.get(f"/api/v1/catalog_items/{item_pk}/")
        catalog = item.get("catalog")
        product = item.get("product")
        if catalog is None or product is None:
            raise WriteScopeViolation(
                f"catalog_item {item_pk} has no catalog or product: {item!r}. "
                "Refusing to delete a row this client cannot describe."
            )
        if int(catalog) not in permitted:
            raise WriteScopeViolation(
                f"catalog_item {item_pk} is in catalogue {catalog}, which is "
                f"not among the {sorted(permitted)} this write may touch. "
                "Deleting it would take the product off a market nobody asked "
                "about."
            )

        log.info(
            "omnitron_catalogue_delete",
            item_pk=item_pk, product=int(product), catalog=int(catalog),
        )
        self._request("DELETE", f"/api/v1/catalog_items/{item_pk}/", _vouched=True)
        return {"pk": item_pk, "product": int(product), "catalog": int(catalog)}

    def create_catalog_item(
        self, *, product_pk: int, catalog_pk: int, allowed_catalogs: Collection[int]
    ) -> dict[str, Any]:
        """Put one product back into one catalogue. The undo of the above.

        `(product, catalog)` is unique upstream, so re-creating a row that was
        never deleted is refused by Omnitron rather than duplicated — which
        makes a restore safe to retry.
        """
        self._require_catalogue_writes()
        self._require_pk(product_pk, "an Omnitron product id")
        self._require_pk(catalog_pk, "an Omnitron catalogue id")
        permitted = {int(c) for c in allowed_catalogs}
        if catalog_pk not in permitted:
            raise WriteScopeViolation(
                f"catalogue {catalog_pk} is not among the {sorted(permitted)} "
                "this write may touch."
            )

        log.info(
            "omnitron_catalogue_create", product=product_pk, catalog=catalog_pk
        )
        return self._request(
            "POST",
            "/api/v1/catalog_items/",
            _vouched=True,
            json={"product": product_pk, "catalog": catalog_pk},
        ).json()

    def catalog_items_for_product(
        self, product_pk: int, catalogs: Collection[int]
    ) -> list[dict[str, Any]]:
        """Every catalogue row this product has in the named catalogues.

        Read one catalogue at a time because `catalog__in` is silently ignored
        upstream — it returns the unfiltered set — which `sync.catalogue`
        found the hard way and is worth not rediscovering here.
        """
        rows: list[dict[str, Any]] = []
        for catalog in sorted({int(c) for c in catalogs}):
            rows.extend(
                record
                for record in self.iter_records(
                    "/api/v1/catalog_items/",
                    params={"catalog": catalog, "product": product_pk},
                )
                if int(record.get("product", 0)) == product_pk
                and int(record.get("catalog", 0)) == catalog
            )
        return rows
