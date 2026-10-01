"""CLI for the reports — `python -m pim_studio.reports <name>`.

Reads only, from tables we already hold. No network, so these run against
whatever the last sync left behind and can be repeated freely.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable

from .. import integrations
from ..logging import configure_logging
from ..rules.coverage import PRESETS, PRESETS_BY_KEY, SHARED, Owner
from .coverage import Scope, coverage_report
from .integrity import check_integrity
from .overview import ALL_SLICE, OverviewReport, overview_report
from .registry import reconcile_registry
from .vocabulary import audit_vocabulary

REPORTS: dict[str, tuple[Callable[..., int], str]] = {}


def report(name: str, description: str) -> Callable[..., Callable[..., int]]:
    def register(fn: Callable[..., int]) -> Callable[..., int]:
        REPORTS[name] = (fn, description)
        return fn

    return register


@report("registry", "our curated attributes against Omnitron's")
def _registry() -> int:
    r = reconcile_registry()
    c = r.counts

    print("REGISTRY RECONCILIATION\n")
    print(f"  mapped to an Omnitron attribute   {c['mapped']}")
    print(f"  ours with no upstream match       {c['unmapped_ours']}")
    print(f"  theirs we do not map              {c['unmapped_theirs']}")
    print(f"  columns that are not attributes   {c['non_attribute_columns']}")
    print(f"  columns unexplained               {c['unexplained_columns']}")
    print(f"  read in one language only         {c['single_language_columns']}")
    print(f"  theirs, unmapped and localizable  {c['unmapped_localizable']}")

    if r.single_language_columns:
        print("\n── localizable, but read in one language ──────────────────────────")
        print("These attributes hold a different value per language upstream, and")
        print("Omnitron serves one language per request. The values sync routes a")
        print("column through its market's locale chain — so a column with no")
        print("market, or a market with no chain, is read in the tenant default")
        print("and the other languages are never seen. Filled, and wrong.")
        print(f"\n{'column':46} {'ours':18} market")
        for column, our_key, market in sorted(r.single_language_columns):
            print(f"  {column:44} {our_key:18} {market or '— (agnostic)'}")

    if r.unmapped_localizable:
        print("\n── theirs, unmapped and localizable ───────────────────────────────")
        print("Per-language content nothing on our side reads at all.")
        for key, data_type in sorted(r.unmapped_localizable):
            print(f"  {key:38} {data_type}")

    if r.filter_attributes:
        unmapped = c["filter_attributes_unmapped"]
        print("\n── the seven filter attributes ────────────────────────────────────")
        print(f"{'omnitron key':26} {'meaning':10} {'upstream':9} {'values':>7}  mapped by us")
        for key, gloss, present, values, mapped in r.filter_attributes:
            print(
                f"{key:26} {gloss:10} {'yes' if present else 'NO':9} {values:>7}  "
                f"{'yes' if mapped else 'NO'}"
            )
        if unmapped:
            print(
                f"\n{unmapped} of 7 are present upstream with real vocabularies and have no\n"
                "column mapping on our side, so nothing reads them today. Wiring those\n"
                "mappings is the registry correction the build log lists as next step 2."
            )

    if r.mapped:
        print("\n── mapped ─────────────────────────────────────────────────────────")
        print(
            f"{'ours':20} {'omnitron key':30} {'type':10} {'loc':4} "
            f"{'sets':>5} {'vocab ours/theirs':>18}  markets"
        )
        for m in sorted(r.mapped, key=lambda x: x.our_key):
            loc = "yes" if m.is_localizable else "—"
            vocab = f"{m.vocabulary_ours}/{m.vocabulary_theirs}"
            markets = ",".join(m.markets) or "—"
            print(
                f"{m.our_key:20} {m.omnitron_key:30} {m.data_type:10} {loc:4} "
                f"{m.set_count:>5} {vocab:>18}  {markets}"
            )

    if r.unmapped_ours:
        print("\n── ours, with no Omnitron attribute behind them ───────────────────")
        print("(expected for values sourced from masterdata or computed by us)")
        for key, channel in r.unmapped_ours:
            print(f"  {key:24} write_channel={channel}")

    if r.non_attribute_columns:
        print("\n── columns that correctly resolve to no attribute ─────────────────")
        for name, why in sorted(r.non_attribute_columns):
            print(f"  {name:46} {why}")

    if r.unexplained_columns:
        print("\n── UNEXPLAINED: mapped to a column that resolves to nothing ───────")
        print("These are real gaps — either the key is wrong or the attribute is gone.")
        for name in sorted(r.unexplained_columns):
            print(f"  {name}")

    if r.unmapped_theirs:
        print(f"\n── theirs, unmapped ({len(r.unmapped_theirs)}) — richest vocabulary first ──")
        print("Not gaps by default: most belong to product families or channels we")
        print("do not manage. Which ones matter needs product data we have not")
        print("imported yet — attribute-set membership is the closest signal we have.")
        print(f"\n{'key':38} {'type':10} {'values':>7}")
        for key, data_type, values in r.unmapped_theirs:
            print(f"  {key:36} {data_type:10} {values:>7}")

    return 0


@report("vocabulary", "is the filter vocabulary fit to show a customer?")
def _vocabulary() -> int:
    r = audit_vocabulary()
    t = r.totals

    print("VOCABULARY COVERAGE — the seven filter attributes\n")
    if r.missing_attributes:
        print(f"  not in the mirror: {', '.join(r.missing_attributes)}")
        print("  (run `python -m pim_studio.sync registry` first)\n")

    print(f"{'attribute':26} {'meaning':10} {'values':>7} {'labels':>7} {'redundant':>10}")
    for s in r.summaries:
        print(f"{s.key:26} {s.gloss:10} {s.values:>7} {s.distinct_labels:>7} {s.redundant:>10}")
    print(
        f"{'TOTAL':26} {'':10} {t['values']:>7} {t['distinct_labels']:>7} {t['redundant']:>10}"
    )

    if r.redundant:
        print("\n── values that render as the same label ───────────────────────────")
        print("Two ways to pick the same thing. Merging them means repointing every")
        print("product that uses the loser, and an attribute value in use cannot be")
        print("edited — so this is create-new-then-remap, and it is not ours to do.")
        for dup in r.redundant:
            print(f"  {dup.attribute:24} {dup.label!r:24} <- {', '.join(dup.values)}")

    if r.contamination:
        print("\n── labels appearing under more than one attribute ─────────────────")
        print("Some are honest (denim really is both a pattern and a fabric); others")
        print("are a value filed under the wrong attribute.")
        for clash in r.contamination:
            print(f"  {clash.label!r:24} {', '.join(clash.attributes)}")

    if r.defective:
        print("\n── present, non-null and meaningless ──────────────────────────────")
        print("A not-null check counts every one of these as filled.")
        for bad in r.defective:
            print(
                f"  {bad.attribute:24} value={bad.value!r:16} "
                f"label={bad.label!r:16} {bad.reason}"
            )

    print("\n── per-market fill ────────────────────────────────────────────────")
    print("Not available: product attribute values are not imported yet, so how")
    print("many products in each market carry a value cannot be counted. A number")
    print("here would be a guess, and a guess here would be acted on.")

    if t["redundant"] or t["contaminated_labels"] or t["defective_values"]:
        print(
            f"\nAs a filter today: {t['values']} options across {t['attributes']} attributes, "
            f"of which {t['redundant']} are duplicates,\n{t['contaminated_labels']} labels sit "
            f"under more than one attribute and {t['defective_values']} are meaningless. "
            "Filling\ngaps on this adds options to a filter that already needs pruning."
        )
    return 0


@report("integrity", "does what we hold match what Omnitron has?")
def _integrity() -> int:
    """The only report that calls the API — a self-consistency check that never
    leaves the database cannot detect the failure it exists for."""
    with integrations.pim_client() as api:
        rep = check_integrity(api)

    print("COUNTS — ours against Omnitron's")
    print()
    print(f"{'resource':30} {'ours':>9} {'theirs':>9} {'drift':>8}")
    for c in rep.counts:
        drift = "?" if c.drift is None else f"{c.drift:+d}"
        mark = "" if c.ok else "   <-"
        theirs = "?" if c.theirs is None else str(c.theirs)
        print(f"{c.name:30} {c.ours:>9} {theirs:>9} {drift:>8}{mark}")

    if rep.orphans:
        print("ORPHANS — rows pointing at something that is not there")
        for what, n in rep.orphans:
            print(f"  {n:>8}  {what}")

    if rep.unfinished:
        print("RUNS THAT NEVER CLOSED")
        for run_id, kind, status in rep.unfinished:
            print(f"  run {run_id:<4} {kind:22} {status}")

    if rep.stale:
        print("STALE — not an error, but a reason a number can be wrong")
        for kind, why in rep.stale:
            print(f"  {kind:24} {why}")

    print("clean" if rep.clean else "not clean — see above")
    return 0 if rep.clean else 1


@report("coverage", "what is missing, on how many options, and whose it is")
def _coverage(args: list[str] | None = None) -> int:
    """`python -m pim_studio.reports coverage [profile] [--catalogue PL]`.

    Prints the same four numbers the panel shows, from the same code. That is
    the point of it being here rather than in the API: a number that can only
    be produced by loading a web page cannot be checked against a spreadsheet,
    and every coverage figure this team has argued about was argued about
    because two people could not both reproduce it.
    """
    rest = list(args or [])
    profile_key = "sellable"
    if rest and not rest[0].startswith("-"):
        profile_key = rest.pop(0)
    if profile_key not in PRESETS_BY_KEY:
        print(f"unknown profile: {profile_key}\n", file=sys.stderr)
        print("profiles:")
        for preset in PRESETS:
            print(f"  {preset.key:14} {preset.label} — {preset.blurb}")
        return 2

    catalogue = ""
    if "--catalogue" in rest:
        index = rest.index("--catalogue")
        catalogue = rest[index + 1] if index + 1 < len(rest) else ""

    r = coverage_report(profile_key, Scope(in_catalogue=catalogue))
    s = r.summary

    print(f"COVERAGE — {r.profile.label}")
    print(f"{r.profile.blurb}\n")
    print(f"  scope                 {r.scope.describe()}")
    print(f"  options               {r.population:,}")
    print(f"  complete              {s.complete:,}")
    print(f"  incomplete            {s.incomplete:,}")
    print(f"  indeterminate         {s.indeterminate:,}   (no source for a requirement)")
    if s.completion_rate is not None:
        print(f"  complete, of judged   {s.completion_rate:.1%}")

    print("\n── issues ─────────────────────────────────────────────────────────")
    print(f"{'issue':26} {'options':>8} {'share':>7}")
    for issue in r.issues:
        share = issue.share(r.population)
        mark = "  <- provisional" if issue.stock_backed else ""
        pct = "—" if share is None else f"{share:.1%}"
        print(f"{issue.label:26} {issue.count:>8,} {pct:>7}{mark}")

    print("\n── what is missing ────────────────────────────────────────────────")
    print("Fill rate is met / (met + missing). `unknown` is in neither half —")
    print("a column nobody has read must not read as 0%.")
    print(
        f"\n{'attribute':24} {'mkt':4} {'met':>7} {'missing':>8} {'unknown':>8} "
        f"{'n/a':>6} {'fill':>7}  owner"
    )
    for tally in r.requirements:
        market = "ALL" if tally.market == SHARED else tally.market
        if tally.blind:
            fill = "no match"
        elif tally.fill_rate is None:
            fill = "never read"
        else:
            fill = f"{tally.fill_rate:.1%}"
        print(
            f"{tally.label[:24]:24} {market:4} {tally.met:>7,} {tally.missing:>8,} "
            f"{tally.unknown:>8,} {tally.na:>6,} {fill:>7}  {tally.owner.value}"
        )

    print("\n── where the work lands ───────────────────────────────────────────")
    print(f"{'owner':10} {'gaps':>8} {'options':>8}  what to do")
    for owner in Owner:
        print(
            f"{owner.value:10} {s.gaps_by_owner.get(owner, 0):>8,} "
            f"{s.products_by_owner.get(owner, 0):>8,}  "
            f"{REMEDY_LINE[owner]}"
        )

    for caveat in r.caveats:
        print(f"\n⚠ {caveat}")
    return 0


REMEDY_LINE = {
    Owner.US: "goes out on the next Omnitron import file",
    Owner.PARTNER: "request on the next product-card file to Turkey",
    Owner.SYSTEM: "arrives from a feed; nothing to upload",
}


@report("funnel", "how far each option got, and where the rest stopped")
def _funnel(args: list[str] | None = None) -> int:
    """`python -m pim_studio.reports funnel [season]`.

    The same numbers the Overview page draws, from the same code — and for the
    same reason coverage has a terminal report: a figure that can only be
    produced by loading a web page cannot be checked against a spreadsheet.

    Read-only. The snapshot the deltas compare against is written by the
    nightly build, never by this.
    """
    rest = list(args or [])
    season = rest.pop(0) if rest and not rest[0].startswith("-") else ""

    r = overview_report(season)
    x = r.extract
    if season and season not in x["slices"]:
        print(f"unknown season: {season}\n", file=sys.stderr)
        print("seasons:")
        for entry in x["seasons"]:
            print(f"  {entry['key']:14} {entry['count']:>7,}")
        return 2

    s = r.slice
    stages = {stage["key"]: stage for stage in x["stages"]}
    markets = x["markets"]

    print("GO-LIVE FUNNEL")
    print("How far each option got, and whose queue the rest sit in.\n")
    print(f"  scope                 {season or 'every season'}")
    print(f"  options               {s['population']:,}")
    print(f"  live on the site      {s['headline']['live']:,}")
    print(f"  blocked everywhere    {s['headline']['blocked']:,}")
    print(f"  waiting on Turkey     {s['headline']['waitingOnPartner']:,}")

    if r.deltas and season_key(r) in r.deltas:
        d = r.deltas[season_key(r)]
        print(
            f"\n  since {d['baselineDate']} ({d['ageDays']}d): "
            f"{_signed(d['live'])} live, {_signed(d['blocked'])} blocked"
        )
    else:
        print("\n  no baseline yet — deltas begin once a week of nights has run")

    print("\n── the funnel ─────────────────────────────────────────────────────")
    print("Cumulative: an option counts at a stage only if every earlier one")
    print("passed. `indet` is a stage no source could answer — never a gap.\n")
    header = f"{'stage':26} {'scope':6} " + " ".join(f"{m:>7}" for m in markets)
    print(header)
    for stage in x["stages"]:
        if not stage["computable"]:
            print(f"{stage['label'][:26]:26} {'—':6} " + "no source ingested")
            continue
        counts = s["counts"][stage["key"]]
        if stage["perMarket"]:
            cells = [counts["markets"][m] for m in markets]
        else:
            cells = [counts["shared"]] * len(markets)
        line = f"{stage['label'][:26]:26} {'per mkt' if stage['perMarket'] else 'all':6} "
        line += " ".join(f"{c['passed']:>7,}" for c in cells)
        indet = sum(c["indeterminate"] for c in cells)
        print(line + (f"   ({indet:,} indet)" if indet else ""))

    print("\n── where they stopped ─────────────────────────────────────────────")
    print(f"{'drop':30} {'options':>8}  reason and owner")
    for drop in s["drops"]:
        stage = stages[drop["to"]]
        sides = (
            [("", drop["shared"])]
            if "shared" in drop
            else [(m, drop["markets"][m]) for m in markets]
        )
        for market, side in sides:
            if not side["count"]:
                continue
            label = f"{drop['from']} -> {drop['to']}" + (f" ({market})" if market else "")
            print(f"{label[:30]:30} {side['count']:>8,}")
            for reason in side["reasons"]:
                print(
                    f"{'':30} {reason['count']:>8,}  {reason['label']} "
                    f"[{reason['owner']}]"
                )

    if s["liveDespiteGaps"]:
        print(
            f"\n{s['liveDespiteGaps']:,} options are live on the site with an "
            "earlier stage still incomplete."
        )
        print("The site enforces the liveness rules, not our content pipeline.")

    if s["defects"]:
        print("\n── wrong, not missing ─────────────────────────────────────────────")
        print("Filled and defective. Different work, different person — never")
        print("part of the funnel.\n")
        for defect in s["defects"]:
            print(f"{defect['label'][:40]:40} {defect['count']:>8,}")
        if x["conflicts"]["productName"]:
            print(
                f"{'name differs between sizes':40} "
                f"{x['conflicts']['productName']:>8,}"
            )

    for caveat in x["caveats"]:
        print(f"\n⚠ {caveat['text']}")
    return 0


def _signed(n: int) -> str:
    return f"+{n:,}" if n > 0 else f"{n:,}"


def season_key(r: OverviewReport) -> str:
    return r.season or ALL_SLICE


def _usage() -> int:
    print("usage: python -m pim_studio.reports <name>\n")
    print("reports:")
    width = max((len(n) for n in REPORTS), default=0)
    for name, (_, description) in sorted(REPORTS.items()):
        print(f"  {name:{width}}  {description}")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        return _usage()
    name = args[0]
    if name not in REPORTS:
        print(f"unknown report: {name}\n", file=sys.stderr)
        return _usage()
    configure_logging()
    fn, _ = REPORTS[name]
    # Most reports take no arguments; coverage takes a profile and flags. The
    # signature decides, rather than a try/except TypeError — that would
    # swallow a genuine TypeError raised *inside* a report and then run it a
    # second time with different arguments.
    if inspect.signature(fn).parameters:
        return fn(args[1:])
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
