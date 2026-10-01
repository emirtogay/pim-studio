"""Parsing CHANGELOG.md for the What's New page.

The regression that matters most is the heading format: the module was ported
from a sibling project that writes `## [0.6.0] — date`, while this repo writes
`## 0.1.0 — date`. The stricter pattern parses zero entries and renders an
empty page with no error anywhere, so the loosened match is pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pim_studio import changelog
from pim_studio.changelog import ChangelogEntry, is_unread, load_entries


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8")
    return path


# ── heading formats ────────────────────────────────────────────────────────


def test_unbracketed_heading_parses(tmp_path: Path) -> None:
    """This repo's own format. A straight port of the sibling project's regex
    required brackets and would have silently found nothing."""
    entries = load_entries(write(tmp_path, "## 0.1.0 — 2026-08-13\n\nBody.\n"))
    assert [(e.version, e.date) for e in entries] == [("0.1.0", "2026-08-13")]


def test_bracketed_heading_also_parses(tmp_path: Path) -> None:
    """The sibling project's format, kept working so the two can converge."""
    entries = load_entries(write(tmp_path, "## [0.6.0] — 2026-04-28\n\nBody.\n"))
    assert [(e.version, e.date) for e in entries] == [("0.6.0", "2026-04-28")]


@pytest.mark.parametrize("dash", ["—", "–", "-"])
def test_any_dash_separator_parses(tmp_path: Path, dash: str) -> None:
    """Em dash, en dash and hyphen are indistinguishable in most editors, and
    typing the wrong one should not empty the page."""
    entries = load_entries(write(tmp_path, f"## 0.1.0 {dash} 2026-08-13\n\nBody.\n"))
    assert len(entries) == 1


def test_a_heading_without_a_date_is_not_an_entry(tmp_path: Path) -> None:
    """`## Unreleased` and `## Known gaps` are prose headings, not releases."""
    entries = load_entries(
        write(tmp_path, "## Unreleased\n\nstuff\n\n## 0.1.0 — 2026-08-13\n\nBody.\n")
    )
    assert [e.version for e in entries] == ["0.1.0"]


# ── bodies and ordering ────────────────────────────────────────────────────


def test_body_is_captured_verbatim_between_headings(tmp_path: Path) -> None:
    entries = load_entries(
        write(
            tmp_path,
            "# Changelog\n\npreamble\n\n"
            "## 0.2.0 — 2026-08-14\n\n**Bold.** Line two.\n\n"
            "## 0.1.0 — 2026-08-13\n\nOlder.\n",
        )
    )
    assert entries[0].body == "**Bold.** Line two."
    assert entries[1].body == "Older."


def test_preamble_before_the_first_heading_is_ignored(tmp_path: Path) -> None:
    """The file opens with an explanation of the format; it is not a release."""
    entries = load_entries(write(tmp_path, "# Changelog\n\nHow this file works.\n"))
    assert entries == []


def test_entries_come_back_in_file_order_newest_first(tmp_path: Path) -> None:
    entries = load_entries(
        write(
            tmp_path,
            "## 0.3.0 — 2026-08-15\n\nc\n## 0.2.0 — 2026-08-14\n\nb\n## 0.1.0 — 2026-08-13\n\na\n",
        )
    )
    assert [e.version for e in entries] == ["0.3.0", "0.2.0", "0.1.0"]


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """A fresh checkout without a changelog must not break the panel."""
    assert load_entries(tmp_path / "nope.md") == []


# ── unread comparison ──────────────────────────────────────────────────────


def test_never_opened_counts_as_unread() -> None:
    assert is_unread(None, current="0.1.0")


def test_same_version_is_read() -> None:
    assert not is_unread("0.2.0", current="0.2.0")


def test_older_last_seen_is_unread() -> None:
    assert is_unread("0.1.0", current="0.2.0")


def test_comparison_is_numeric_not_lexicographic() -> None:
    """The case a string compare gets wrong: '0.10.0' < '0.9.0' as text."""
    assert is_unread("0.9.0", current="0.10.0")
    assert not is_unread("0.10.0", current="0.9.0")


def test_a_prerelease_suffix_is_tolerated() -> None:
    assert not is_unread("0.2.0", current="0.2.0-beta")


def test_an_unparseable_version_fails_open() -> None:
    """An unnecessary dot costs nothing; silently withholding release notes does."""
    assert is_unread("not-a-version", current="0.2.0")


# ── the real file ──────────────────────────────────────────────────────────


