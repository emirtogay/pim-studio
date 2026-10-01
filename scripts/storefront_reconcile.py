"""Reconcile the panel against the storefronts — the only oracle that is not
itself a report.

    python scripts/storefront_reconcile.py                 # fetch and reconcile
    python scripts/storefront_reconcile.py --cached        # reuse the last pull
    python scripts/storefront_reconcile.py --market PL --list "No price"

Two earlier reconciliations measured against
`data/uploads/catalogue-control-table.csv` and both were measuring the wrong
thing: the control table is a Looker export, derived like our panel is, and on
2026-08-20 it sat 246 below the site it claims to describe. The site is the
definition of live. It is also enumerable, which nobody had checked —
`/list?format=json&page_size=100` returns the whole population with attributes,
46 requests a market.

The one trap: the market is selected by the `pz-locale` / `pz-currency` cookie
pair and by nothing else. Without them the same URL answers with some other
channel's 1,433 products, cheerfully and with a 200.

What comes out is not a pair of totals. Totals tell you two numbers disagree;
this joins on ModelColor and buckets every option the storefront is selling by
what our panel says about it, which is a different defect and a different owner
per bucket.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from pim_studio.rules.ban import BanVerdict, classify_comment  # noqa: E402
from pim_studio.sync.storefront import STOREFRONTS  # noqa: E402

#: Read from the sync stage rather than restated. The cookie pair is the entire
#: market selector on these sites — a copy of it here that drifted would not
#: fail, it would quietly reconcile Poland against some other channel's range.
#: The *listing* read is not shared: the stage wants option keys and this wants
#: whole rows, so they are different reads of the same URL.
MARKETS = tuple(STOREFRONTS)

#: The storefront's own ModelColor. Present on 4,577 of PL's 4,592 rows; the
#: `base_code`/`listing_code` pair is the fallback for the rest and agrees with
#: it everywhere both exist.
_MC_ATTR = "integration_model_renk"

#: Kept from each listing row. The listing carries the full attribute set, and
#: pulling it all through would make the cache 40x larger for no question we
#: ask of it.
_KEEP = ("pk", "sku", "base_code", "listing_code", "in_stock", "stock",
         "price", "is_listable", "product_type", "absolute_url")
_KEEP_ATTR = (_MC_ATTR, "PL_comment", "PL_sezon", "integration_cinsiyet")

_CACHE = pathlib.Path(__file__).resolve().parent / ".storefront_cache"
_EXTRACT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/pim_studio/admin/frontend/public/demo/products.json"
)


def _fetch(base: str, cookie: str, params: str, tries: int = 4) -> dict:
    url = f"{base}/list?format=json&{params}"
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "pim-studio/storefront-reconcile",
                "Accept": "application/json",
                "Cookie": cookie,
            })
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def pull(market: str) -> list[dict]:
    """Every option the storefront is currently selling in one market."""
    shop = STOREFRONTS[market]
    base, cookie = shop.origin, shop.cookie
    payload = _fetch(base, cookie, "page_size=100&page=1")
    pages = payload["pagination"]["num_pages"]
    print(f"{market}: {payload['pagination']['total_count']:,} options, "
          f"{pages} pages", file=sys.stderr, flush=True)

    rows: list[dict] = []
    page = 1
    while True:
        for product in payload.get("products", []):
            row = {k: product.get(k) for k in _KEEP}
            attributes = product.get("attributes") or {}
            row.update({k: attributes.get(k) for k in _KEEP_ATTR})
            rows.append(row)
        if page >= pages:
            return rows
        page += 1
        payload = _fetch(base, cookie, f"page_size=100&page={page}")


def load_storefront(markets: tuple[str, ...], cached: bool) -> dict[str, dict[str, dict]]:
    _CACHE.mkdir(exist_ok=True)
    out: dict[str, dict[str, dict]] = {}
    for market in markets:
        path = _CACHE / f"{market}.json"
        if cached and path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
        else:
            rows = pull(market)
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        out[market] = {
            (r.get(_MC_ATTR) or f"{r['base_code']}.{r['listing_code']}"): r
            for r in rows
        }
    return out


def load_panel(path: pathlib.Path) -> dict[str, dict]:
    """What the Products page shows, read from the extract that feeds it."""
    return {p["modelColor"]: p for p in json.loads(path.read_text(encoding="utf-8"))}


def verdict(panel: dict[str, dict], mc: str, market: str) -> str:
    """Our answer for one option in one market, in the storefront's terms."""
    row = panel.get(mc)
    if row is None:
        return "not held at all"
    if row["live"].get(market):
        return "live (we agree)"
    return row["blockedBy"].get(market) or "blocked, no reason given"


