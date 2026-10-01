"""Ask Omnitron the same product under every locale, and see what changes.

    python scripts/probe_locale.py --sku L3558.132.T4
    python scripts/probe_locale.py --sku L3558.132.T4,PH9619.166.T3
    python scripts/probe_locale.py --sku L3558.132.T4 --locales tr,pl,cs,sk,hu,en,ru,uk

Two questions this exists to settle, neither of which any export we hold can
answer.

**Which content columns are actually localisable.** Omnitron stores per-market
content twice: as flat suffixed columns (`urun_detay_pol`, `urun_detay_hu`)
and inside a per-locale translation store. Whether a flat column is itself
localisable is a boolean in the attribute registry, and it decides whether one
column holds four different texts or one text four times. An export cannot
distinguish them: `urun_adi_cz/sk/hu` look identical across locales here only
because barely fifty options hold anything at all, and identical
`urun_detay_*` values might mean "not localisable" or might mean "localisable
and equal". Reading the same product under `tr`, `pl`, `cs`, `sk` and `hu` and
comparing shows which columns actually move.

**Whether the Hungarian text is in the Hungarian place.** The translation store
on this tenant is known to be structurally corrupt — Polish names filed under
`en`, Czech under `hu` — which is why `sync/values.py` reads the flat fields
and ignores the store entirely (CHANGELOG 0.5.0). That fix does not settle the
remaining worry: if the Hungarian content was uploaded while the session locale
was Polish, the Hungarian text is sitting in the Polish locale and every
Hungarian figure this panel prints is wrong in a way no not-null check can see.
So this also runs the language detector over each stored value and says what
language it actually reads as. Hungarian text under a Polish key is invisible
to `rules/language`, which only ever acts on an English verdict.

Locale is an `Accept-Language` header and only a header. `?locale=`,
`?language=` and `?lang=` are all accepted and silently ignored, which is worse
than an error because the response looks right — `OmnitronClient.get` says the
same thing, and this script goes through it rather than around it.

Read-only: `OmnitronClient` cannot write. Needs live credentials, so it cannot
run in CI, which is exactly why it prints its evidence rather than asserting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pim_studio.logging import configure_logging  # noqa: E402
from pim_studio.omnitron.client import OmnitronClient  # noqa: E402
from pim_studio.rules.language import detect_language  # noqa: E402

#: Turkish first: it is the tenant's own default, so it is the baseline every
#: other locale is compared against. An untranslated value silently returns it.
#: `en` is included by default because the backoffice URL puts the interface
#: language at `en` and the translation store has been seen holding Polish
#: names under the `en` key — content entered while the UI sat in English is a
#: live suspicion, not a hypothetical. `ru`/`uk` stay opt-in via `--locales`.
DEFAULT_LOCALES = ("tr", "pl", "cs", "sk", "hu", "en")

#: The content keys worth dumping out of the translation store, per store
#: locale. Same non-derivable spellings as `CONTENT_FIELDS`; listed, not
#: generated.
STORE_KEYS = (
    "urun_adi_pl", "urun_adi_cz", "urun_adi_sk", "urun_adi_hu",
    "urun_detay_pol", "urun_detay_cze", "urun_detay_sk", "urun_detay_hu",
    "integration_kalem_icerik_pol", "integration_kalem_icerik_cz",
    "integration_kalem_icerik_sk", "integration_kalem_icerik_hu",
)

#: The flat suffixed columns, without the `attributes__` prefix the exports
#: carry. Spellings are inconsistent on purpose — `_pol` for the Polish detail
#: and `_pl` for the Polish name, three different spellings of Czech — and
#: nothing about them is derivable, so they are listed rather than generated.
#: See `seed.py:162-189`.
CONTENT_FIELDS = (
    ("product name", ("urun_adi_pl", "urun_adi_cz", "urun_adi_sk", "urun_adi_hu")),
    (
        "product detail",
        ("urun_detay_pol", "urun_detay_cze", "urun_detay_sk", "urun_detay_hu"),
    ),
    (
        "composition",
        (
            "integration_kalem_icerik",
            "integration_kalem_icerik_pol",
            "integration_kalem_icerik_cz",
            "integration_kalem_icerik_sk",
            "integration_kalem_icerik_hu",
        ),
    ),
)

TRUNCATE = 72


def _shorten(value: str) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= TRUNCATE else text[: TRUNCATE - 1] + "…"


def _fetch(api: OmnitronClient, sku: str, locale: str) -> dict | None:
    """The product record as this locale sees it."""
    payload = api.get("/api/v1/products/", params={"sku": sku, "limit": 5}, locale=locale)
    for record in payload.get("results", ()):
        if str(record.get("sku", "")) == sku:
            return record
    return None


def _values(record: dict, column: str) -> str:
    attributes = record.get("attributes") or {}
    value = attributes.get(column)
    return "" if value is None else str(value)


def probe(sku: str, locales: tuple[str, ...]) -> int:
    with OmnitronClient() as api:
        records: dict[str, dict] = {}
        for locale in locales:
            record = _fetch(api, sku, locale)
            if record is None:
                print(f"not found under {locale}: {sku}", file=sys.stderr)
                return 1
            records[locale] = record

    baseline = locales[0]
    print(f"SKU {sku}")
    print(f"baseline locale: {baseline}")
    # The detector is constrained to the five languages that can legitimately
    # appear in our markets, so it has no Turkish. Turkish text comes back as
    # whichever of the five it resembles, confidently and wrongly. Said here
    # rather than suppressed: the Turkish baseline is what proves a column
    # untranslated, so it has to be shown, and a reader who is not told will
    # believe the label.
    print(
        "note: the detector knows only PL/CZ/SK/HU/EN — any Turkish value below\n"
        "      will be mislabelled as one of them. Read those rows for whether\n"
        "      the text moved, not for what language it claims to be.\n"
    )

    for label, columns in CONTENT_FIELDS:
        print(f"── {label} " + "─" * max(0, 62 - len(label)))
        for column in columns:
            by_locale = {loc: _values(records[loc], column) for loc in locales}
            stored = by_locale[baseline]

            if not any(by_locale.values()):
                print(f"{column:34} empty in every locale")
                continue

            # Does the flat column move under Accept-Language at all? This is
            # the localisability question, answered by observation rather than
            # by trusting the registry's boolean.
            moved = sorted(
                loc for loc, text in by_locale.items() if text != stored and loc != baseline
            )
            verdict = (
                f"varies under: {', '.join(moved)}"
                if moved
                else "identical in every locale"
            )
            print(f"{column:34} {verdict}")

            # And what language is actually sitting there? This is the
            # HU-under-PL question, and no single-value check in the app can
            # see it: `rules/language` acts only on an English verdict.
            for locale in locales:
                text = by_locale[locale]
                if not text:
                    # Shown, not skipped: where a value is *absent* is half the
                    # locale×column matrix this probe exists to draw.
                    print(f"{'':34}   [{locale}] (empty)")
                    continue
                language, confidence = detect_language(text)
                reads = f"{language.lower()} {confidence:.2f}" if language else "unknown"
                print(f"{'':34}   [{locale}] reads as {reads:16} {_shorten(text)}")
        print()

    # The translation store, for contrast. We deliberately do not read it — it
    # is corrupt on this tenant — but seeing it beside the flat fields is what
    # makes the difference between the two legible.
    print("── localised_attributes (the store we ignore) " + "─" * 22)
    for locale in locales:
        store = records[locale].get("localized_attributes") or {}
        if not store:
            print(f"[{locale}] empty")
            continue
        for key in sorted(store):
            name = (store[key] or {}).get("l_name") or ""
            if name:
                print(f"[{locale}] {key:6} l_name  {_shorten(name)}")

    # The store's copies of the content columns, read from the baseline record
    # (the store arrives whole regardless of the request's locale, so one view
    # of it is every view). This is where "Polish name filed under en" is
    # visible directly: the store locale is the row label, the language the
    # value actually reads as sits beside it.
    print()
    print("── store content keys, by store locale (baseline record) " + "─" * 10)
    store = records[baseline].get("localized_attributes") or {}
    printed = False
    for store_locale in sorted(store):
        bucket = store[store_locale] or {}
        for key in STORE_KEYS:
            value = bucket.get(key)
            if value in (None, ""):
                continue
            printed = True
            language, confidence = detect_language(str(value))
            reads = f"{language.lower()} {confidence:.2f}" if language else "unknown"
            print(f"[{store_locale}] {key:30} reads as {reads:16} {_shorten(str(value))}")
    if not printed:
        print("no content keys held in the store for this product")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sku", required=True,
        help="a full SKU, e.g. L3558.132.T4 — or several, comma-separated",
    )
    ap.add_argument(
        "--locales",
        default=",".join(DEFAULT_LOCALES),
        help=f"comma list, baseline first (default {','.join(DEFAULT_LOCALES)})",
    )
    args = ap.parse_args(argv)
    locales = tuple(loc.strip() for loc in args.locales.split(",") if loc.strip())
    if not locales:
        print("no locales given", file=sys.stderr)
        return 2
    skus = [s.strip() for s in args.sku.split(",") if s.strip()]
    if not skus:
        print("no SKUs given", file=sys.stderr)
        return 2
    configure_logging()
    worst = 0
    for i, sku in enumerate(skus):
        if i:
            print("\n" + "═" * 68 + "\n")
        worst = max(worst, probe(sku, locales))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