def test_the_repos_own_changelog_parses() -> None:
    """The check that would have caught the ported-regex bug immediately."""
    entries = load_entries()
    assert entries, "CHANGELOG.md must yield at least one entry"
    assert all(isinstance(e, ChangelogEntry) for e in entries)
    assert all(e.body for e in entries), "an entry with an empty body is a parse failure"


def test_current_version_is_not_behind_the_newest_changelog_entry() -> None:
    """The regression: an editable install writes its version metadata once and
    does not refresh it on a bump, so `importlib.metadata` reported 0.1.0 while
    pyproject and the changelog said 0.2.0.

    The direction of the failure is what makes it serious. A version that reads
    too low makes `is_unread` False for everyone who saw the previous release,
    so the bell stays dark on precisely the release it exists to announce — and
    only in development, where the feature would be tested and passed.
    """
    entries = load_entries()
    assert entries
    newest = entries[0].version
    assert changelog._parse_version(changelog.current_version()) >= changelog._parse_version(
        newest
    ), "current_version() must never report older than the newest documented release"


def test_a_user_on_the_previous_release_sees_the_new_one_as_unread() -> None:
    entries = load_entries()
    if len(entries) < 2:
        pytest.skip("needs at least two releases to compare")
    assert is_unread(entries[1].version), "the bell must pulse for the release just shipped"


def test_payload_shape_matches_what_the_frontend_expects() -> None:
    """The key is `current_version`, matching the ChangelogPayload interface in
    lib/api.ts. Calling it `version` would typecheck on both sides and render
    an empty string in the page header."""
    payload = changelog.payload()
    assert set(payload) == {"current_version", "entries"}
    entries = payload["entries"]
    assert isinstance(entries, list) and entries
    assert set(entries[0]) == {"version", "date", "body"}


def test_write_static_emits_loadable_json(tmp_path: Path) -> None:
    import json

    target = tmp_path / "demo" / "changelog.json"
    written = changelog.write_static(target)
    assert written == target

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["current_version"] == changelog.current_version()
    assert data["entries"][0]["version"] == load_entries()[0].version


# ── the artefact the What's New page actually reads ────────────────────────
#
# `changelog.py` writes `demo/changelog.json` and the page fetches it as a
# static file. Getting the *contents* right is only half of it: the file is
# regenerated on every container boot under a URL that never changes, so a
# browser that reuses a cached copy shows old release notes under a new
# version number. That is not hypothetical — the panel served `v0.24.0` in the
# sidebar and "Currently running version 0.23.0" on What's New in the same
# screenshot, because Starlette sends `etag` and `last-modified` but no
# `cache-control`, and an unbounded response may be held on heuristic
# freshness without ever being revalidated.


def test_demo_artefacts_must_be_revalidated_before_reuse(tmp_path) -> None:
    """`no-cache` on /demo, so a regenerated file is never served from a stale
    browser copy. It still stores and still 304s — it just has to ask."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from pim_studio.admin.app import RevalidatedStatic

    (tmp_path / "changelog.json").write_text('{"current_version": "9.9.9"}')

    app = Starlette()
    app.mount("/demo", RevalidatedStatic(directory=tmp_path), name="demo")
    response = TestClient(app).get("/demo/changelog.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    # The validators have to survive, or every load is a full re-download
    # rather than a 304.
    assert response.headers["etag"]
    assert response.headers["last-modified"]


def test_a_revalidated_file_still_answers_304(tmp_path) -> None:
    """The point of `no-cache` over `no-store`: revalidation stays cheap."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from pim_studio.admin.app import RevalidatedStatic

    (tmp_path / "coverage.json").write_text('{"version": 1}')
    app = Starlette()
    app.mount("/demo", RevalidatedStatic(directory=tmp_path), name="demo")
    client = TestClient(app)

    etag = client.get("/demo/coverage.json").headers["etag"]
    again = client.get("/demo/coverage.json", headers={"if-none-match": etag})
    assert again.status_code == 304


def test_hashed_assets_are_cached_forever(tmp_path) -> None:
    """The other half of the trade. Revalidating the document on every load is
    only affordable because the bundle it names never has to be re-fetched."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from pim_studio.admin.app import ImmutableStatic

    (tmp_path / "index-abc123.js").write_text("console.log(1)")
    app = Starlette()
    app.mount("/assets", ImmutableStatic(directory=tmp_path), name="assets")
    headers = TestClient(app).get("/assets/index-abc123.js").headers

    assert "immutable" in headers["cache-control"]
    assert "max-age=31536000" in headers["cache-control"]
