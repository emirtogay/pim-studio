"""Inbound sync from Omnitron.

Every stage in this package pulls from the live API and writes into our own
tables. Nothing here writes *to* Omnitron — the client cannot
(`omnitron.client.ReadOnlyViolation`), and the write path back into the
catalogue stays a generated file that a human uploads
(`05 - Decisions Log`, 2026-08-14).

A stage is a function that takes an open `OmnitronClient` and a `SyncRun`, and
reports what it saw. The `SyncRun` handles the bookkeeping so no stage has to
remember to write `sync_runs` — see `runs.py` for why that row is committed
before any work starts.
"""

from __future__ import annotations

from .batching import SKU_BATCH_SIZE, SkuFetch, chunked, fetch_by_sku
from .runs import (
    MODE_FULL,
    MODE_INCREMENTAL,
    IncrementalSweepViolation,
    SyncMode,
    SyncRun,
    last_full_run,
    last_successful_run,
    sync_run,
)
from .writer import Divergence, UpsertResult, upsert_attribute_values

__all__ = [
    "MODE_FULL",
    "MODE_INCREMENTAL",
    "SKU_BATCH_SIZE",
    "Divergence",
    "IncrementalSweepViolation",
    "SkuFetch",
    "SyncMode",
    "SyncRun",
    "UpsertResult",
    "chunked",
    "fetch_by_sku",
    "last_full_run",
    "last_successful_run",
    "sync_run",
    "upsert_attribute_values",
]
