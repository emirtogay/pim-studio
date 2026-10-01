"""Running the stages in an order that is correct by construction.

Every stage here is individually safe to re-run. The *sequence* is not
optional, and that was learned the hard way: `values` was run before
`catalogue`, so it imported content for 26,813 products while the catalogue
pass then grew the set to 100,051. The result looked like a successful sync
and left three quarters of the products with no content at all — 1,715 of
18,036 ModelColors had a name, and nothing anywhere said why.

Nothing prevented it, which is the actual defect. A correct order that lives
in someone's head is not a property of the system.

The dependencies, and why each one holds:

    registry   → nothing. Attribute definitions are tenant-global.
    catalogue  → nothing. Defines *which products exist* for us.
    products   → catalogue. Refreshes identity for the set catalogue found;
                 running it first refreshes a set that is about to change.
    values     → catalogue (the product set) and registry (what the attribute
                 keys mean). Both, or it writes a partial matrix.
    liveness   → catalogue. Price and stock are keyed on products we hold.
    images     → catalogue. Same reason.

`--check-only` prints the plan without calling anything, which is the cheap
way to confirm the order before a long run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..logging import log
from ..omnitron.client import OmnitronClient

#: name -> the stages that must have run before it, in this same invocation.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "registry": (),
    "catalogue": (),
    "products": ("catalogue",),
    "values": ("catalogue", "registry"),
    # Needs the curated registry's column mappings to know which attributes
    # are dropdown-backed, and Omnitron's attribute pks to fetch each
    # vocabulary. Independent of any product data.
    "labels": ("registry",),
    "liveness": ("catalogue",),
    "images": ("catalogue",),
    # Depends on nothing. It reads the public shop rather than Omnitron, which
    # is the whole reason it is worth having: a stage that shared its inputs
    # with the others would fail the same way they do.
    "storefront": (),
}

#: The order a full sync runs in. Derived by hand rather than topologically
#: sorted because the tie-breaks are judgement, not graph theory: `registry`
#: leads because it is quick and a failure there means the rest is pointless,
#: and `images` trails because it is the longest and least urgent.
# `labels` sits next to `registry`: both are vocabulary-shaped, both are
# quick, and neither depends on the product set.
#
# `storefront` leads. It is three minutes, it depends on nothing, and it
# produces the number people actually look at — so a night that fails at
# `values` still leaves the headline current. It is also the one stage whose
# failure means something specific rather than "Omnitron was unwell": the
# storefronts are up or the shops are down.
FULL_ORDER = (
    "storefront", "registry", "labels", "catalogue", "products", "values",
    "liveness", "images",
    # Reads what `values` has just written and offers a replacement wherever a
    # market field holds English or Turkish. In the nightly pass because
    # nothing about it is a publication: it writes to the `proposal` lane,
    # which no push reads and no shop sees, and a person still has to accept
    # each one. The push is excluded from `all` because it changes the live
    # catalogue; this does not.
    #
    # THE DAY A PAID TRANSLATOR IS CONFIGURED, revisit this line. A source that
    # costs money per item running unattended every night is a different
    # decision from a free one, and the only thing standing between them today
    # is that `MachineSource` has no translator.
    "proposals",
    # Last, and cheap: it reads the shop only for markets with values waiting,
    # so on a day nobody pushed it is one query. It runs after `values` because
    # a value the nightly import has just re-read is the one it asks about.
    "publication",
)


@dataclass
class StageResult:
    name: str
    status: str  # ok | failed | skipped
    detail: str = ""


def plan(only: tuple[str, ...] | None = None) -> list[str]:
    """Return the stages to run, in dependency order.

    Asking for a stage pulls in what it depends on. Requesting `values` alone
    is almost always a mistake — it is the request that caused the incident
    this module exists to prevent — so the dependency is added rather than
    assumed satisfied.
    """
    wanted = list(only or FULL_ORDER)
    resolved: list[str] = []

    def visit(name: str, seen: tuple[str, ...] = ()) -> None:
        if name in resolved:
            return
        if name in seen:
            raise ValueError(f"circular dependency at {name}: {' -> '.join(seen)}")
        for dep in DEPENDENCIES.get(name, ()):
            visit(dep, (*seen, name))
        resolved.append(name)

    for name in wanted:
        visit(name)
    return [s for s in FULL_ORDER if s in resolved]


def run(
    api: OmnitronClient,
    stages: dict[str, Callable[[OmnitronClient, bool], int]],
    only: tuple[str, ...] | None = None,
    stop_on_failure: bool = True,
    full: bool = False,
) -> list[StageResult]:
    """Execute the plan, stopping at the first failure by default.

    Continuing past a failure is what produces the half-synced state that is
    hardest to diagnose: every later stage succeeds against incomplete input
    and reports success. `--keep-going` exists for the case where someone is
    deliberately collecting every error at once.

    `full` is passed to every stage rather than decided per stage. A night that
    reconciles the catalogue but reads prices incrementally is a coherent thing
    to want and an incoherent thing to reason about later — "was 14 August a
    full sync?" needs one answer, not seven.
    """
    order = plan(only)
    results: list[StageResult] = []
    log.info("pipeline_start", stages=order, mode="full" if full else "incremental")

    for name in order:
        fn = stages.get(name)
        if fn is None:
            results.append(StageResult(name, "skipped", "no such stage"))
            continue
        try:
            code = fn(api, full)
        except Exception as exc:  # noqa: BLE001 — recorded, then re-raised or not
            results.append(StageResult(name, "failed", f"{type(exc).__name__}: {exc}"))
            log.error("pipeline_stage_failed", stage=name, error=str(exc))
            if stop_on_failure:
                break
            continue
        if code != 0:
            results.append(StageResult(name, "failed", f"exit {code}"))
            if stop_on_failure:
                break
            continue
        results.append(StageResult(name, "ok"))

    log.info(
        "pipeline_finished",
        ok=[r.name for r in results if r.status == "ok"],
        failed=[r.name for r in results if r.status == "failed"],
    )
    return results
