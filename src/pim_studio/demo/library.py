"""The asset library and the photo server, as the image pipeline sees them.

Promoted from `FakeLibrary`, `FakeServer` (`tests/test_brandlibrary_publish.py`)
and `FakeClient` (`tests/test_brandlibrary_check.py`), with one difference that
matters: `MockLibrary` is the real `BrandLibraryClient`. Its paging, its
concurrent catalogue scan, its content-type check and its `check`/`test`
wording all run unchanged against the demo world's media API — only the
transport and the token exchange are swapped, as the client's own tests do
(`tests/test_brandlibrary_client.py`). Downloads are placeholder JPEGs drawn
in the option's colour with its code and the view on them.

`MockPhotoServer` implements the `Uploader` protocol from
`brandlibrary/photoserver.py` over a folder: `<UPLOADS_ROOT>/mock-ftp/<dir>/`.
The demo web transport serves that folder back at the photo server's public
address, so the handoff bundle downloads what was "uploaded".
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import httpx

from ..brandlibrary.client import (
    TOKEN_PATH,
    AuthFailed,
    BrandLibraryClient,
    BrandLibraryError,
)
from ..config import settings
from .web import mock_ftp_root, transport
from .world import World, current

_TIMEOUT = httpx.Timeout(60.0, connect=5.0)


class MockLibrary(BrandLibraryClient):
    """`BrandLibraryClient` against the demo's asset library."""

    def __init__(self, world: World | None = None) -> None:
        super().__init__(
            client_id="demo-client",
            client_secret="demo-placeholder",
            domain=settings.brand_library_domain or "assets.example.test",
            category=settings.brand_library_category,
            concurrency=4,
        )
        self.world = world or current()

    @property
    def _client(self) -> httpx.Client:
        existing = getattr(self._local, "client", None)
        if existing is None:
            existing = httpx.Client(
                base_url=self.base_url,
                headers={"User-Agent": self._user_agent, "Accept": "application/json"},
                transport=transport(self.world),
                timeout=_TIMEOUT,
                follow_redirects=True,
            )
            self._local.client = existing
        return existing

    def _access_token(self, force: bool = False) -> str:
        """The client-credentials exchange, through the demo transport."""
        with self._token_lock:
            if self._token and not force and time.time() < self._token_expires_at - 120:
                return str(self._token)
            response = self._client.post(
                TOKEN_PATH,
                data={"grant_type": "client_credentials", "client_id": self._client_id,
                      "client_secret": self._client_secret},
            )
            if response.status_code >= 400:
                raise AuthFailed(f"the demo library refused the token request "
                                 f"({response.status_code})")
            payload = response.json()
            self._token = str(payload["access_token"])
            self._token_expires_at = time.time() + float(payload.get("expires_in") or 3600)
            return self._token

    def _stream(self, url: str, dest: Path) -> int:
        """`BrandLibraryClient._stream`, through the demo transport."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        written = 0
        with self._client.stream("GET", url) as response:
            if response.status_code in (401, 403):
                raise AuthFailed(f"download blocked ({response.status_code})")
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes():
                    written += handle.write(chunk)
        if written == 0:
            partial.unlink(missing_ok=True)
            raise BrandLibraryError("downloaded 0 bytes")
        partial.replace(dest)
        return written


class MockPhotoServer:
    """An `Uploader` over a local folder standing in for the FTP account."""

    def __init__(self, remote_dir: str | None = None, root: Path | None = None) -> None:
        # Same rule as `PhotoServer`: a blank directory means the module's own
        # folder, never the account root.
        self.remote_dir = (remote_dir or settings.ftp_remote_dir or "pim-studio").strip("/")
        self.root = (root or mock_ftp_root()) / self.remote_dir

    def existing(self) -> set[str]:
        if not self.root.exists():
            return set()
        return {p.name for p in self.root.iterdir() if p.is_file()}

    def upload(self, local: Path, remote: str) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / remote
        partial = target.with_suffix(target.suffix + ".part")
        shutil.copyfile(local, partial)
        partial.replace(target)
        return target.stat().st_size

    def close(self) -> None:
        return None
