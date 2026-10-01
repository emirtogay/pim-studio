"""Where every client for an external system is built — the demo-mode switch.

Four systems sit outside this application: the PIM API, the brand's asset
library, the FTP photo server and the public storefronts (plus anything else
reached over plain HTTP). Every call site that needs one asks here instead of
constructing it, and this module is the only place that reads
`settings.demo_mode`. With it off, each factory returns exactly what the call
site used to build; with it on, the mock from `pim_studio.demo`, fed from one
synthetic catalogue, and nothing leaves the process.

Keeping the decision in one file is the point. A demo flag tested at twenty
call sites is twenty places for the next change to forget, and the first one
forgotten reaches a real system from a demo.

`real=` exists for call sites whose tests replace the client by patching the
name in that module (`imports_api.OmnitronClient`, `sync.__main__.OmnitronClient`).
They pass the name they import, so the patch still lands.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from .brandlibrary.client import BrandLibraryClient
from .brandlibrary.photoserver import PhotoServer, Uploader
from .config import settings
from .omnitron.client import OmnitronClient

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session


def demo_mode() -> bool:
    """Whether the external systems are mocked in this process."""
    return bool(settings.demo_mode)


def pim_client(real: Callable[[], OmnitronClient] | None = None) -> OmnitronClient:
    """The PIM API client: `OmnitronClient()`, or the demo's `MockPim`.

    Raises what `OmnitronClient()` raises when the PIM is not configured —
    callers already turn that into their own refusal.
    """
    if demo_mode():
        from .demo import MockPim

        return MockPim()
    return (real or OmnitronClient)()


def brand_library_client(
    db: Session | None = None, **credentials: Any
) -> BrandLibraryClient:
    """The asset library client.

    With no `credentials`, `brandlibrary.session.build(db)` picks the best one
    on record — OAuth, then a pasted cookie, then the environment. With them
    (a pasted cookie being tested before it is stored), they are used as given.
    In demo mode, the demo's `MockLibrary` either way.
    """
    if demo_mode():
        from .demo import MockLibrary

        return MockLibrary()
    if credentials:
        return BrandLibraryClient(**credentials)
    if db is None:
        raise ValueError("brand_library_client needs a session to read the stored credential")
    from .brandlibrary import session as session_store

    return session_store.build(db)


def photo_server(**options: Any) -> Uploader:
    """The FTP photo server, or the demo's folder-backed `MockPhotoServer`.

    `options` are `PhotoServer`'s own arguments; the mock reads only
    `remote_dir`.
    """
    if demo_mode():
        from .demo import MockPhotoServer

        return MockPhotoServer(remote_dir=options.get("remote_dir"))
    return PhotoServer(**options)


def http_client(**options: Any) -> httpx.Client:
    """An `httpx.Client` for plain HTTP: the storefronts, the photo server.

    In demo mode the client carries a mock transport that answers every demo
    host and refuses everything else, so nothing reaches the network.
    """
    if demo_mode():
        from .demo import transport

        return httpx.Client(transport=transport(), **options)
    return httpx.Client(**options)


def http_get(url: str, **options: Any) -> httpx.Response:
    """`httpx.get`, or the same request answered by the demo transport."""
    if demo_mode():
        follow = options.pop("follow_redirects", False)
        with http_client(follow_redirects=follow, **options) as client:
            return client.get(url)
    return httpx.get(url, **options)
