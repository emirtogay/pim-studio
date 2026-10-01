"""The Omnitron client — and specifically, that it cannot write.

The read-only decision (vault, `05 - Decisions Log`, 2026-08-14) is only worth
anything if it is enforced rather than agreed to. These tests are the
enforcement's enforcement.
"""

from __future__ import annotations

import httpx
import pytest

from pim_studio.omnitron import OmnitronClient, OmnitronError, ReadOnlyViolation
from pim_studio.omnitron.client import DEFAULT_PAGE_SIZE, WriteScopeViolation

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}


def client(handler) -> OmnitronClient:
    """A client whose transport is a callable, so nothing touches the network."""
    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def logged_in(handler) -> OmnitronClient:
    c = client(handler)
    c._token = "test-token"  # skip the login round trip
    return c


# ── read-only ──────────────────────────────────────────────────────────────


def test_the_write_methods_are_named_and_nothing_generic_exists() -> None:
    """The first line of defence: there is nothing general-purpose to call.

    A guard that raises can be bypassed by someone reaching for `_request`
    directly; a method that does not exist cannot be called by accident at all.
    Each door is named after exactly what it may do, and the terms of each are
    the tests below. A bare `post` or `delete` appearing here would be a door
    that says nothing about what goes through it.
    """
    for verb in ("post", "patch", "put", "delete", "create", "update", "upsert"):
        assert not hasattr(OmnitronClient, verb), f"OmnitronClient grew a `{verb}`"
    assert hasattr(OmnitronClient, "patch_product")
    assert hasattr(OmnitronClient, "delete_catalog_item")
    assert hasattr(OmnitronClient, "create_catalog_item")


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"])
def test_request_refuses_every_verb_but_get(method: str) -> None:
    """The second line: even reaching past the public API gets stopped."""
    c = logged_in(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ReadOnlyViolation) as e:
        c._request(method, "/api/v1/products/")
    assert method in str(e.value)


def test_read_only_violation_is_an_omnitron_error() -> None:
    """So a caller catching OmnitronError cannot accidentally swallow it as a
    different class of problem and carry on."""
    assert issubclass(ReadOnlyViolation, OmnitronError)


def test_no_write_ever_reaches_the_transport() -> None:
    """The guarantee that actually matters: nothing leaves the process."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"results": [], "next": None})

    c = logged_in(handler)
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        with pytest.raises(ReadOnlyViolation):
            c._request(method, "/api/v1/products/")
    c.get("/api/v1/attributes/")

    assert [m for m, _ in seen] == ["GET"]


# ── auth ───────────────────────────────────────────────────────────────────


def test_login_is_the_only_post_and_takes_no_caller_url() -> None:
    """The one carve-out. It builds its own URL, so it cannot be repurposed."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/auth/login/"):
            return httpx.Response(200, json={"key": "abc123"})
        return httpx.Response(200, json={"results": [], "next": None})

    c = client(handler)
    c.get("/api/v1/attributes/")
    assert seen[0] == ("POST", "/api/i1/auth/login/")
    assert seen[1][0] == "GET"


def test_token_header_is_token_not_bearer() -> None:
    """Verified against the live instance: `WWW-Authenticate: Token`."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={})

    logged_in(handler).get("/api/v1/attributes/")
    assert captured["authorization"] == "Token test-token"


def test_401_triggers_exactly_one_reauth() -> None:
    """Akinon's own SDK has no path back from a 401. Ours re-logins once — and
    only once, so a genuinely bad credential fails instead of looping."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/login/"):
            return httpx.Response(200, json={"key": "fresh"})
        if len([c for c in calls if "attributes" in c]) == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"ok": True})

    c = logged_in(handler)
    assert c.get("/api/v1/attributes/") == {"ok": True}
    assert calls.count("/api/i1/auth/login/") == 1


def test_repeated_login_failures_stop_rather_than_hammer() -> None:
    """The breaker copied from `channel_app`. Without it, bad credentials turn
    into an unbounded loop against the login endpoint."""
    c = client(lambda r: httpx.Response(400, json={"detail": "bad"}))
    for _ in range(3):
        with pytest.raises(OmnitronError):
            c._login()
    with pytest.raises(OmnitronError, match="Refusing to retry"):
        c._login()


# ── pagination ─────────────────────────────────────────────────────────────


def test_iter_pages_walks_until_next_is_empty() -> None:
    pages = [
        {"results": [{"id": 1}, {"id": 2}], "next": "?page=2"},
        {"results": [{"id": 3}], "next": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["page"]) - 1])

    got = list(logged_in(handler).iter_records("/api/v1/attributes/"))
    assert [r["id"] for r in got] == [1, 2, 3]


