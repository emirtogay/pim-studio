"""Write one attribute of one product into Omnitron, and watch what else moves.

    python scripts/probe_omnitron_write.py --sku L3558.132.T4 --market PL
    python scripts/probe_omnitron_write.py --sku L3558.132.T4 --market PL --write
    python scripts/probe_omnitron_write.py --sku L3558.132.T4 --market CZ \\
        --attribute integration_kalem_icerik_cz --write

Run from the host, once, before `OMNITRON_WRITES_ENABLED` is ever set on the
server. Without `--write` it only reads and prints, so it is safe to run as
often as you like. With `--write` it needs `OMNITRON_WRITES_ENABLED=true` in
this shell — the client refuses otherwise — and it does exactly one write,
then one to put the value back.

Two questions decide whether the panel may push at all, and neither can be
answered from documentation:

**Does the patch merge or replace?** `PATCH /api/v1/products/{pk}/` takes an
`attributes` object. If it merges, a body naming one key touches one key. If
it replaces, the same body wipes every other attribute of the product — the
Russian name, the shared description, the ERP season — and returns 200 all
the same. So this reads every attribute under every locale before the write
and again after, and lists what changed besides the key it sent. Anything
at all in that list means **replace**, and the push stays off.

(The integration upsert `POST /api/i1/products/` was the original target and
is not usable: it is a *full* upsert and rejects a partial body, demanding
`name`, `base_code`, `product_type` and `attribute_set` — measured
2026-09-08. Submitting `name` under a market locale is precisely how the
Turkish master title becomes a market "translation", so the partial PATCH is
both the working door and the safer one.)

**Does a locale header confine the write?** `urun_adi_pl` is localizable:
Omnitron holds one copy per language and serves the one `Accept-Language`
names, or the tenant default — the Turkish store's — with no header. The
push writes such keys under the market's locale on the assumption that the
header confines the write to that copy. This checks: after writing under
`pl`, the default view and the `tr-tr` view must read exactly as before.

The verdict is printed at the end, with the environment line to set if both
hold. Findings belong in the vault (`Findings/`) and in `05 - Decisions Log`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pim_studio.logging import configure_logging  # noqa: E402
from pim_studio.omnitron.client import OmnitronClient, OmnitronError  # noqa: E402
from pim_studio.sync.targets import WRITE_LOCALES  # noqa: E402

#: The default (headerless) view first, then ours, then the two witnesses:
#: Turkey's explicit locale and English, which the store has been seen holding
#: Polish names under.
VIEWS: tuple[str | None, ...] = (None, "pl", "cs", "sk", "hu", "tr-tr", "en")

#: The market-named key the probe writes when none is given.
DEFAULT_KEY = {
    "PL": "urun_adi_pl",
    "CZ": "urun_adi_cz",
    "SK": "urun_adi_sk",
    "HU": "urun_adi_hu",
}

#: Keys that are one copy for the whole tenant. Writing one of these is not a
#: per-market write and the probe refuses it: it would move Turkey too.
FORBIDDEN = {
    "name", "integration_kalem_icerik", "integration_kol_boyu", "integration_yaka",
    "integration_kalip", "integration_desen", "is_active",
}

TRUNCATE = 60


def _short(value: object) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= TRUNCATE else text[: TRUNCATE - 1] + "…"


def _record(api: OmnitronClient, sku: str, locale: str | None) -> dict | None:
    payload = api.get(
        "/api/v1/products/", params={"sku__in": sku, "limit": 5}, locale=locale
    )
    for record in payload.get("results", ()):
        if str(record.get("sku", "")) == sku:
            return record
    return None


def _read(api: OmnitronClient, sku: str, locale: str | None) -> dict[str, str] | None:
    record = _record(api, sku, locale)
    if record is None:
        return None
    return {
        k: ("" if v is None else str(v))
        for k, v in (record.get("attributes") or {}).items()
    }


def snapshot(api: OmnitronClient, sku: str) -> dict[str | None, dict[str, str]]:
    views: dict[str | None, dict[str, str]] = {}
    for locale in VIEWS:
        attributes = _read(api, sku, locale)
        if attributes is None:
            raise SystemExit(f"{sku} not found under {locale or 'the default view'}")
        views[locale] = attributes
    return views


def diff(
    before: dict[str | None, dict[str, str]], after: dict[str | None, dict[str, str]]
) -> list[tuple[str | None, str, str, str]]:
    """(view, key, was, now) for every attribute that reads differently."""
    out = []
    for view in before:
        pre, post = before[view], after.get(view, {})
        for key in sorted(set(pre) | set(post)):
            if pre.get(key, "") != post.get(key, ""):
                out.append((view, key, pre.get(key, ""), post.get(key, "")))
    return out


def print_key(views: dict[str | None, dict[str, str]], key: str) -> None:
    for locale, attributes in views.items():
        print(f"  {locale or 'default':8} {key} = {_short(attributes.get(key, ''))!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sku", required=True)
    parser.add_argument("--market", choices=sorted(WRITE_LOCALES), default="PL")
    parser.add_argument(
        "--attribute", help="Omnitron key to write; defaults to the market's name key"
    )
    parser.add_argument(
        "--localizable", choices=("yes", "no"), default="yes",
        help="whether Omnitron holds this key per language (omnitron_attributes.is_localizable)",
    )
    parser.add_argument("--write", action="store_true", help="actually write, then restore")
    parser.add_argument("--keep", action="store_true", help="do not restore the original value")
    args = parser.parse_args(argv)
    configure_logging()

    key = args.attribute or DEFAULT_KEY[args.market]
    if key in FORBIDDEN or not key.endswith(("_pl", "_cz", "_sk", "_hu", "_pol", "_cze")):
        print(f"refusing to write {key}: not a market-named key", file=sys.stderr)
        return 2
    locale = WRITE_LOCALES[args.market] if args.localizable == "yes" else None

    with OmnitronClient() as api:
        identity = _record(api, args.sku, None)
        if identity is None:
            raise SystemExit(f"{args.sku} not found")
        pk = identity.get("pk")
        if not isinstance(pk, int):
            raise SystemExit(f"{args.sku} has no usable Omnitron pk: {pk!r}")
        before = snapshot(api, args.sku)
        print(f"SKU {args.sku} · pk {pk} · market {args.market} · key {key} · locale {locale}")
        print("before:")
        print_key(before, key)
        if not args.write:
            print("\nread-only run; add --write to test the upsert.")
            return 0

        original = before[locale].get(key, "")
        marker = f" [probe {datetime.now(UTC):%H%M}]"
        candidate = (original or "PROBE") + marker
        print(f"\nwriting {candidate!r} under {locale or 'no header'} …")
        try:
            api.patch_product(
                pk,
                {key: candidate},
                allowed_keys={key},
                locale=locale,
                localizable_keys={key} if locale else (),
            )
        except OmnitronError as exc:
            print(f"write failed: {exc}", file=sys.stderr)
            return 1
        after = snapshot(api, args.sku)
        print("after:")
        print_key(after, key)

        changes = diff(before, after)
        others = [c for c in changes if c[1] != key]
        ours = [c for c in changes if c[1] == key]
        landed = after[locale].get(key, "") == candidate
        default_moved = any(v in (None, "tr-tr") for v, _k, _w, _n in ours) if locale else False

        print("\nverdict:")
        print(f"  landed under {locale or 'default'}: {'yes' if landed else 'NO'}")
        verdict = " — REPLACE semantics or a side effect; keep writes OFF" if others else " (merge)"
        print(f"  other attributes changed: {len(others)}{verdict}")
        for view, k, was, now in others[:20]:
            print(f"    {view or 'default':8} {k}: {_short(was)!r} -> {_short(now)!r}")
        if locale:
            print(f"  views where {key} moved: {[v or 'default' for v, *_ in ours]}")
            print(
                "  default / tr-tr copies untouched: "
                + ("yes" if not default_moved else "NO — the header did not confine the write")
            )

        restored = True
        if not args.keep:
            print(f"\nrestoring {original!r} …")
            try:
                api.patch_product(
                    pk,
                    {key: original},
                    allowed_keys={key},
                    locale=locale,
                    localizable_keys={key} if locale else (),
                )
            except OmnitronError as exc:
                print(f"RESTORE FAILED: {exc} — put {original!r} back by hand", file=sys.stderr)
                restored = False
            else:
                final = snapshot(api, args.sku)
                leftover = diff(before, final)
                restored = not leftover
                print("  back to the starting state: " + ("yes" if restored else "NO"))
                for view, k, was, now in leftover[:20]:
                    print(f"    {view or 'default':8} {k}: {_short(was)!r} -> {_short(now)!r}")

    ok = landed and not others and not default_moved and restored
    print()
    if ok:
        print("Both hold. Set on the server, then restart the panel:")
        print("  OMNITRON_WRITES_ENABLED=true")
        print("  OMNITRON_WRITE_MODE=merge")
    else:
        print("Do not enable writes. Record the output above in the vault and decide.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
