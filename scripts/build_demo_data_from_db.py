"""Build the frontend extract from the database instead of the export files.

    python scripts/build_demo_data_from_db.py

`build_demo_data.py` joins five hand-placed export snapshots. This reads the
same shapes out of `attribute_values`, `products` and `catalogue_memberships`,
so what the panel renders is what the sync actually pulled from Omnitron rather
than what was true in April.

Output is byte-compatible with the existing generator — same two files, same
keys — so no frontend change is needed. That is the point: the bridge is a
generator swap, not a rewrite, and the real fix (a FastAPI layer the panel
queries live) can land later without the UI moving twice.

Liveness comes from `pim_studio.rules.liveness`, not from a copy of it. Until
2026-08-20 this module reimplemented three of the six rules inline and skipped
ban and image entirely, so the panel called products live that had no photo and
products live that merch had explicitly removed. Five of the six rules are
evaluated here now; only rule 4 (category assignment) is not, because it is not
synced. See `Findings/Audit 2026-08-20`, F5.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlalchemy import select  # noqa: E402

from pim_studio.db import session_scope  # noqa: E402
from pim_studio.models import (  # noqa: E402
    Attribute,
    AttributeValue,
    BanComment,
    CatalogueMembership,
    Product,
    VocabularyLabel,
)
from pim_studio.reports.coverage import (  # noqa: E402
    load_catalogue,
    to_extract,
)
from pim_studio.reports.overview import (  # noqa: E402
    build_overview,
    compute_deltas,
    load_funnel,
    record_snapshot,
)
from pim_studio.rules import (  # noqa: E402
    BanVerdict,
    classify_comment,
)
from pim_studio.rules.funnel import (  # noqa: E402
    defect_english,
    defect_name_detail,
    fold_seasons,
    market_liveness,
    missing_label,
)
from pim_studio.rules.language import english_confidence  # noqa: E402
from pim_studio.rules.liveness import STOCK_THRESHOLD  # noqa: E402

# Where the panel reads its extract from.
#
# In dev that is the frontend's `public/demo/`, which Vite copies into the
# build. In the container there is no frontend source to write into — the
# built SPA is served from `admin/static/`, and `static/demo/` is a bind mount
# from the host so the extract survives the `--rm` container that regenerates
# it. Hence `DEMO_OUT_DIR`, which `pim-studio-sync.sh` sets.
DEFAULT_OUT_DIR = PROJECT_ROOT / "src/pim_studio/admin/frontend/public/demo"
OUT_DIR = Path(os.environ["DEMO_OUT_DIR"]) if os.environ.get("DEMO_OUT_DIR") else DEFAULT_OUT_DIR
MARKETS = ("PL", "CZ", "SK", "HU")


def _size_order(label: str) -> tuple[int, str]:
    """Sort sizes the way a person reads them: T1, T2 … T10, T11.

    Plain string sort gives T1, T10, T11, T2, which makes an eleven-size table
    unreadable and looks like a data fault. Anything without a trailing number
    keeps its alphabetical place after the numbered ones.
    """
    digits = "".join(c for c in label if c.isdigit())
    return (int(digits), label) if digits else (10**6, label)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--min-stock", type=int, default=0,
        help="drop a ModelColor unless one of its SKUs has at least this much "
             "stock in some market. Default 0 — nothing is dropped. Exclusion "
             "is the wrong default here: a product with no stock is a product "
             "with a problem, and hiding it is how the problem stops being "
             "counted. Prefer the facets in the panel.",
    )
    ap.add_argument(
        "--require-price", action="store_true",
        help="drop ModelColors with no price in any market",
    )
    ap.add_argument(
        "--require-catalogue", action="store_true",
        help="drop ModelColors in none of the four catalogues",
    )
    ap.add_argument(
        "--live-only", action="store_true",
        help="keep only ModelColors live in at least one market",
    )
    ap.add_argument(
        "--all", action="store_true",
        help="no filtering — emit every ModelColor",
    )
    args = ap.parse_args()
    if args.all:
        args.min_stock = 0
        args.require_price = args.require_catalogue = args.live_only = False

    with session_scope() as db:
        attr_key = {
            row[0]: row[1] for row in db.execute(select(Attribute.id, Attribute.key))
        }

        # The whole registry, so the panel's attribute matrix is whatever the
        # registry says rather than a list hardcoded in the frontend. The old
        # hardcoded list showed 15 rows, three of them pinned to "missing"
        # regardless of the database - `fit` and `collar_type` among them,
        # which between them have 13,339 real values.
        registry = [
            {
                "key": a.key,
                "label": a.label,
                "dataType": a.data_type,
                "group": a.display_group,
                "order": a.display_order,
                # write_channel is about who fills a gap, which is exactly what
                # the matrix's owner column means.
                "owner": {
                    "omnitron_import": "us",
                    "product_card": "partner",
                    "read_only": "system",
                }.get(a.write_channel, "system"),
                "perMarket": bool(a.per_market),
                "longText": bool(a.long_text),
                "gatesGoLive": bool(a.gates_go_live),
                "appliesWhenSubcategory": a.applies_when_subcategory,
            }
            for a in db.scalars(
                select(Attribute).order_by(Attribute.display_group, Attribute.display_order)
            )
        ]

        # Localised dropdown labels, keyed attribute -> baseline -> market.
        # `translated` travels with each one: Omnitron returns the Turkish
        # label when a translation is absent, so without the flag the panel
        # would present a Turkish word as Polish — the defect this module
        # exists to find, manufactured by the tool itself.
        loc_market = {"pl": "PL", "cs": "CZ", "sk": "SK", "hu": "HU"}
        labels: dict[str, dict[str, dict[str, dict]]] = {}
        for key, baseline, locale, label, translated, ambiguous in db.execute(
            select(
                Attribute.key, VocabularyLabel.baseline, VocabularyLabel.locale,
                VocabularyLabel.label, VocabularyLabel.translated,
                VocabularyLabel.ambiguous,
            ).join(Attribute, Attribute.id == VocabularyLabel.attribute_id)
        ):
            market = loc_market.get(locale)
            if market is None:
                continue
            labels.setdefault(key, {}).setdefault(baseline, {})[market] = {
                "label": label,
                "translated": bool(translated),
                **({"ambiguous": True} if ambiguous else {}),
            }

        # The ban list as the panel holds it, not the seeded default: the
        # table is editable precisely so a new merch convention does not need
        # a deploy, and reading the seed instead would silently ignore edits.
        ban_table = {
            comment.strip().lower(): bans
            for comment, bans in db.execute(
                select(BanComment.comment, BanComment.bans)
            )
        }

        # Whether the image sync has ever produced anything. If it has not,
        # every product would look image-less and the rule would block the
        # entire catalogue — so "we never looked" must not read as "there is
        # no image". This is the same unknown-is-not-zero guard the rule
        # applies to price and stock, at the only grain we can detect it.
        images_synced = bool(
            db.scalar(
                select(AttributeValue.id)
                .join(Attribute, Attribute.id == AttributeValue.attribute_id)
                .where(Attribute.key == "image_0", AttributeValue.value.isnot(None))
                .limit(1)
            )
        )

        # Which markets the price and stock syncs have actually read. Absence
        # of a row only means "none" for a market we looked at; for one we
        # never synced it means nothing at all, and the rules must abstain.
        markets_priced = {
            r[0] for r in db.execute(
                select(AttributeValue.market)
                .join(Attribute, Attribute.id == AttributeValue.attribute_id)
                .where(Attribute.key == "price").distinct()
            )
        }
        markets_stocked = {
            r[0] for r in db.execute(
                select(AttributeValue.market)
                .join(Attribute, Attribute.id == AttributeValue.attribute_id)
                .where(Attribute.key == "stock").distinct()
            )
        }

        # ModelColor -> sku count, and the set of markets holding any of its SKUs.
        mc_skus: dict[str, int] = defaultdict(int)
        mc_markets: dict[str, set[str]] = defaultdict(set)
        mc_sku_list: dict[str, list[str]] = defaultdict(list)
        sku_to_mc: dict[str, str] = {}
        sku_size: dict[str, str] = {}
        # Newest Omnitron modification across the ModelColor's SKUs — the
        # header's LAST CHANGE. Was hardcoded to the April export date, which
        # told everyone the data was four months stale when the sync had run
        # that morning.
        mc_changed: dict[str, object] = {}
        for sku, mc, size, modified in db.execute(
            select(
                Product.sku, Product.model_color, Product.size,
                Product.omnitron_modified_date,
            ).where(Product.is_size())
        ):
            mc_skus[mc] += 1
            mc_sku_list[mc].append(sku)
            sku_to_mc[sku] = mc
            # Fall back to the SKU's own tail (`…TR2.TXS` -> `TXS`) so a row
            # still identifies itself when `size` was never populated.
            sku_size[sku] = size or sku.rsplit(".", 1)[-1]
            if modified is not None:
                prev = mc_changed.get(mc)
                if prev is None or modified > prev:
                    mc_changed[mc] = modified

        member_sku: dict[str, set[str]] = defaultdict(set)
        for mc, sku, market in db.execute(
            select(Product.model_color, Product.sku, CatalogueMembership.market)
            .join(CatalogueMembership, CatalogueMembership.product_id == Product.id)
            .where(CatalogueMembership.left_at.is_(None))
        ):
            mc_markets[mc].add(market)
            member_sku[sku].add(market)

        # (model_color, attribute, market) -> value, rolling SKU-grain values up.
        values: dict[tuple[str, str, str], str] = {}
        by_mc: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
        # Keyed on **SKU**, not ModelColor. Liveness is a SKU-level fact and is
        # never rolled up (vault, 2026-08-13), and unioning these per ModelColor
        # silently invents live products: size T4 is in the catalogue, T6 has a
        # price, T8 has stock, no single size has all three — and the ModelColor
        # counted as live anyway. That is precisely the case of a size removed
        # from the catalogue still counting because a sibling was not.
        priced_sku: dict[str, set[str]] = defaultdict(set)
        stocked_sku: dict[str, set[str]] = defaultdict(set)
        priced: dict[str, set[str]] = defaultdict(set)
        stocked: dict[str, set[str]] = defaultdict(set)
        # Best stock any one SKU of this ModelColor carries, in any market.
        # Max rather than sum: eight sizes of one unit each is not eight units
        # of a sellable product, and a threshold is a question about depth.
        best_stock: dict[str, int] = defaultdict(int)
        # Kept per SKU as well as rolled up: "which sizes are out of stock" is
        # the question someone actually acts on, and a ModelColor total of 312
        # units hides that four of eleven sizes are on zero.
        sku_price: dict[str, dict[str, str]] = defaultdict(dict)
        sku_stock: dict[str, dict[str, int]] = defaultdict(dict)
        # The barcode is a size-level identity that nothing else carries, so
        # without this it is only ever visible as one arbitrary size's value.
        sku_ean: dict[str, str] = {}
        # (attribute, market) pairs whose SKUs do not agree, per ModelColor.
        # `values` below keeps the first value it sees and drops the rest,
        # which is right for a ModelColor-grain attribute (there is only one)
        # and wrong for a SKU-grain one: the article code contains the size,
        # so every size differs by construction and the matrix was showing
        # whichever one happened to be read first. A cell that cannot have a
        # single answer must not print one.
        varies: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

        for entity_kind, entity_key, attribute_id, market, value in db.execute(
            select(
                AttributeValue.entity_kind, AttributeValue.entity_key,
                AttributeValue.attribute_id, AttributeValue.market, AttributeValue.value,
            ).where(AttributeValue.state_lane == "current")
        ):
            key = attr_key.get(attribute_id)
            if key is None or value is None:
                continue
            mc = entity_key if entity_kind == "model_color" else sku_to_mc.get(entity_key)
            if mc is None:
                continue
            if key == "price":
                priced[mc].add(market)
                if entity_kind == "sku":
                    priced_sku[entity_key].add(market)
                    sku_price[entity_key][market] = value
            elif key == "stock":
                try:
                    qty = float(value)
                except ValueError:
                    qty = 0.0
                # Imported, not restated. This line held its own hardcoded
                # `> 1` until 2026-08-20 — a second copy of the rule that the
                # canonical one in `rules/liveness` could not reach. Changing
                # the threshold there would have moved nothing anyone could
                # see, because this is the code that produces the panel's
                # numbers.
                if qty > STOCK_THRESHOLD:
                    stocked[mc].add(market)
                    if entity_kind == "sku":
                        stocked_sku[entity_key].add(market)
                if entity_kind == "sku":
                    # Zero is a fact worth keeping, not an absence: a size on
                    # zero across every market is exactly what blocks a market
                    # going live, and dropping it would render as "no data".
                    sku_stock[entity_key][market] = int(qty)
                best_stock[mc] = max(best_stock[mc], int(qty))
            if key == "ean" and entity_kind == "sku":
                sku_ean[entity_key] = value
            first = values.setdefault((mc, key, market), value)
            if first is value:
                # Same data, indexed by ModelColor. Built here because doing it
                # per product meant scanning all ~380k values 16,901 times.
                by_mc[mc].setdefault(key, {})[market] = value
            elif first != value:
                varies[mc][key].add(market)

        # The coverage extract, read through `reports/coverage` rather than
        # assembled here from the locals above.
        #
        # It costs a second pass over `attribute_values`, which is the
        # expensive query in this script — and that is the right trade. The
        # rules that decide whether a value is missing or merely unread live in
        # one module, and this generator's own history is the argument: it
        # carried its own copy of three liveness rules for four months, and the
        # panel called banned products live the whole time.
        coverage = to_extract(load_catalogue(db))

        # The funnel, read through `reports/overview` for the same reason: the
        # CLI report and this extract must be the same numbers, and the only
        # way to guarantee that is one function.
        #
        # The snapshot is written here rather than in the report because this
        # is the path that runs nightly. A manual rebuild through the panel
        # lands on the same date and refines that row instead of adding one,
        # so "a week ago" stays unambiguous however often the button is
        # pressed.
        funnel_data = load_funnel(db)
        overview = build_overview(funnel_data)
        today = datetime.now(UTC).date()
        overview["deltas"] = compute_deltas(db, overview, today)
        snapshotted = record_snapshot(db, overview, today)

    def v(mc: str, key: str, market: str = "*") -> str | None:
        return values.get((mc, key, market))

    # Wrong-language detection, run here at generation so the number ships
    # with the data and nothing downstream re-implements it (the rule module
    # is explicit about that). Only the English verdict is trusted, only
    # local-market fields are judged, and only confident calls are kept —
    # below the threshold the flag is noise, and a flag that cries wolf
    # stops being read.
    # Majority spelling per casefolded season, so the facet shows one row per
    # season in whichever spelling Omnitron uses most. Shared with the funnel
    # report, which slices by season and has to agree with these facet values
    # exactly or its links land on an empty page.
    _season_canon = fold_seasons(
        value
        for (_mc, key, _market), value in values.items()
        if key == "pl_sezon" and value
    )

    def _fold_season(raw: str | None) -> str:
        if not raw:
            return "—"
        return _season_canon.get(raw.casefold(), raw)

    _LANG_FIELDS = ("product_name", "product_detail", "description")
    en_flags: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for (mc, key, market), value in values.items():
        if key not in _LANG_FIELDS or market == "*" or not value:
            continue
        conf = english_confidence(value)
        if conf >= 0.90:
            en_flags[mc].setdefault(key, {})[market] = round(conf, 2)

    products: list[dict] = []
    details: dict[str, dict] = {}
    funnel = {
        "all": 0, "no catalogue": 0, "no price": 0,
        "below min stock": 0, "not live anywhere": 0, "kept": 0,
    }

    for mc in sorted(mc_skus):
        funnel["all"] += 1
        in_market = mc_markets.get(mc, set())

        # Filters, applied cheapest-signal-first so the funnel reads as a
        # diagnosis rather than a single yes/no. Most of the 18,036 are old
        # products nobody intends to sell; showing them buries the ones that
        # matter and distorts every coverage percentage in the panel.
        if args.require_catalogue and not in_market:
            funnel["no catalogue"] += 1
            continue
        if args.require_price and not priced.get(mc):
            funnel["no price"] += 1
            continue
        if best_stock.get(mc, 0) < args.min_stock:
            funnel["below min stock"] += 1
            continue
        # Liveness comes from `rules/funnel.market_liveness`, not from a copy
        # of it.
        #
        # This block reimplemented three of the six rules inline until
        # 2026-08-20 and silently skipped ban and image — so the panel showed
        # 751KFA2471.K78 as live in CZ, SK and HU while its own images panel
        # said "the product cannot go live without one", and the market tile
        # asserted "image and ban checks pass" for checks that never ran.
        # ~150-250 ModelColors per market were live on paper and banned in
        # fact. See `Findings/Audit 2026-08-20`, F5. It then sat here untested
        # for another five days, which is why it now lives in the rules with
        # the grain and stock-guard cases pinned by name.
        image = v(mc, "image_0")
        verdict = classify_comment(v(mc, "pl_comment"), ban_table)
        banned = verdict is BanVerdict.BANNED

        verdicts = market_liveness(
            MARKETS,
            mc_sku_list.get(mc, ()),
            in_markets=in_market,
            member=member_sku,
            priced=priced_sku,
            stock=sku_stock,
            markets_priced=markets_priced,
            markets_stocked=markets_stocked,
            banned=banned,
            images_synced=images_synced,
            has_image=bool(image),
        )
        # Observed beats derived. `funnel_data.is_live` falls back to the rules
        # for any market the storefront pull has not reached, so this is the
        # real answer where we have one and the old answer where we do not.
        live = {m: funnel_data.is_live(mc, m) for m in MARKETS}
        # The derived reason, kept whatever `live` says. Until now this was
        # only written when the option was not live, which made the interesting
        # case invisible: an option the shop is selling while our own rules
        # call it blocked is not a contradiction to hide, it is the queue.
        blocked = {
            m: verdicts[m].blocker for m in MARKETS if not verdicts[m].live
        }

        if args.live_only and not any(live.values()):
            funnel["not live anywhere"] += 1
            continue
        funnel["kept"] += 1

        # These strings are facet values the Products page filters on, and the
        # Overview page links to. They are built from one definition in
        # `rules/funnel` so a rename cannot leave a work queue pointing at
        # nothing — an empty queue looks exactly like a job already done.
        missing = [
            missing_label(m, label)
            for m in MARKETS
            for label, key in (("name", "product_name"), ("detail", "product_detail"),
                               ("description", "description"))
            if v(mc, key, m) is None
        ]
        defects = []
        for m in MARKETS:
            name, detail = v(mc, "product_name", m), v(mc, "product_detail", m)
            if name and detail and name != detail:
                defects.append(defect_name_detail(m))

        image = v(mc, "image_0")
        # Blockers as facets rather than as exclusions. These are the reasons a
        # product is not sellable, which is the whole subject of this panel —
        # filtering them out of the extract would remove precisely the products
        # someone opens it to find, and would quietly shrink every denominator.
        if not priced.get(mc):
            defects.append("No price anywhere")
        if not stocked.get(mc):
            defects.append("No stock anywhere")
        if not in_market:
            defects.append("Not in any catalogue")
        if not image:
            defects.append("No image")
        for m in MARKETS:
            if m in in_market and m not in priced.get(mc, ()):
                missing.append(missing_label(m, "price"))
            if m in in_market and m not in stocked.get(mc, ()):
                missing.append(missing_label(m, "stock"))
            # In some catalogues but not this one. Distinct from the
            # "Not in any catalogue" defect above, and actionable in a way it
            # is not: somebody decided this market's rollout and stopped, so
            # the funnel can hand back a list rather than a total.
            if in_market and m not in in_market:
                missing.append(missing_label(m, "catalogue"))

        # Which registry attributes that gate go-live are actually absent, and
        # whose problem each one is. `write_channel` already answers the second
        # question — the panel used to call every gap "ours", which puts the
        # Turkey team's fields in our queue and quietly loses the ones we can
        # only chase.
        # A filled field in the wrong language passes every not-null check,
        # so it must be a defect, never a gap. One entry per field and market,
        # named the way the reviewer will act on it.
        for fkey, fmarkets in en_flags.get(mc, {}).items():
            flabel = {"product_name": "name", "product_detail": "detail",
                      "description": "description"}[fkey]
            for fm in sorted(fmarkets):
                defects.append(defect_english(fm, flabel))

        gaps = []
        for meta in registry:
            if not meta["gatesGoLive"]:
                continue
            if meta["perMarket"]:
                for m in MARKETS:
                    if m in in_market and not v(mc, meta["key"], m):
                        gaps.append(
                            {"scope": m, "label": meta["label"], "owner": meta["owner"]}
                        )
            elif not v(mc, meta["key"]):
                gaps.append(
                    {"scope": "ALL", "label": meta["label"], "owner": meta["owner"]}
                )

        markets_block = {
            m: {
                "name": v(mc, "product_name", m),
                "detail": v(mc, "product_detail", m),
                "desc": v(mc, "description", m),
                "storefront": v(mc, "storefront_name", m),
                "price": v(mc, "price", m),
                "stock": v(mc, "stock", m),
                "live": live[m],
            }
            for m in MARKETS
        }

        products.append(
            {
                "modelColor": mc,
                "name": v(mc, "product_name", "PL") or v(mc, "storefront_name", "PL") or mc,
                "imageUrl": image,
                "imageCount": 1 if image else 0,
                # Case-folded to the dominant spelling: Omnitron holds both
                # OUTLET (9,138) and Outlet (2,337) and they are one season —
                # the vocabulary decision already matches case-insensitively,
                # and a facet that splits them overstates the variety of the
                # data. Format variants (26AW vs AW26) are NOT folded: those
                # are upstream defects worth seeing. The matrix still shows
                # the raw value.
                "season": _fold_season(v(mc, "pl_sezon")),
                "erpSeason": _fold_season(v(mc, "pl_sezon")),
                # Two different attributes, and they were both reading
                # `sub_category` — so the Products page rendered a Category
                # facet and a Sub-category facet holding identical values
                # (Polo 2,473 above Polo 2,473), and `main_category` never
                # reached the UI at all despite being sourced since 2026-08-18
                # and filled on 23,593 options.
                #
                # `main_category` is the top of the tree — Giyim, Ayakkabı,
                # Aksesuar — which is the cut a merchandiser actually starts
                # from, so the duplicate facet was also the more useful one
                # missing.
                "mainCategory": v(mc, "main_category") or "—",
                "subCategory": v(mc, "sub_category") or "—",
                "gender": v(mc, "gender") or "—",
                "skuCount": mc_skus[mc],
                "live": live,
                "blockedBy": blocked,
                #: Which markets' `live` was measured rather than computed.
                #: Absent on extracts built before the storefront stage.
                "observed": {
                    m: mc in funnel_data.listed[m]
                    for m in MARKETS
                    if funnel_data.listed.get(m)
                },
                "missing": missing,
                "gaps": gaps,
                "defects": defects,
                "markets": sorted(in_market),
                "lastChangedAt": (
                    mc_changed[mc].strftime("%d %b %Y") if mc in mc_changed else None
                ),
                "lastChangedVia": "Omnitron sync" if mc in mc_changed else None,
            }
        )
        # Every value this ModelColor holds, keyed attribute -> market. The
        # matrix renders from this; `pl` and `markets` below stay for the
        # cards and the older callers that read them.
        attr_values = by_mc.get(mc, {})

        # Sizes, in the order they were seen, each with its own stock and price
        # per market. Screenshot-for-screenshot what the design asks for, and
        # every number is a row in `attribute_values` at SKU grain.
        sizes = [
            {
                "label": sku_size.get(sku, sku),
                "sku": sku,
                "stock": {m: sku_stock[sku][m] for m in MARKETS if m in sku_stock[sku]},
                "price": {m: sku_price[sku][m] for m in MARKETS if m in sku_price[sku]},
                "ean": sku_ean.get(sku),
            }
            for sku in sorted(
                mc_sku_list.get(mc, ()),
                key=lambda s: _size_order(sku_size.get(s, s)),
            )
        ]

        details[mc] = {
            "attributes": attr_values,
            # attribute -> the markets whose sizes disagree. The matrix reads
            # this and says "per size" rather than printing one size's value
            # as though it were the option's.
            "perSize": {k: sorted(ms) for k, ms in sorted(varies.get(mc, {}).items())},
            "en": en_flags.get(mc, {}),
            "sizes": sizes,
            "photos": [image] if image else [],
            "pl": {
                "name": v(mc, "product_name", "PL"),
                "detail": v(mc, "product_detail", "PL"),
                "season": v(mc, "pl_sezon"),
                "comment": v(mc, "pl_comment"),
                "price": v(mc, "price", "PL"),
            },
            "stock": 0,
            "markets": markets_block,
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "products.json").write_text(
        json.dumps(products, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (OUT_DIR / "details.json").write_text(
        json.dumps(details, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (OUT_DIR / "attributes.json").write_text(
        json.dumps(registry, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (OUT_DIR / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (OUT_DIR / "coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (OUT_DIR / "overview.json").write_text(
        json.dumps(overview, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    # Small on purpose, and separate on purpose. `products.json` is a bare
    # array and runs to megabytes; a live count needs its `as of` beside it on
    # the page, and neither wrapping the array nor stamping every row is worth
    # the cost of one more request.
    (OUT_DIR / "meta.json").write_text(
        json.dumps(
            {
                "generatedAt": datetime.now(UTC).isoformat(),
                "observedAt": (
                    funnel_data.observed_at.isoformat()
                    if funnel_data.observed_at
                    else None
                ),
                "observedMarkets": list(funnel_data.observed_markets()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    live_counts = {m: sum(1 for p in products if p["live"][m]) for m in MARKETS}
    print("funnel:")
    for label in ("all", "no catalogue", "no price", "below min stock",
                  "not live anywhere", "kept"):
        if funnel[label] or label in ("all", "kept"):
            print(f"  {label:20} {funnel[label]:>7}")
    print()
    print(f"model colors      {len(products)}")
    print(f"with an image     {sum(1 for p in products if p['imageUrl'])}")
    for m in MARKETS:
        print(f"live in {m:12} {live_counts[m]}")
    print(f"attributes        {len(registry)}")
    print(f"localised terms   {sum(len(v) for v in labels.values())}")
    print(f"coverage options  {len(coverage['products'])}")
    print(f"never-read fields {len(coverage['unread'])}")
    print(f"funnel slices     {len(overview['slices'])}")
    if not snapshotted:
        # Never fatal, so it has to be said out loud — a baseline that silently
        # did not get written is a delta that silently never appears.
        print("snapshot          NOT WRITTEN — next week has no baseline")
    else:
        print(
            "snapshot          "
            + (f"{today} (deltas vs {overview['deltas']['all']['baselineDate']})"
               if overview["deltas"]
               else f"{today} (no baseline yet)")
        )
    print(f"written           {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
