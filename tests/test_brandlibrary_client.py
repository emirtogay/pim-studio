"""The brand library client, against the shapes its instance actually returns.

Every constant asserted here was a guess before the original `brandlib_core`
was read, and every guess was wrong. They are pinned as tests because the next
person to touch this file will have Bynder's public documentation to hand,
which disagrees with this instance in six places.

The most important test in the file is the HTML one: a dead cookie answers 200
with a login page, not 401. Parsed as JSON that is an empty library, so an
expired session reads as "the brand has no photos for any of these options" —
and several hundred of them get taken to the brand.
"""

from __future__ import annotations

import httpx
import pytest

from pim_studio.brandlibrary.client import (
    CATEGORY_PROPERTY,
    ECOMMERCE,
    SCOPE,
    TOKEN_PATH,
    YEAR_PROPERTY,
    ZALANDO_KEY,
    AuthFailed,
    BrandLibraryClient,
    ReadOnlyViolation,
    asset_from,
    parse_cookie,
)


def client(handler) -> BrandLibraryClient:
    """A client whose transport is a function over (method, url)."""
    c = BrandLibraryClient(cookie="SESS=abc", domain="library.test")
    c._local.client = httpx.Client(
        base_url="https://library.test",
        headers=c._headers,
        transport=httpx.MockTransport(handler),
    )
    return c