def test_pagination_defaults_are_ordered_and_bounded() -> None:
    """`sort=id` because Omnitron paginates by offset and an unordered walk over
    a shifting set can skip or repeat rows. 100 because that is the largest
    Akinon's own code ever asks for and no ceiling is documented."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"results": [], "next": None})

    list(logged_in(handler).iter_pages("/api/v1/attributes/"))
    assert seen["sort"] == "id"
    assert seen["limit"] == str(DEFAULT_PAGE_SIZE)


def test_caller_params_survive_and_can_override() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"results": [], "next": None})

    list(logged_in(handler).iter_pages(
        "/api/v1/product_price/",
        params={"modified_date__gte": "2026-08-01", "sort": "pk"},
    ))
    assert seen["modified_date__gte"] == "2026-08-01"
    assert seen["sort"] == "pk"


# ── locale ─────────────────────────────────────────────────────────────────


def test_locale_becomes_the_accept_language_header() -> None:
    """Locale is a header and only a header — `?locale=` is silently ignored
    by Omnitron, so the header is the one channel that actually works."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={})

    logged_in(handler).get("/api/v1/products/", locale="pl")
    assert captured["accept-language"] == "pl"


def test_no_locale_sends_no_accept_language_at_all() -> None:
    """Absent, not defaulted: the tenant default answering is a semantic the
    values stage depends on (the baseline view), so the client must not invent
    a language behind the caller's back."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={})

    logged_in(handler).get("/api/v1/products/")
    assert "accept-language" not in captured


def test_iter_records_carries_the_locale_across_pages() -> None:
    """A pagination walk that dropped the header after page one would return a
    response that is half Polish, half Turkish, and looks fine."""
    seen: list[str | None] = []
    pages = [
        {"results": [{"id": 1}], "next": "?page=2"},
        {"results": [{"id": 2}], "next": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-language"))
        return httpx.Response(200, json=pages[int(request.url.params["page"]) - 1])

    got = list(logged_in(handler).iter_records("/api/v1/attribute_value/", locale="cs"))
    assert [r["id"] for r in got] == [1, 2]
    assert seen == ["cs", "cs"]


# ── retries ────────────────────────────────────────────────────────────────


def test_429_is_retried_not_fatal(monkeypatch) -> None:
    """Akinon's SDK gives up on anything below 499, so it treats a rate limit as
    fatal. That is survivable for an attended run and not for a nightly job."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        return httpx.Response(200, json={"ok": True})

    assert logged_in(handler).get("/api/v1/attributes/") == {"ok": True}
    assert attempts["n"] == 2


