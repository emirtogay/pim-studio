"""Reports read; they never write.

Everything in this package answers a question from tables we already hold —
our curated registry, the Omnitron mirror, the product table. Nothing here
calls the API and nothing here mutates a row, so a report can be run at any
time, repeatedly, without consequence.

That constraint is the point. A report that quietly fixed what it found would
make the numbers it prints a moving target, and the whole reason to look is to
decide what to change.
"""

from __future__ import annotations

from .coverage import (
    Catalogue,
    CoverageReport,
    Scope,
    build_report,
    coverage_report,
    from_extract,
    load_catalogue,
    to_extract,
)
from .registry import ReconciliationReport, reconcile_registry
from .values import (
    TooManyValues,
    ValueBlock,
    ValueColumn,
    ValueSelection,
    load_values,
)
from .workbook import build_workbook, filename_for

__all__ = [
    "Catalogue",
    "CoverageReport",
    "ReconciliationReport",
    "Scope",
    "TooManyValues",
    "ValueBlock",
    "ValueColumn",
    "ValueSelection",
    "build_report",
    "build_workbook",
    "coverage_report",
    "filename_for",
    "from_extract",
    "load_catalogue",
    "load_values",
    "reconcile_registry",
    "to_extract",
]