def json_response(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


# ── the shapes this instance actually uses ─────────────────────────────────


def test_the_year_filter_is_property_year() -> None:
    """Not `property_season_year`, which is what Bynder's docs suggest."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return json_response({"total": {"count": 0}, "media": []})

    client(handler).count("2026")

    assert seen[YEAR_PROPERTY] == "2026"
    assert YEAR_PROPERTY == "property_Year"


def test_the_catalogue_is_filtered_to_ecommerce() -> None:
    """Without it the scan roughly triples and pulls in campaign assets that
    would publish onto product pages."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return json_response({"total": {"count": 0}, "media": []})

    client(handler).count()

    assert seen[CATEGORY_PROPERTY] == ECOMMERCE


def test_the_total_is_asked_for_and_read_the_instance_way() -> None:
    """`total=1` in, `{"total": {"count": N}}` out — both differ from the docs."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return json_response({"total": {"count": 48210}, "media": []})

    assert client(handler).count() == 48210
    assert seen["total"] == "1"


def test_a_browser_shape_is_sent_or_the_cookie_is_not_honoured() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return json_response({"total": {"count": 1}, "media": []})

    client(handler).count()

    assert seen["x-requested-with"] == "XMLHttpRequest"
    assert seen["referer"] == "https://library.test/"
    assert "Mozilla" in seen["user-agent"]
    assert seen["cookie"] == "SESS=abc"


# ── the failure that reads as success ──────────────────────────────────────


def test_an_html_login_page_is_an_auth_error_not_an_empty_library() -> None:
    """The single most important line in the client.

    A dead cookie answers 200 with HTML. Parsed as JSON it is a library holding
    nothing, which is indistinguishable from every option genuinely lacking a
    photo — and the run would record hundreds of absences and send somebody to
    the brand for photos that exist.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Sign in</body></html>")

    with pytest.raises(AuthFailed, match="login page"):
        client(handler).count()


def test_a_401_is_an_auth_error_with_the_remedy_in_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    with pytest.raises(AuthFailed, match="cookie"):
        client(handler).count()


def test_test_reports_zero_visible_assets_as_a_failure() -> None:
    """Signed in with nothing visible is a permissions problem wearing a
    success, and would otherwise start an eight-minute scan of nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"total": {"count": 0}, "media": []})

    ok, message = client(handler).test()

    assert ok is False
    assert "no assets are visible" in message


def test_a_client_with_no_cookie_says_where_to_get_one() -> None:
    with pytest.raises(AuthFailed, match="Images page"):
        BrandLibraryClient(cookie="")


def test_the_client_refuses_to_write() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return json_response({})

    with pytest.raises(ReadOnlyViolation):
        client(handler).request("POST", "/api/v4/media/")


# ── assets ─────────────────────────────────────────────────────────────────


def test_metaproperties_arrive_as_one_element_lists() -> None:
    asset = asset_from(
        {
            "id": 12,
            "name": "AF0276_J2G_32",
            "extension": ["jpg"],
            "property_BI_PRODUCT_ID": ["AF0276"],
            "property_BI_COLOR_ID": ["J2G"],
        }
    )

    assert asset.product_id == "AF0276"
    assert asset.color_id == "J2G"
    assert asset.extension == "jpg"


def test_the_zalando_edition_lives_under_its_marketplace_key() -> None:
    """`MKP_ZAL`, not `zalando`."""
    asset = asset_from({"id": "1", "name": "x", "thumbnails": {ZALANDO_KEY: "https://cdn/z"}})

    assert asset.has_zalando
    assert asset.edition_url("zalando") == "https://cdn/z"
    assert ZALANDO_KEY == "MKP_ZAL"


def test_the_original_is_a_top_level_field_not_a_derivative() -> None:
    """It is not in `thumbnails`, and it does not need the download endpoint —
    that is only its fallback when the signed URL has expired."""
    asset = asset_from({"id": "1", "name": "x", "original": "https://cdn/o.jpg"})

    assert asset.edition_url("original") == "https://cdn/o.jpg"


def test_an_assets_own_extension_is_kept() -> None:
    """Publishing a PNG as .jpg produces a file browsers refuse to render."""
    asset = asset_from({"id": "1", "name": "shot", "extension": ["png"]})

    assert asset.filename == "shot.png"


def test_an_extension_already_on_the_name_is_not_doubled() -> None:
    asset = asset_from({"id": "1", "name": "shot.jpg", "extension": "jpg"})

    assert asset.filename == "shot.jpg"


# ── pasting a cookie ───────────────────────────────────────────────────────


def test_a_raw_cookie_header_is_accepted() -> None:
    assert parse_cookie("SESS=abc; other=1") == ("SESS=abc; other=1", None)


def test_a_cookie_prefix_is_stripped() -> None:
    assert parse_cookie("Cookie: SESS=abc")[0] == "SESS=abc"


def test_a_copy_as_curl_is_accepted_because_that_is_what_people_paste() -> None:
    """Nobody types a cookie. They right-click a request and copy it as cURL."""
    curl = (
        "curl 'https://library.test/api/v4/media/?type=image' "
        "-H 'cookie: SESS=abc; other=1' "
        "-H 'user-agent: Mozilla/5.0 (Windows NT 10.0)' --compressed"
    )

    cookie, user_agent = parse_cookie(curl)

    assert cookie == "SESS=abc; other=1"
    assert user_agent == "Mozilla/5.0 (Windows NT 10.0)"


def test_a_curl_using_the_cookie_flag_is_also_accepted() -> None:
    cookie, _ = parse_cookie("curl https://library.test -b 'SESS=abc'")

    assert cookie == "SESS=abc"


def test_nothing_pasted_is_nothing_parsed() -> None:
    assert parse_cookie("") == (None, None)
    assert parse_cookie("   ") == (None, None)


def test_a_paste_that_is_not_a_cookie_is_refused() -> None:
    """A cookie header is name=value pairs. Without this check any stray paste
    is accepted as a credential and surfaces later as a failed run."""
    assert parse_cookie("hello") == (None, None)
    assert parse_cookie("https://assets.example.test/") == (None, None)


# ── OAuth client credentials ───────────────────────────────────────────────
#
# The mode that makes the weekly run unattended. Everything below is about the
# difference between a job that needs a person each Tuesday and one that does
# not, so it is worth pinning tightly.


def oauth_client(handler, **kw) -> BrandLibraryClient:
    c = BrandLibraryClient(
        client_id="id-1", client_secret="secret-1", domain="library.test", **kw
    )
    transport = httpx.MockTransport(handler)
    c._local.client = httpx.Client(base_url="https://library.test", transport=transport)
    # The token exchange is a bare httpx.post, not the pooled client, so it is
    # routed by patching the module's httpx.
    return c


def token_and_media(token_calls: list, media_response=None, media_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if TOKEN_PATH in request.url.path:
            token_calls.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200,
                json={
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "access_token": f"tok-{len(token_calls)}",
                    "scope": SCOPE,
                },
            )
        if media_status != 200:
            return httpx.Response(media_status, json={})
        return httpx.Response(200, json=media_response or {"total": {"count": 7}, "media": []})

    return handler


def test_oauth_is_preferred_and_sends_a_bearer_token(monkeypatch) -> None:
    calls: list = []
    seen: dict = {}
    handler = token_and_media(calls)

    def spy(request: httpx.Request) -> httpx.Response:
        if TOKEN_PATH not in request.url.path:
            seen.update({k.lower(): v for k, v in request.headers.items()})
        return handler(request)

    transport = httpx.MockTransport(spy)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw)
    )
    client = oauth_client(spy)

    assert client.mode == "oauth"
    assert client.count() == 7
    assert seen["authorization"] == "Bearer tok-1"
    assert "cookie" not in seen


def test_the_token_request_asks_for_client_credentials_and_asset_read(monkeypatch) -> None:
    """Exactly the grant and scope Bynder documents — and no more than
    `asset:read`, because there is no code path here that writes."""
    calls: list = []
    handler = token_and_media(calls)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw)
    )

    oauth_client(handler).count()

    assert calls[0]["grant_type"] == "client_credentials"
    assert calls[0]["client_id"] == "id-1"
    assert calls[0]["scope"] == SCOPE
    assert SCOPE == "asset:read"


def test_one_token_serves_many_requests(monkeypatch) -> None:
    """A token per page would be ~1,200 token requests per run, and the library
    rate-limits."""
    calls: list = []
    handler = token_and_media(calls)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw)
    )
    client = oauth_client(handler)

    for _ in range(5):
        client.count()

    assert len(calls) == 1


def test_a_revoked_token_is_reminted_once_rather_than_losing_the_run(monkeypatch) -> None:
    """An hour-long token can be revoked early. Re-minting once is the
    difference between recovering and stopping eight minutes in."""
    calls: list = []
    state = {"rejections": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        if TOKEN_PATH in request.url.path:
            calls.append(1)
            return httpx.Response(
                200, json={"access_token": f"tok-{len(calls)}", "expires_in": 3600}
            )
        if state["rejections"]:
            state["rejections"] -= 1
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"total": {"count": 3}, "media": []})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw)
    )

    assert oauth_client(handler).count() == 3
    assert len(calls) == 2  # minted, rejected, minted again


def test_bad_client_credentials_name_the_setting_and_the_scope(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw)
    )

    with pytest.raises(AuthFailed, match="BRAND_LIBRARY_CLIENT_ID"):
        oauth_client(handler).count()


def test_a_client_with_neither_credential_says_both_ways_in() -> None:
    with pytest.raises(AuthFailed, match="OAuth client id"):
        BrandLibraryClient()


def test_test_reports_which_way_it_got_in(monkeypatch) -> None:
    """The difference decides whether anybody has to do anything each week."""
    calls: list = []
    handler = token_and_media(calls)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw)
    )

    ok, message = oauth_client(handler).test()

    assert ok
    assert "signs itself in" in message


def test_a_cookie_client_says_the_cookie_will_expire() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"total": {"count": 5}, "media": []})

    ok, message = client(handler).test()

    assert ok
    assert "expires within hours" in message


def test_only_the_number_gets_a_spaced_thousands_separator() -> None:
    """Spacing the whole sentence also ate the comma out of "a pasted cookie,
    which expires", leaving a double space mid-clause."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"total": {"count": 48210}, "media": []})

    _, message = client(handler).test()

    assert "48 210 photos visible" in message
    assert "pasted cookie, which expires" in message
    assert "  " not in message