def test_a_4xx_that_is_not_retryable_raises_with_context(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    c = logged_in(lambda r: httpx.Response(404, text="no such thing"))
    with pytest.raises(OmnitronError, match="404"):
        c.get("/api/v1/nope/")


# ── configuration ──────────────────────────────────────────────────────────


def test_missing_config_fails_with_an_actionable_message() -> None:
    """Explicitly empty is an error; omitted falls back to settings.

    Pinned because collapsing those two made this test's outcome depend on
    whether the machine running it happened to have a `.env.local`.
    """
    with pytest.raises(OmnitronError, match="OMNITRON_BASE_URL"):
        OmnitronClient(base_url="", username="u", password="p")
    with pytest.raises(OmnitronError, match="OMNITRON_USERNAME"):
        OmnitronClient(base_url="https://x", username="", password="")


def test_base_url_trailing_slash_does_not_double_up() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    c = OmnitronClient(base_url="https://x.net/", username="u", password="p")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "t"
    c.get("/api/v1/attributes/")
    assert seen[0].startswith("https://x.net/api/v1/attributes/")


# ── transport failures ─────────────────────────────────────────────────────


def test_a_dropped_connection_is_retried_not_fatal() -> None:
    """The hole that killed a 2,000-call image pass after 169,003 rows.

    A dropped connection raises out of `request()` and never reaches the status
    checks, so retrying on status alone left every long read one network blip
    away from dying — and at that length a blip is the expected event, not a
    rare one.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadError("[WinError 10054] forcibly closed")
        return httpx.Response(200, json={"ok": True})

    c = logged_in(handler)
    assert c.get("/api/v1/products/") == {"ok": True}
    assert calls["n"] == 3, "should have retried twice before succeeding"


def test_transport_failure_gives_up_as_an_omnitron_error() -> None:
    """So a caller catching OmnitronError sees a persistent network failure
    the same way it sees any other read failure, rather than an httpx type
    leaking out of the client."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = logged_in(handler)
    with pytest.raises(OmnitronError) as e:
        c.get("/api/v1/products/")
    assert "ConnectError" in str(e.value)


# ── the one write ──────────────────────────────────────────────────────────
#
# `patch_product` is the door. These are its terms: shut unless configured,
# no caller-supplied path at all, only the keys the registry allowed for the
# market, and a locale on anything Omnitron holds per language.


def _writes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", True)


def test_the_write_is_shut_until_writes_are_switched_on(monkeypatch) -> None:
    from pim_studio.config import settings

    monkeypatch.setattr(settings, "omnitron_writes_enabled", False)
    seen: list[str] = []
    c = logged_in(lambda r: (seen.append(r.method), httpx.Response(200, json={}))[1])
    with pytest.raises(ReadOnlyViolation, match="switched off"):
        c.patch_product(1, {"urun_adi_pl": "Polo"}, allowed_keys={"urun_adi_pl"}, locale="pl")
    assert seen == []


def test_the_product_id_must_come_from_a_record_not_be_constructed(monkeypatch) -> None:
    _writes_on(monkeypatch)
    seen: list[str] = []
    c = logged_in(lambda r: (seen.append(r.method), httpx.Response(200, json={}))[1])
    for bad in (0, -1, "357545", None, True):
        with pytest.raises(WriteScopeViolation, match="Omnitron product id"):
            c.patch_product(
                bad, {"urun_adi_pl": "Polo"}, allowed_keys={"urun_adi_pl"}, locale="pl"
            )
    assert seen == []


@pytest.mark.parametrize(
    ("attributes", "reason"),
    [
        ({}, "non-empty"),
        ({"urun_adi_ru": "x"}, "urun_adi_ru"),
        ({"integration_kalem_icerik": "x"}, "integration_kalem_icerik"),
        ({"name": "x"}, "name"),
        ({"is_active": "false"}, "is_active"),
    ],
)
def test_a_write_refuses_any_key_the_registry_did_not_allow(
    monkeypatch, attributes, reason
) -> None:
    """There is no field here that could reach `name`, `is_active` or an
    image: the body is `attributes` and the keys are checked against the set
    the caller's registry permits for that market."""
    _writes_on(monkeypatch)
    seen: list[str] = []
    c = logged_in(lambda r: (seen.append(r.method), httpx.Response(200, json={}))[1])
    with pytest.raises(WriteScopeViolation, match=reason):
        c.patch_product(1, attributes, allowed_keys={"urun_adi_pl"}, locale="pl")
    assert seen == []


def test_a_localizable_key_needs_one_of_our_locales(monkeypatch) -> None:
    _writes_on(monkeypatch)
    c = logged_in(lambda r: httpx.Response(200, json={}))
    with pytest.raises(WriteScopeViolation, match="localizable"):
        c.patch_product(
            1, {"urun_adi_pl": "Polo"}, allowed_keys={"urun_adi_pl"},
            locale=None, localizable_keys={"urun_adi_pl"},
        )
    for bad in ("tr-tr", "en", "hu-HU"):
        with pytest.raises(WriteScopeViolation, match="not one of ours"):
            c.patch_product(
                1, {"urun_adi_pl": "Polo"}, allowed_keys={"urun_adi_pl"},
                locale=bad, localizable_keys={"urun_adi_pl"},
            )


def test_a_valid_write_patches_the_product_by_id_under_the_locale(monkeypatch) -> None:
    _writes_on(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"pk": 357545})

    c = logged_in(handler)
    out = c.patch_product(
        357545, {"urun_adi_pl": "Koszulka polo"},
        allowed_keys={"urun_adi_pl", "urun_detay_pol"}, locale="pl",
        localizable_keys={"urun_adi_pl"},
    )
    assert out == {"pk": 357545}
    (req,) = seen
    assert (req.method, req.url.path) == ("PATCH", "/api/v1/products/357545/")
    assert req.headers["accept-language"] == "pl"
    assert req.headers["authorization"] == "Token test-token"
    import json as _json

    # `attributes` and nothing else — no name, no sku, no product_type.
    assert _json.loads(req.content) == {"attributes": {"urun_adi_pl": "Koszulka polo"}}


def test_a_write_of_only_plain_keys_sends_no_locale_header(monkeypatch) -> None:
    _writes_on(monkeypatch)
    seen: list[httpx.Request] = []
    c = logged_in(lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    c.patch_product(1, {"PL_sezon": "26AW"}, allowed_keys={"PL_sezon"}, locale=None)
    assert "accept-language" not in seen[0].headers


def test_a_bare_request_write_is_still_refused_with_writes_on(monkeypatch) -> None:
    """`patch_product` vouches for its call; nothing else can."""
    _writes_on(monkeypatch)
    c = logged_in(lambda r: httpx.Response(200, json={}))
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        with pytest.raises(ReadOnlyViolation):
            c._request(method, "/api/v1/products/1/", json={"attributes": {}})
