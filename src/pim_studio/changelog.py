"""Reads CHANGELOG.md and exposes structured entries for the admin panel.

Ported from the price-comparison app, which has run this in production since
April. Two things are deliberately different here.

**The heading pattern accepts brackets or no brackets.** The price app writes
`## [0.6.0] — 2026-04-28`; this repo's CHANGELOG.md writes
`## 0.1.0 — 2026-08-13`. Porting the stricter regex unchanged would have parsed
zero entries and shown an empty page with no error — the file is there, the
code runs, and nothing is displayed. Accepting both is one optional group and
removes a class of silent failure that a shared format convention would only
have prevented by everyone remembering it.

**Both em-dash and hyphen separators are accepted**, for the same reason: the
character between version and date is invisible in most editors and typing the
wrong one should not empty the page.

Format expected:

    ## 0.2.0 — 2026-08-14

    Body paragraph one.

    Body paragraph two.

    ## 0.1.0 — 2026-08-13
    ...

The body between two version headers is preserved verbatim as raw markdown.
The frontend renders it with a small helper rather than a full markdown
library, so keep the format restricted — paragraphs, **bold**, and lists.

Version comparison tokenises the dotted parts as integers, which is enough for
`MAJOR.MINOR.PATCH` and gets `0.10.0 > 0.9.0` right where a string compare
would not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

#: Resolves to the repo root in development and to `/app/` in the container,
#: where the Dockerfile copies CHANGELOG.md next to pyproject.toml.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CHANGELOG_PATH = _PROJECT_ROOT / "CHANGELOG.md"

#: `## 0.2.0 — 2026-08-14` or `## [0.2.0] - 2026-08-14`. The brackets are
#: optional and the separator may be an em dash, en dash or hyphen — see the
#: module docstring for why this is looser than the source it was ported from.
_HEADING_RE = re.compile(
    r"^##\s*\[?(?P<version>\d[^\]\s]*)\]?\s*[—–-]\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$"
)

#: The distribution name in pyproject.toml. Wrong here means `current_version`
#: silently falls back to the changelog's top entry, which is usually right and
#: therefore hides the mistake.
_DIST_NAME = "pim-studio"


@dataclass(frozen=True)
class ChangelogEntry:
    version: str
    date: str  # ISO yyyy-mm-dd
    body: str  # Raw markdown between this entry's header and the next.

    def as_dict(self) -> dict[str, str]:
        return {"version": self.version, "date": self.date, "body": self.body}


def current_version() -> str:
    """The running version — the newer of the installed package and the changelog.

    `pyproject.toml` is the source of truth, but `importlib.metadata` does not
    read it: it reads the *installed* distribution metadata, which an editable
    install writes once and does not refresh when the version is bumped. So a
    developer who bumps to 0.2.0 and writes the release notes still gets 0.1.0
    from the metadata until they reinstall.

    That matters because of which way it fails. Under-reporting the version
    makes `is_unread` return False for everyone who had already seen the
    previous release — the bell stays dark on exactly the release it exists to
    announce, and it does so only in development, which is where the feature
    would be tested and wrongly judged to work.

    Taking the newer of the two is correct in both environments: in a container
    the package is installed properly and the metadata wins ties, while in a
    working tree a changelog entry that exists is proof that version is real.
    """
    installed: str | None
    try:
        installed = _pkg_version(_DIST_NAME)
    except PackageNotFoundError:
        installed = None

    entries = load_entries()
    documented = entries[0].version if entries else None

    candidates = [v for v in (installed, documented) if v]
    if not candidates:
        return "0.0.0"
    return max(candidates, key=_parse_version)


def load_entries(path: Path | None = None) -> list[ChangelogEntry]:
    """Parse CHANGELOG.md into entries, newest first.

    A missing file returns an empty list rather than raising: "no changelog
    yet" is a legitimate state for a fresh checkout, and an admin panel should
    not fail to load over it.
    """
    source = path or _CHANGELOG_PATH
    if not source.exists():
        return []

    entries: list[ChangelogEntry] = []
    version: str | None = None
    date: str | None = None
    body: list[str] = []

    def flush() -> None:
        if version is not None and date is not None:
            entries.append(
                ChangelogEntry(version=version, date=date, body="\n".join(body).strip())
            )

    for line in source.read_text(encoding="utf-8").splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            version = match.group("version")
            date = match.group("date")
            body = []
        elif version is not None:
            body.append(line)

    flush()
    return entries


def _parse_version(value: str) -> tuple[int, ...]:
    """Tokenise `MAJOR.MINOR.PATCH` into ints for comparison.

    A trailing pre-release tag (`0.4.0-beta`) drops its suffix. Only released
    versions reach stakeholders, so this avoids taking a dependency on
    `packaging` for a single comparison.
    """
    parts: list[int] = []
    for token in value.split("."):
        head = re.match(r"^\d+", token)
        parts.append(int(head.group(0)) if head else 0)
    return tuple(parts)


def is_unread(last_seen: str | None, current: str | None = None) -> bool:
    """Whether this user should see the unread dot on the bell.

    Never having opened the page counts as unread. Otherwise the comparison is
    numeric, so `0.10.0` correctly beats `0.9.0`.

    Fails open. A version string we cannot parse shows the indicator rather
    than hiding it — the cost of an unnecessary dot is trivial next to silently
    withholding release notes.
    """
    cur = current or current_version()
    if last_seen is None:
        return True
    try:
        return _parse_version(cur) > _parse_version(last_seen)
    except ValueError:
        return True


def payload() -> dict[str, object]:
    """The shape the admin panel consumes, from `/api/changelog` or a static file.

    The key is `current_version`, matching the `ChangelogPayload` interface the
    frontend already declares in `lib/api.ts`. Naming it `version` here would
    typecheck on both sides and render an empty string.
    """
    return {
        "current_version": current_version(),
        "entries": [e.as_dict() for e in load_entries()],
    }


def write_static(target: Path | None = None) -> Path:
    """Write `payload()` to the frontend's demo folder.

    The panel has no API layer yet, so it reads its data from static JSON in
    `public/demo/` — the same route `products.json` and `details.json` take.
    Generating it here rather than hand-writing a JSON file keeps one parser:
    when `/api/changelog` lands it calls `payload()` too, and the page's data
    does not change shape.
    """
    import json

    # `DEMO_OUT_DIR` for the same reason `build_demo_data_from_db.py` has it:
    # in the container the panel is served from `admin/static/demo/`, which is
    # a bind mount, and there is no frontend source tree to write into.
    import os

    if target is not None:
        destination = target
    elif os.environ.get("DEMO_OUT_DIR"):
        destination = Path(os.environ["DEMO_OUT_DIR"]) / "changelog.json"
    else:
        destination = (
            _PROJECT_ROOT
            / "src"
            / "pim_studio"
            / "admin"
            / "frontend"
            / "public"
            / "demo"
            / "changelog.json"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return destination


if __name__ == "__main__":
    written = write_static()
    data = payload()
    entries = data["entries"]
    assert isinstance(entries, list)
    print(f"version   {data['current_version']}")
    print(f"entries   {len(entries)}")
    print(f"written   {written}")
