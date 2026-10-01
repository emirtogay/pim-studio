"""The SPA catch-all may not serve anything outside the static directory.

`GET /{path:path}` is the one route in the app with no authentication — it has
to be, because it answers a hard refresh on `/products/L3539.132` with
`index.html` before anybody has a session. That makes it the route where a
path-traversal bug is worth the most to an attacker and costs the least to
reach, and it had one.

The mechanism is worth stating precisely, because the intuition that "the
framework normalises the URL" is what let it stand. ASGI hands the handler
`scope["path"]` **percent-decoded and not normalised**: uvicorn removes no dot
segments, so `/../../../../.env.local` and its encoded spellings arrive at the
handler exactly as written. The handler joined that onto `STATIC_DIR` and
served whatever `is_file()` accepted.

In production that is not a theoretical read. The container runs `WORKDIR /app`
under `network_mode: host`, so uvicorn is on the LAN at `0.0.0.0:8003`, and
four levels up from the static directory is the deploy root — far enough for
`.env.local`, and further up, for `/proc/self/environ`, which holds every
secret handed to the container as an environment variable. `SESSION_SECRET_KEY`
is among them, and it signs the session cookie: reading it once is enough to
forge a session for any user id, which turns a file read into full admin.

Confirmed over a real socket on 2026-09-09, not inferred from the source. The
assertions below are written against the two spellings separately on purpose —
an HTTP client normalises the plain form before it leaves, so a test that only
sent `../` would have passed against the vulnerable code.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import app as app_module


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


#: Spellings of "four levels up, then a file in the deploy root".
#:
#: The encoded ones are the load-bearing cases. `httpx` collapses `../` in the
#: plain form before the request is sent, so that one exercises the client as
#: much as the server; `%2e%2e` and `..%2f` survive the client untouched and
#: are decoded by uvicorn *after* routing, which is the actual attack.
TRAVERSALS = [
    "/../../../../pyproject.toml",
    "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/pyproject.toml",
    "/%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fpyproject.toml",
    "/..%2f..%2f..%2f..%2fpyproject.toml",
    "/....//....//....//....//pyproject.toml",
    "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/.env.local",
    "/../../../../alembic.ini",
    "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_traversal_never_escapes_the_static_directory(client: TestClient, path: str) -> None:
    """Whatever the answer is, it is not the contents of another file.

    Asserted on the body rather than the status code, because the correct
    behaviour here is not a 404 — an unknown path is a client-side route and
    gets `index.html` with a 200. "Served the SPA" and "served
    `pyproject.toml`" are both 200s, so only the body can tell them apart.
    """
    response = client.get(path, follow_redirects=False)

    body = response.text
    for marker in ("[build-system]", "SESSION_SECRET", "OMNITRON_PASSWORD",
                   "sqlalchemy.url", "root:x:0:0"):
        assert marker not in body, f"{path} leaked {marker!r} from outside the static root"


def test_a_real_static_file_is_still_served(client: TestClient) -> None:
    """The confinement must not cost the route its actual job.

    A guard that also blocks the legitimate read is not a fix, it is a
    different outage — and this one would be invisible in the API tests,
    which never ask for a static file.
    """
    static_dir = app_module.STATIC_DIR
    if not static_dir.exists():
        pytest.skip("frontend is not built in this checkout")

    real = next(
        (p for p in static_dir.iterdir() if p.is_file() and p.suffix in {".html", ".ico", ".txt"}),
        None,
    )
    if real is None:
        pytest.skip("no plain static file to ask for")

    response = client.get(f"/{real.name}")
    assert response.status_code == 200
    assert response.content == real.read_bytes()
