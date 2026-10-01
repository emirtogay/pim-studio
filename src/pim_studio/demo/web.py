"""One `httpx` transport for every demo host.

`integrations.http_client()` hands this to any code that would otherwise make a
plain HTTP request, so the code under it — the storefront crawl, the
publication check, the bundle's photo download — runs exactly as written and
simply finds the demo on the other end. Routing is by host:

    pim.example.test              the PIM API (`pim.handle`)
    northwind-{pl,cz,sk,hu}.…     the four public shops, `/list?format=json`
    assets.example.test           the asset library's API and its files
    photos.example.test           the photo server's public folder

A host the demo does not know answers 502 rather than reaching for the network:
an unmocked request is a bug in the demo, and it should look like one.

The shop listing is the shape `FakeShop` in `tests/test_sync_publication.py`
serves — a card per option, `integration_model_renk` and the market's content
keys under `attributes`, paged with a `total_count` it keeps to.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..config import settings
from ..sync.storefront import STOREFRONTS
from . import pim
from .placeholder import render
from .world import World, current

_SHOP_HOSTS: dict[str, str] = {
    urlsplit(shop.origin).hostname or "": market for market, shop in STOREFRONTS.items()
}

#: Where the demo library's API and files answer — whatever the configured
#: domain, since the asset records name this host for their files.
LIBRARY_HOST = "assets.example.test"

#: `/m/<asset id>/<edition>/<name>.jpg` on the asset library's file host.
_ASSET_FILE = re.compile(r"^/m/(?P<id>[^/]+)/(?P<edition>[^/]+)/(?P<name>[^/]+)$")
_MEDIA_ITEM = re.compile(r"^/api/v4/media/(?P<id>[^/]+)/download/?$")


def mock_ftp_root() -> Path:
    """Where `MockPhotoServer` keeps what it was sent: `<UPLOADS_ROOT>/mock-ftp`."""
    return Path(settings.uploads_root) / "mock-ftp"


def _host(url: str | None, fallback: str) -> str:
    return (urlsplit(url).hostname if url else None) or fallback


def _shop(world: World, market: str, request: httpx.Request) -> httpx.Response:
    if request.url.path.rstrip("/") != "/list":
        return httpx.Response(404, text="not found")
    rows = world.listing(market)
    size = max(1, int(request.url.params.get("page_size") or 24))
    page = max(1, int(request.url.params.get("page") or 1))
    pages = max(1, -(-len(rows) // size))
    return httpx.Response(
        200,
        json={
            "pagination": {
                "num_pages": pages,
                "total_count": len(rows),
                "current_page": page,
            },
            "products": rows[(page - 1) * size : page * size],
        },
    )


def _library(world: World, request: httpx.Request) -> httpx.Response:
    """The asset library: its media API, its token endpoint, and its files."""
    path = request.url.path
    if path.rstrip("/") == "/v6/authentication/oauth2/token":
        return httpx.Response(200, json={"access_token": "demo-token", "expires_in": 3600})
    if path.rstrip("/") == "/api/v4/media":
        params = request.url.params
        records = [
            r for r in world.assets()
            if (params.get("type") in (None, r.get("type")))
            and (not params.get("property_2017_Category")
                 or params["property_2017_Category"] in r.get("property_2017_Category", []))
            and (not params.get("property_Year")
                 or params["property_Year"] in r.get("property_Year", []))
        ]
        limit = max(1, int(params.get("limit") or 50))
        page = max(1, int(params.get("page") or 1))
        body: dict[str, object] = {"media": records[(page - 1) * limit : page * limit]}
        if params.get("total"):
            body["total"] = {"count": len(records)}
        return httpx.Response(200, json=body)
    if match := _MEDIA_ITEM.match(path):
        for record in world.assets():
            if record["id"] == match.group("id"):
                return httpx.Response(200, json={"s3_file": record["original"]})
        return httpx.Response(404, json={"detail": "no such asset"})
    if match := _ASSET_FILE.match(path):
        return _photo(world, match.group("id"), match.group("edition"), match.group("name"))
    return httpx.Response(404, json={"detail": f"no {path}"})


def _photo(world: World, asset_id: str, edition: str, name: str) -> httpx.Response:
    """A placeholder JPEG for one asset edition: the option's colour, its code
    and which view it is."""
    stem = name.rsplit(".", 1)[0]
    by_key = {o.library_key: o for o in world.catalog.options}
    key, _, view = stem.rpartition("_")
    option = by_key.get(key.removeprefix("swatch_")) or by_key.get(stem.removeprefix("swatch_"))
    if option is None:
        rgb, code = (120, 120, 120), stem
    else:
        rgb, code = option.colour.rgb, option.model_color
    label = {
        "01": "FRONT", "02": "BACK", "03": "DETAIL", "04": "ON MODEL",
    }.get(view, "SWATCH" if stem.startswith("swatch_") else "CAMPAIGN")
    if edition == "marketplace":
        label = f"{label} · MARKETPLACE CROP"
    payload = render(code, label, rgb, size=(480, 640) if edition != "webimage" else (240, 320))
    return httpx.Response(200, content=payload, headers={"Content-Type": "image/jpeg"})


def _photo_server(request: httpx.Request) -> httpx.Response:
    """The photo server's public folder, read back from `mock-ftp`."""
    root = mock_ftp_root().resolve()
    candidate = (root / request.url.path.lstrip("/")).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return httpx.Response(404, text="not found")
    return httpx.Response(
        200, content=candidate.read_bytes(), headers={"Content-Type": "image/jpeg"}
    )


def handler(world: World | None = None):  # noqa: ANN201 — an httpx handler
    """The request handler for every demo host, bound to one world."""

    def handle(request: httpx.Request) -> httpx.Response:
        bound = world or current()
        host = request.url.host
        if host == _host(pim.BASE_URL, "pim.example.test"):
            return pim.handle(bound, request)
        if host in _SHOP_HOSTS:
            return _shop(bound, _SHOP_HOSTS[host], request)
        if host in {LIBRARY_HOST, _host(f"https://{settings.brand_library_domain}", "")}:
            return _library(bound, request)
        if host == _host(settings.ftp_base_url, "photos.example.test"):
            return _photo_server(request)
        return httpx.Response(
            502, text=f"demo mode: no mock answers for {host}, and nothing real is reached"
        )

    return handle


def transport(world: World | None = None) -> httpx.MockTransport:
    """A transport that answers every demo host and nothing else."""
    return httpx.MockTransport(handler(world))