#: Display order. `live (we agree)` first because everything under it is a
#: disagreement, and the tail is the residual nobody has explained.
_ORDER = ("live (we agree)", "Banned", "No price", "Not in catalogue",
          "Out of stock", "No image", "not held at all",
          "blocked, no reason given")


def report(panel: dict[str, dict], sites: dict[str, dict[str, dict]]) -> None:
    markets = tuple(sites)
    width = 26

    def line(label: str, values: list) -> None:
        print(f"{label:{width}}" + "".join(f"{v:>10,}" if isinstance(v, int)
                                           else f"{v:>10}" for v in values))

    print("\nHow many options each storefront is selling, against how many the")
    print("panel calls live there.\n")
    line("", list(markets))
    line("storefront", [len(sites[m]) for m in markets])
    line("panel says live", [sum(1 for p in panel.values() if p["live"].get(m))
                             for m in markets])
    line("gap", [len(sites[m]) - sum(1 for p in panel.values() if p["live"].get(m))
                 for m in markets])

    tally = {m: collections.Counter(verdict(panel, mc, m) for mc in sites[m])
             for m in markets}
    print("\nEvery option a storefront is selling, by what the panel says about it:\n")
    line("", list(markets))
    for bucket in _ORDER:
        if any(tally[m][bucket] for m in markets):
            line("  " + bucket, [tally[m][bucket] for m in markets])

    print("\nThe other direction — panel says live, storefront does not list it.")
    print("Rule 4 (category assigned) is not synced, so this is where it would show:\n")
    line("  unexplained", [
        sum(1 for mc, p in panel.items() if p["live"].get(m) and mc not in sites[m])
        for m in markets
    ])

    print("\nThe ban list against what is actually on sale. A confirmed ban still")
    print("selling is a merchandising gap, not a measurement one:\n")
    line("", list(markets))
    for label, want in (("  banned and selling", BanVerdict.BANNED),
                        ("  unclassified comment", BanVerdict.UNKNOWN)):
        line(label, [sum(1 for r in sites[m].values()
                         if classify_comment(r["PL_comment"]) is want)
                     for m in markets])
    unclassified: collections.Counter[str] = collections.Counter()
    for m in markets:
        for r in sites[m].values():
            if classify_comment(r["PL_comment"]) is BanVerdict.UNKNOWN:
                unclassified[r["PL_comment"]] += 1
    if unclassified:
        print("\n  awaiting classification in `ban_comments`:")
        for comment, n in unclassified.most_common():
            print(f"    {n:5,}  {comment}")


def listing(panel: dict[str, dict], site: dict[str, dict], market: str,
            bucket: str, limit: int) -> None:
    hits = [mc for mc in site if verdict(panel, mc, market) == bucket]
    print(f"\n{len(hits):,} options the {market} storefront sells and the panel "
          f"calls {bucket!r}:\n")
    for mc in hits[:limit]:
        row = site[mc]
        print(f"  {mc:20} {(row['PL_sezon'] or '-'):10} "
              f"{(row['PL_comment'] or ''):32} {row['absolute_url']}")
    if len(hits) > limit:
        print(f"  ... {len(hits) - limit:,} more (raise --limit)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cached", action="store_true",
                        help="reuse the last pull instead of hitting the sites")
    parser.add_argument("--extract", type=pathlib.Path, default=_EXTRACT,
                        help="the products.json the panel renders")
    parser.add_argument("--market", choices=MARKETS, action="append",
                        help="restrict to one market; repeatable")
    parser.add_argument("--list", dest="bucket",
                        help="print the options in one bucket instead of the table")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    markets = tuple(args.market) if args.market else MARKETS
    if not args.extract.exists():
        print(f"no extract at {args.extract} — run "
              f"`python scripts/build_demo_data_from_db.py` first", file=sys.stderr)
        return 1

    panel = load_panel(args.extract)
    sites = load_storefront(markets, args.cached)

    if args.bucket:
        for market in markets:
            listing(panel, sites[market], market, args.bucket, args.limit)
    else:
        report(panel, sites)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
