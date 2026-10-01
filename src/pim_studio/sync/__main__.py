"""CLI for the sync stages — `python -m pim_studio.sync <stage> [--full]`.

Deliberately thin. Each stage is a function registered in `STAGES`; this module
resolves the name, opens one client for the run, and gets out of the way. Run
with no arguments to list what exists.

Every stage takes `(api, full)`. `full` is not a performance knob — it decides
whether the stage is allowed to conclude that something upstream has been
*removed*. The default is incremental because that is the nightly cadence; the
weekly reconciliation passes `--full`. A stage with no watermark behind it
promotes itself to full, so a first run is always complete.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from .. import integrations
from ..logging import configure_logging, log
from ..omnitron.client import OmnitronClient, OmnitronError
from . import (
    catalogue,
    labels,
    liveness,
    pipeline,
    products,
    registry,
    scope,
    storefront,
    values,
)
from .runs import (
    IncrementalSweepViolation,
    SyncMode,
    reconcile_overdue_days,
    reconciliation_overdue,
    sync_run,
)

#: A stage: given a client and whether this is a full pass, do the work and
#: return a process exit code.
StageFn = Callable[[OmnitronClient, bool], int]


class StageNotConfigured(RuntimeError):
    """A stage ran but had nothing mapped to work on.

    Raised rather than returned because these checks sit inside a `sync_run`,
    which reads "no exception" as success. Returning an exit code from in
    there tells the shell it failed and the database that it succeeded, and
    the database is the half every later reader trusts.
    """

#: stage name -> (callable, one-line description). Stages land here as they are
#: built; the CLI is the only place that needs to know they exist.
STAGES: dict[str, tuple[StageFn, str]] = {}


def stage(name: str, description: str) -> Callable[[StageFn], StageFn]:
    def register(fn: StageFn) -> StageFn:
        STAGES[name] = (fn, description)
        return fn

    return register


def _mode_line(mode: SyncMode) -> str:
    """The first line every mode-aware stage prints.

    The watermark is shown, not just the word `incremental`, because "this run
    read a window" and "this run read the window starting 14 August" are
    different amounts of information when a number looks wrong.
    """
    window = f" (since {mode.since:%Y-%m-%d %H:%M} UTC)" if mode.since else ""
    return f"{'mode':22} {mode.label}{window}"


@stage("verify-scope", "confirm our four catalogues still point where we think")
def _verify_scope(api: OmnitronClient, full: bool = True) -> int:
    """Check the hardcoded market ids against the live tenant.

    Worth running before any scheduled sync. The ids in `scope.py` were read on
    one day; if the tenant is reconfigured, a sync using stale ids keeps
    succeeding while reading another market's data.
    """
    # Nothing to be incremental about: this reads six ids and compares them.
    del full
    print(f"host  {api.base_url}\n")
    print(f"{'market':7} {'channel':>8} {'catalog':>8} {'price':>7} {'stock':>7} {'tree':>6}")
    for m in scope.MARKETS.values():
        flag = "  (channel inactive)" if not m.channel_is_active else ""
        print(
            f"{m.code:7} {m.channel_pk:>8} {m.catalog_pk:>8} {m.price_list_pk:>7} "
            f"{m.stock_list_pk:>7} {m.category_tree_pk:>6}{flag}"
        )

    drift = scope.verify_against_live(api)
    if drift:
        print("\nDRIFT — the tenant no longer matches scope.py:", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nDo not run a scheduled sync until this is reconciled: the ids "
            "still resolve, so every stage would succeed while reading the "
            "wrong market.",
            file=sys.stderr,
        )
        return 1

    print("\nscope verified — all four catalogues match scope.py")
    if scope.EXTRA_PRICE_LISTS_IGNORED:
        print("\ndeclared but deliberately not read:")
        for pk, why in scope.EXTRA_PRICE_LISTS_IGNORED.items():
            print(f"  price list {pk} — {why}")
    return 0


@stage("registry", "mirror Omnitron's attributes, vocabulary, sets and configs")
def _registry(api: OmnitronClient, full: bool = False) -> int:
    """Full read of all four registry resources.

    Not scoped by market, and that is correct rather than an oversight: an
    attribute definition has no channel dimension — there is no PL copy of
    `integration_yaka` to prefer over a TR one. The four-channel line in
    `scope.py` governs market data, and the reconciliation report is where
    "this attribute is only used by channels we ignore" gets said.
    """
    # A full read every time, by design — `registry.py` explains why the
    # watermark is deliberately left unused here. Four resources, ~1 minute.
    del full
    stages = (
        ("attributes", registry.sync_attributes),
        ("attribute_values", registry.sync_attribute_values),
        ("attribute_sets", registry.sync_attribute_sets),
        ("attribute_configs", registry.sync_attribute_configs),
    )
    header = f"{'resource':20} {'read':>7} {'new':>7} {'changed':>8} {'same':>8} {'removed':>8}"
    print(header)
    with sync_run("omnitron_registry") as run:
        for label, fn in stages:
            s = fn(api, run)
            print(
                f"{label:20} {s['read']:>7} {s['inserted']:>7} "
                f"{s['updated']:>8} {s['unchanged']:>8} {s['removed']:>8}"
            )
        if run.rows_dropped:
            print(f"\ndropped {run.rows_dropped}:")
            for reason, n in sorted(run.drop_reasons.items()):
                print(f"  {n:>6}  {reason}")
    return 0


@stage("products", "refresh our own SKUs against the live API, then apply deletions")
def _products(api: OmnitronClient, full: bool = False) -> int:
    """Two passes under one run: identity refresh, then the deletion feed.

    Order matters — the deletion pass matches on `omnitron_pk`, which the
    refresh is what populates. Scope stays our own SKU list by construction:
    `sku__in` names every product we ask about, so there is nothing here for
    a missing filter to widen.
    """
    # Already incremental on its own terms and safely so: the deletion feed is
    # append-only, so windowing it can miss nothing a full read would find.
    # There is no absence to conclude here, hence no mode to honour.
    del full
    with sync_run("omnitron_products") as run:
        refreshed = products.refresh_products(api, run)
        deletions = products.delist_deleted(api, run)

        print(f"{'our skus':22} {refreshed['skus']}")
        print(f"{'found upstream':22} {refreshed['found']}")
        print(f"{'not found':22} {refreshed['missing']}")
        print(f"{'identity changed':22} {refreshed['updated']}")
        print(f"{'base_code disagrees':22} {refreshed['base_code_disagreements']}")
        print(f"{'deletions in feed':22} {deletions['deletions_in_feed']}")
        print(f"{'ours delisted':22} {deletions['ours_delisted']}")
    return 0


@stage("catalogue", "discover our product set from catalogue membership")
def _catalogue(api: OmnitronClient, full: bool = False) -> int:
    """Reconcile `products` against what the four catalogues actually hold.

    Run this before `products` and `values`: it defines the set those two then
    work over. Long — four catalogue passes plus resolution of everything new.

    Measured across every recorded run to 2026-09-03: the incremental pass
    averages **7 seconds** and the full one 16m 46s. That gap is why this was
    split, and the 7 seconds is also the warning — fourteen consecutive nightly
    incrementals read 0 rows and refreshed nothing, because upstream barely ever
    touches `catalog_items.modified_date`. A stage that legitimately sees
    nothing looks exactly like a stage that is broken.

    Cron now runs this full every night. On a night with no changes an
    incremental read still sees nothing, and "nothing" would retire everything —
    so the sweep stays behind `mode.full` regardless of what the schedule says.
    """
    mode = SyncMode.resolve("omnitron_catalogue", full)
    with sync_run("omnitron_catalogue", mode=mode) as run:
        s = catalogue.discover_products(api, run, mode)
        print(_mode_line(mode))
        print(f"{'products in our 4':22} {s['catalogue_union']}")
        print(f"{'already known':22} {s['already_known']}")
        print(f"{'newly created':22} {s['products_created']}")
        print(f"{'unresolvable':22} {s['unresolvable']}")
        print(f"{'memberships added':22} {s['added']}")
        print(f"{'memberships refreshed':22} {s['refreshed']}")
        if mode.full:
            print(f"{'memberships departed':22} {s['departed']}")
        else:
            print(f"{'memberships departed':22} not checked — incremental pass")
    return 0


@stage("liveness", "sync price and stock per market, and product images")
def _liveness(api: OmnitronClient, full: bool = False) -> int:
    """The last inputs the panel needs: price, stock, image.

    This stage once failed twice in a row, the second attempt after five and a
    half hours, and the incremental path was built to make it survivable. That
    is no longer why it exists. Measured across every recorded run to
    2026-09-03, the full pass averages 31m 44s and has never exceeded 34m 12s,
    against 16m 11s for the incremental — so the split now buys about fifteen
    minutes, not the difference between finishing and not.

    Worth stating plainly because the old number outlived its truth and was
    still shaping the schedule a fortnight later: cron runs this full every
    night, and the reason is that reconciliation is worth fifteen minutes.
    """
    mode = SyncMode.resolve("omnitron_liveness", full)
    with sync_run("omnitron_liveness", mode=mode) as run:
        s = liveness.sync_liveness(api, run, mode)
        print(_mode_line(mode))
        for code in ("PL", "CZ", "HU", "SK"):
            print(f"{'values ' + code:22} {s.get('values_' + code, 0)}")
        if mode.full:
            print(f"{'retired':22} {s['retired']}")
        else:
            print(f"{'retired':22} not checked — incremental pass")
        i = liveness.sync_images(api, run)
        print(f"{'modelcolors w/ image':22} {i['model_colors']}")
        print(f"{'our photos landed':22} {i['landed']}")
        print(f"{'total written':22} {s['written'] + i['written']}")
    return 0


@stage("images", "capture one image per ModelColor — market-agnostic")
def _images(api: OmnitronClient, full: bool = True) -> int:
    """Split from `liveness` on purpose.

    Price and stock are per market and cost four filtered reads; images hang
    off the product and cost one pass over every product. Bundling them meant
    re-reading ~4,000 pages of price and stock just to retry images, which is
    how the image phase kept being the expensive thing to recover.
    """
    with sync_run("omnitron_images") as run:
        s = liveness.sync_images(api, run)
        print(f"{'modelcolors w/ image':22} {s['model_colors']}")
        print(f"{'our photos landed':22} {s['landed']}")
        print(f"{'written':22} {s['written']}")
    return 0


@stage("storefront", "ask the four shops what they are selling - observed liveness")
def _storefront(api: OmnitronClient, full: bool = True) -> int:
    """The only stage that does not touch Omnitron.

    `api` is accepted and ignored. Every stage takes it; giving this one a
    different shape to save an unused argument would mean the runner had to
    know which is which, and the runner not knowing is what keeps
    `pipeline.run` a loop rather than a switch.

    `full` is ignored too, and that is not a gap: 46 pages *is* the whole
    listing, so every pull is a full pass and the departure sweep is always
    sound. What makes it sound is `read_listing` refusing to return a partial
    set, not a mode flag.
    """
    with sync_run("storefront") as run:
        s = storefront.sync_storefront(run)
        print(f"{'observed at':22} {s['observed_at']}")
        print()
        print(f"{'market':10} {'listed':>8} {'new':>6} {'gone':>6}  note")
        for market in ("PL", "CZ", "SK", "HU"):
            if f"error_{market}" in s:
                print(f"{market:10} {'failed':>8}  {s['error_' + market][:60]}")
                continue
            skew = s.get(f"shortfall_{market}", 0)
            note = (
                f"index shifted mid-crawl, {skew} missed; departures held over"
                if skew
                else ""
            )
            print(
                f"{market:10} {s['listed_' + market]:>8,} "
                f"{s['added_' + market]:>6,} {s['left_' + market]:>6,}  {note}"
            )
        if s["markets_failed"]:
            raise StageNotConfigured(
                f"{s['markets_failed']} of 4 storefronts could not be read. "
                "Their listings were left as they were rather than swept - a "
                "partial read is indistinguishable from a market withdrawing "
                "its whole range."
            )
    return 0


@stage("proposals", "propose a translation for values in the wrong language")
def _proposals(api: OmnitronClient, full: bool = True) -> int:
    """Find PL/CZ/SK/HU content holding English or Turkish, and offer a
    replacement into the `proposal` lane for a person to accept or reject.

    Writes nothing to the `current` lane and never reaches Omnitron — a
    translation of a garment description is a suggestion, and the lifecycle
    already had a word for that.

    **Part of `all`**, unlike the push. The push is excluded because it changes
    the live catalogue; this only fills a lane nothing reads until somebody
    accepts a row, so leaving it to be remembered by hand meant a backlog that
    only shrank when someone thought of it. Running it here also makes it
    self-limiting: values that already carry a proposal are skipped, so each
    night adds the next batch rather than re-offering the same ones.

    `api` and `full` are accepted and ignored.
    """
    from ..db import session_scope
    from . import proposals

    with sync_run("proposals") as run:
        with session_scope() as db:
            s = proposals.generate(
                db,
                [
                    proposals.SiblingSource(db),
                    proposals.GroupingRowSource(db),
                    proposals.MachineSource(),
                ],
                changed_by="cli",
                sync_run_id=run.id,
            )
        run.rows_seen = s["candidates"]
        run.rows_imported = s["proposed"]
        if s.get("withdrawn"):
            print(f"{'withdrawn, no longer needed':26} {s['withdrawn']:,}")
        print(f"{'in the wrong language':26} {s['candidates']:,}")
        for language, n in sorted(s["by_language"].items()):
            print(f"  {language.lower():24} {n:,}")
        print(f"{'the sources could answer':26} {s.get('answerable', 0):,}")
        print(f"{'proposals written':26} {s['proposed']:,}")
        if s.get("held_back"):
            print(f"{'held back by the cap':26} {s['held_back']:,} "
                  f"(run it again to take the next batch)")
        for name, n in sorted(s["by_source"].items()):
            print(f"  from {name:21} {n:,}")
        if s["candidates"] and not s["proposed"]:
            print()
            print("Nothing could be proposed. The free sources only offer text the")
            print("catalogue already holds — two colours of one base code carrying it")
            print("identically, or the style's own grouping row — and no translator")
            print("is configured for the rest.")
    return 0


@stage("publication", "ask each shop which pushed values it is actually serving")
def _publication(api: OmnitronClient, full: bool = True) -> int:
    """Close the loop a push cannot close on its own.

    The push confirms against Omnitron seconds after writing. This confirms
    against the shop, which on the first real push was two hours behind — and
    on a different channel, 27 seconds. Until it runs, a pushed value reads as
    written but not yet visible, which is the honest state.

    Costs one query on a day nobody pushed: only markets with something
    outstanding are crawled. `api` and `full` are accepted and ignored, for the
    reason given on the storefront stage.
    """
    from ..db import session_scope
    from . import publication

    with sync_run("publication") as run:
        with session_scope() as db:
            s = publication.confirm_published(db)
        run.rows_seen = s["outstanding"]
        run.rows_imported = s["published"]
        print(f"{'outstanding':22} {s['outstanding']:,}")
        if s.get("global_outstanding"):
            print(f"{'  of them global':22} {s['global_outstanding']:,} "
                  f"(asked of every shop until one serves it)")
        print(f"{'now on the shop':22} {s['published']:,}")
        print(f"{'markets read':22} {s['markets_read']}")
        for market, why in sorted(s["skipped_markets"].items()):
            print(f"  {market} skipped: {why}")
        if s["outstanding"] and not s["markets_read"] and s["skipped_markets"]:
            raise StageNotConfigured(
                "Every market with outstanding values failed to read, so "
                "nothing could be confirmed. The values are unchanged and "
                "will be asked about again - this is not a failed push."
            )
    return 0


@stage("labels", "mirror dropdown vocabularies in all four market languages")
def _labels(api: OmnitronClient, full: bool = True) -> int:
    """Localised labels, and which terms Omnitron has never translated."""
    with sync_run("omnitron_labels") as run:
        s = labels.sync_labels(api, run)
        if not s:
            raise StageNotConfigured("no dropdown attributes mapped — nothing to localise")
        print(f"{'attributes':22} {s['attributes']}")
        print(f"{'label rows':22} {s['rows']}")
        for market in ("PL", "CZ", "SK", "HU"):
            print(f"{'  translated ' + market:22} {s['translated_' + market]}%")
        if s["ambiguous"]:
            print()
            print(
                f"{s['ambiguous']} terms share a baseline label with a "
                "different translation, so which applies to a product cannot "
                "be told from the label alone. Recorded as ambiguous rather "
                "than guessed."
            )
    return 0


@stage("push", "push approved values into Omnitron — runs every pending push batch")
def _push(api: OmnitronClient, full: bool = True) -> int:
    """The one stage that writes. Refuses unless the configuration allows it.

    Not part of `all`: a push is something a person starts, from the Imports
    page or here, and the nightly pipeline must never carry one along.
    """
    from sqlalchemy import select

    from .. import locks
    from ..db import session_scope
    from ..models import PushBatch
    from . import push

    with locks.held(locks.OMNITRON_PUSH) as got:
        if not got:
            print("another push holds the lock; nothing started")
            return 1
        with session_scope() as db:
            pending = list(
                db.scalars(
                    select(PushBatch.id)
                    .where(PushBatch.status == push.BATCH_PENDING)
                    .order_by(PushBatch.id)
                )
            )
        if not pending:
            print("no pending push batches")
            return 0
        worst = 0
        for batch_id in pending:
            with sync_run(push.RUN_KIND, triggered_by="cli") as run:
                s = push.run_batch(api, batch_id, run)
            print(f"{'batch':22} #{batch_id} {s['status']}")
            print(f"{'items':22} {s['items']}")
            print(f"{'written':22} {s['written']}")
            print(f"{'confirmed':22} {s['confirmed']}")
            print(f"{'failed':22} {s['failed']}")
            print(f"{'skipped':22} {s['skipped']}")
            if s["halted"]:
                print(f"{'HALTED':22} {s['halted']}")
                worst = 2
            elif s["status"] != push.BATCH_DONE:
                worst = max(worst, 1)
        return worst


@stage("values", "import attribute values for our products — fills attribute_values")
def _values(api: OmnitronClient, full: bool = True) -> int:
    """The stage that takes `attribute_values` from zero rows to real data."""
    with sync_run("omnitron_values") as run:
        s = values.import_values(api, run)
        if not s:
            raise StageNotConfigured(
                "no attribute_columns mapped for source=omnitron — nothing to import"
            )
        print(f"{'mappings':22} {s['mappings']}")
        print(f"{'skus read':22} {s['skus']}")
        if s.get("grouping_rows"):
            print(
                f"{'grouping rows read':22} {s['grouping_rows_found']} of {s['grouping_rows']}"
            )
        print(f"{'locale views':22} {s['fetch_locales']}")
        print(f"{'values inserted':22} {s['inserted']}")
        print(f"{'values changed':22} {s['updated']}")
        print(f"{'confirmed live':22} {s['confirmed']}")
        if s.get("cleared_unrendered"):
            print(
                f"{'values cleared':22} {s['cleared_unrendered']}"
                "  (the key the shop renders no longer holds them)"
            )
        if s.get("shared_key_proposed") or s.get("shared_key_withdrawn"):
            print(
                f"{'shared-key proposals':22} {s['shared_key_proposed']}"
                f"  (withdrawn {s['shared_key_withdrawn']})"
            )
        print(f"{'values unchanged':22} {s['unchanged']}")
        print(f"{'cosmetic resolved':22} {s['resolved_cosmetic']}")
        for rule in ("whitespace", "truncation", "case"):
            if s.get(f"resolved_{rule}"):
                print(f"{'  by ' + rule:22} {s['resolved_' + rule]}")
        print(f"{'modelcolor conflicts':22} {s['conflicts']}")
        print(f"{'mapping collisions':22} {s['mapping_collisions']}")
        print(f"{'ours protected':22} {s['protected']}")
        if s.get("locale_coverage"):
            print()
            print(
                "locale routing        found: designated/fallback/baseline/echo · "
                "withheld: empty/suspect"
            )
            for row in s["locale_coverage"]:
                found = (
                    f"{row['designated']}/{row['fallback']}/{row['baseline']}"
                    f"/{row['equals_baseline']}"
                )
                withheld = f"{row['empty']}/{row['suspect_skipped']}"
                extra = ""
                if row["fallback_locales"]:
                    inside = ", ".join(
                        f"{loc}:{n}" for loc, n in sorted(row["fallback_locales"].items())
                    )
                    extra = f"  (fallback {inside})"
                if row["suspect_written"]:
                    extra += f"  ({row['suspect_written']} written flagged EN?)"
                label = f"{row['attribute']} {row['market']}"
                print(f"  {label:20} {found:18} · {withheld}{extra}")
            if any(row["equals_baseline"] for row in s["locale_coverage"]):
                print(
                    "  echo: the market locale answered with the default-locale text — "
                    "written (as the\n  locale-less fetch always did), but no translation "
                    "exists upstream. See\n  locale_value_coverage in the log."
                )
        if s["mapping_collisions"]:
            print(
                f"\n{s['mapping_collisions']} keys had two different source columns "
                "mapped to one\nattribute — a registry defect rather than a data one. "
                "See `mapping_collision`\nin the log for which columns disagree."
            )
        if s["conflicts"]:
            print(
                f"\n{s['conflicts']} ModelColors carry different values across their own\n"
                "SKUs and were not written. The majority value is usually the\n"
                "un-updated generic one, so picking it would be worse than abstaining;\n"
                "the candidates are in the log."
            )
    return 0


def _reconciliation_warnings() -> list[str]:
    """Kinds that have not had a full pass recently enough, with how long.

    Each line carries its own threshold. The kinds reconcile on different
    cadences — catalogue every two days, liveness weekly — so one number in the
    header would be wrong for whichever kind it did not describe.
    """
    return [
        f"{kind}: {'never' if days is None else f'{days}d ago'} "
        f"(overdue past {reconcile_overdue_days(kind)}d)"
        for kind, days in reconciliation_overdue()
    ]


def _run_all(
    api: OmnitronClient, only: tuple[str, ...] | None = None, full: bool = False
) -> int:
    """Every stage, in dependency order, stopping at the first failure."""
    print(f"mode: {'full' if full else 'incremental'}\n")
    results = pipeline.run(
        api, {name: fn for name, (fn, _) in STAGES.items()}, only=only, full=full
    )
    print()
    print(f"{'stage':14} {'result':8} detail")
    for r in results:
        print(f"{r.name:14} {r.status:8} {r.detail}")
    failed = [r for r in results if r.status == "failed"]
    if failed:
        print(
            f"Stopped at {failed[0].name}. Later stages did not run — "
            "continuing past a failure is what produces a half-synced database "
            "in which every subsequent stage succeeds against incomplete input."
        )
        return 1

    # Said at the end of a *successful* run on purpose. A chain of green nights
    # is precisely the state in which nobody thinks to ask when the last full
    # reconciliation was, and departures are invisible until one happens.
    overdue = _reconciliation_warnings()
    if overdue:
        print()
        print(
            "RECONCILIATION OVERDUE — these have not had a full pass within "
            "the cadence they are held to:",
            file=sys.stderr,
        )
        for line in overdue:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nIncremental passes cannot see removals, so departures and "
            "retirements are accumulating unrecorded. Run `sync all --full`.",
            file=sys.stderr,
        )
    return 0


def _usage() -> int:
    print("usage: python -m pim_studio.sync [stage ...] [--full]\n")
    print("  all    every stage, in dependency order")
    print("  plan   print that order without running anything")
    print()
    print("  Naming no stage but passing a mode flag means `all` in that")
    print("  mode, so `--full` on its own is `all --full`.")
    print()
    print("  --full  read everything rather than only what changed since the")
    print("          last run. Required for the reconciliation sweeps — an")
    print("          incremental pass cannot tell 'removed' from 'unchanged',")
    print("          so it is never allowed to retire anything. This is what")
    print("          cron runs, nightly; it costs ~32 min over an incremental.")
    print()
    print("stages:")
    width = max((len(n) for n in STAGES), default=0)
    for name, (_, description) in sorted(STAGES.items()):
        print(f"  {name:{width}}  {description}")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Pulled out before anything else reads positionally, so `--full` may sit
    # anywhere. `sync all --full` and `sync --full all` are the same request and
    # a cron line that gets the order wrong should not silently run the other one.
    #
    # Scanned in command-line order rather than by flag, so if both appear the
    # later one wins — which is what someone appending a flag to an existing
    # command expects. Default is incremental: that is the nightly cadence, and
    # a stage with no watermark promotes itself to full anyway.
    full = False
    saw_mode_flag = False
    positional: list[str] = []
    for arg in args:
        if arg == "--full":
            full = True
            saw_mode_flag = True
        elif arg == "--incremental":
            full = False
            saw_mode_flag = True
        else:
            positional.append(arg)
    args = positional

    if args and args[0] in {"-h", "--help"}:
        return _usage()
    if not args:
        # A mode flag with no stage named means `all` in that mode. It used to
        # mean the usage text and exit 2, which is how the weekly
        # reconciliation came to never run: `pim-studio-sync.sh --full` built
        # the argv `--full` with no `all`, so every Sunday for weeks the job
        # printed help, exited non-zero, and left `last_full_run` ageing while
        # the nightly incrementals stayed green. Nothing else `--full` on its
        # own could plausibly mean, so it is honoured rather than refused.
        #
        # A bare invocation with no arguments at all still prints usage: that
        # is someone asking what this does, not asking for a four-hour sync.
        if not saw_mode_flag:
            return _usage()
        args = ["all"]

    name = args[0]
    if name == "all":
        configure_logging()
        with integrations.pim_client(real=OmnitronClient) as api:
            return _run_all(api, full=full)
    if name == "plan":
        for i, stage_name in enumerate(pipeline.plan(), start=1):
            deps = pipeline.DEPENDENCIES.get(stage_name, ())
            after = f"  (after {', '.join(deps)})" if deps else ""
            print(f"  {i}. {stage_name}{after}")
        return 0
    # Every argument is a stage, run in the order given. Previously only
    # args[0] was read and the rest were dropped without a word, so
    # `sync values liveness images` ran `values`, exited 0, and looked like it
    # had done all three - the panel then showed products with nothing live
    # and no images, which reads as a data problem rather than a missing stage.
    #
    # Sequential and literal, not dependency-resolved: `all` is the entry point
    # that knows the order. Naming stages yourself means you meant those, in
    # that order. Stop at the first failure for the same reason `all` does.
    unknown = [a for a in args if a not in STAGES]
    if unknown:
        print(f"unknown stage: {', '.join(unknown)}\n", file=sys.stderr)
        return _usage()

    configure_logging()
    try:
        with integrations.pim_client(real=OmnitronClient) as api:
            for pos, stage_name in enumerate(args):
                fn, _ = STAGES[stage_name]
                rc = fn(api, full)
                if rc != 0:
                    remaining = ", ".join(args[pos + 1 :])
                    if remaining:
                        print(
                            f"\nStopped at {stage_name}. Did not run:"
                            f" {remaining}. Continuing past a failure is what"
                            " produces a half-synced database in which every"
                            " subsequent stage succeeds against incomplete"
                            " input.",
                            file=sys.stderr,
                        )
                    return rc
        return 0
    except OmnitronError as e:
        log.error("sync_cli_failed", stage=name, error=str(e))
        print(f"\n{name}: {e}", file=sys.stderr)
        return 1
    except scope.MarketScopeViolation as e:
        log.error("sync_cli_scope_violation", stage=name, error=str(e))
        print(f"\n{name}: out of scope - {e}", file=sys.stderr)
        return 1
    except StageNotConfigured as e:
        log.error("sync_cli_not_configured", stage=name, error=str(e))
        print(f"\n{name}: {e}", file=sys.stderr)
        return 1
    except IncrementalSweepViolation as e:
        # A programming error, not a data one, and worth its own exit path so
        # the message names the fix rather than arriving as a bare traceback.
        log.error("sync_cli_sweep_violation", stage=name, error=str(e))
        print(f"\n{name}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
