"""The rules, in one place, so they survive the demo script.

Every function here implements a definition written down in the vault under
`Rules/`, which describes those definitions as the contract. They lived in
`scripts/build_demo_data.py` first, and that script says of itself that it
"makes presentational choices that the real ingest must not make" — so anything
left in it was going to be rewritten from scratch when the importer landed, and
the reconciliation test Phase 2 names as its acceptance criterion would have
had nothing to reconcile against.

They are pure functions over plain values: no database, no file paths, no
settings. That is what lets the demo generator, the importer and the tests all
call the same code, and it is why the rules are testable before the importer
exists.

Where a rule and its current call site disagree, the rule here implements the
vault, and the divergence is marked at the call site rather than smoothed over.
Two such divergences are live today and are recorded in
`Findings/Audit 2026-08-14`: unknown ban comments are collapsed to not-banned,
and English flags are raised with no confidence threshold. Both are one line at
the call site now that the rule itself abstains correctly.
"""

from .ban import BanVerdict, classify_comment
from .categories import main_category
from .coverage import (
    ISSUES,
    PRESETS,
    STOCK_CAVEAT,
    AttributeMeta,
    CoverageSummary,
    Owner,
    ProductCoverage,
    ProductFacts,
    Profile,
    Requirement,
    Standing,
    Verdict,
    evaluate_product,
    summarise,
    tally_requirements,
)
from .identifiers import model_color, normalise_blank
from .language import (
    FLAG_THRESHOLD,
    english_confidence,
    foreign_language,
    reads_as_english,
)
from .liveness import Blocker, LivenessInput, first_failure

__all__ = [
    "FLAG_THRESHOLD",
    "ISSUES",
    "PRESETS",
    "STOCK_CAVEAT",
    "AttributeMeta",
    "BanVerdict",
    "Blocker",
    "CoverageSummary",
    "LivenessInput",
    "Owner",
    "ProductCoverage",
    "ProductFacts",
    "Profile",
    "Requirement",
    "Standing",
    "Verdict",
    "classify_comment",
    "english_confidence",
    "foreign_language",
    "evaluate_product",
    "first_failure",
    "main_category",
    "model_color",
    "normalise_blank",
    "reads_as_english",
    "summarise",
    "tally_requirements",
]
