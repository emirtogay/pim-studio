"""Mirroring Omnitron's attribute registry.

Four resources, all small enough to read in full every time:

    attributes        149 rows      2 requests
    attribute_value   20,382 rows   ~204 requests
    attribute_set     25 rows       1 request
    attribute_config  795 rows      8 requests

**Full reads, not deltas**, and that is a deliberate choice rather than an
oversight. `modified_date__gte` works here — it is verified — and using it
would cut the vocabulary read to a handful of requests. But a delta sync
cannot see a deletion: an attribute value removed upstream simply stops
appearing, which is indistinguishable from one that did not change. For a
mirror whose entire job is to say what Omnitron currently has, that is the
wrong trade at this size. Roughly 215 requests a night buys deletion detection
for free. The watermark stays available in `runs.last_successful_run` for the
product sync, where the volumes actually justify it.

Pruning is guarded. A read that fails half way through looks exactly like a
catalogue that shrank, and pruning on that would delete most of the registry
and report success. So rows are only removed after a clean full read, and only
if the shrinkage is small enough to be plausible.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..logging import log
from ..models import (
    Base,
    OmnitronAttribute,
    OmnitronAttributeConfig,
    OmnitronAttributeSet,
    OmnitronAttributeValue,
    utcnow,
)
from ..omnitron.client import OmnitronClient
from .runs import SyncRun

#: Refuse to prune if a full read came back missing more than this share of
#: what we already hold. A genuine registry does not lose a fifth of itself
#: overnight; a truncated read looks exactly like one that did.
_MAX_PRUNE_FRACTION = 0.20

#: Rows per write transaction. Large enough that 20,382 values is a handful of
#: commits, small enough that a failure does not roll back the whole read.
_WRITE_CHUNK = 1000


def _parse_dt(value: Any) -> datetime | None:
    """Omnitron sends `2026-07-01T06:34:45.800235Z`."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        log.warning("unparseable_datetime", value=str(value)[:64])
        return None


def _columns(model: type[Base]) -> dict[str, Any]:
    return {c.name: c for c in model.__table__.columns}


def _as_utc(value: Any) -> Any:
    """Make a datetime comparable regardless of which dialect produced it.

    `DateTime(timezone=True)` does not round-trip awareness on SQLite: the
    dialect stores a string and hands back a *naive* datetime, while the value
    parsed from Omnitron carries UTC. The two describe the same instant and
    compare unequal, so a straight `!=` reports every dated row as changed on
    every run — which is exactly what the change detection above exists to
    avoid, and it fails silently by over-reporting rather than under-reporting.

    PostgreSQL does return aware datetimes, so the comparison has to cope with
    both. Everything we store is UTC, so treating a naive value as UTC is
    sound rather than a guess.
    """
    if not isinstance(value, datetime):
        return value
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) or isinstance(b, datetime):
        return _as_utc(a) == _as_utc(b)
    return bool(a == b)


def _to_row(model: type[Base], record: dict[str, Any]) -> dict[str, Any]:
    """Project an API record onto a mirror model's columns.

    Generic rather than four hand-written mappers, because the field names
    match one-for-one and a hand mapper would be four places to forget a new
    column. Anything Omnitron sends that we have no column for is dropped
    silently — the mirror tracks what we chose to keep, not everything that
    exists.
    """
    cols = _columns(model)
    row: dict[str, Any] = {}
    for name, column in cols.items():
        if name == "synced_at":
            continue
        if name not in record:
            continue
        value = record[name]
        kind = column.type.__class__.__name__
        if kind == "DateTime":
            value = _parse_dt(value)
        elif isinstance(value, (list, dict)):
            # `default_fields` is a JSON list upstream. Stored as its raw JSON
            # rather than normalised: nothing reads it yet, and inventing a
            # shape for an unused field is guessing at its meaning.
            value = json.dumps(value, ensure_ascii=False)
        elif kind == "Boolean" and value is None:
            value = False
        row[name] = value
    row["synced_at"] = utcnow()
    return row


def _write(db: Session, model: type[Base], rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Upsert by primary key, skipping rows that have not changed.

    Returns (inserted, updated, unchanged).

    The comparison deliberately excludes `synced_at`, which differs on every
    run by construction. Including it would mark all 20,382 vocabulary rows
    dirty every night and make the run's `updated` count a constant — when the
    single most useful thing this mirror can tell us is "three vocabulary
    values changed today". The cost is reading the rows rather than just their
    ids; the benefit is that the number means something.

    `synced_at` therefore records when a row last *changed*, not when it was
    last confirmed. Same trade, and same reasoning, as `writer.py`.
    """
    if not rows:
        return 0, 0, 0

    compared = [c for c in _columns(model) if c != "synced_at"]
    pks = [r["pk"] for r in rows]
    existing = {
        row.pk: row  # type: ignore[attr-defined]
        for row in db.scalars(
            select(model).where(model.pk.in_(pks))  # type: ignore[attr-defined]
        ).all()
    }

    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []
    unchanged = 0

    for row in rows:
        current = existing.get(row["pk"])
        if current is None:
            to_insert.append(row)
        elif any(not _same(getattr(current, c), row.get(c)) for c in compared):
            to_update.append(row)
        else:
            unchanged += 1

    if to_insert:
        db.bulk_insert_mappings(model, to_insert)  # type: ignore[arg-type]
    if to_update:
        db.bulk_update_mappings(model, to_update)  # type: ignore[arg-type]
    return len(to_insert), len(to_update), unchanged


def _prune(db: Session, model: type[Base], seen: set[int], run: SyncRun) -> int:
    """Remove rows Omnitron no longer has, if that is a believable claim."""
    held = db.scalar(select(func.count()).select_from(model)) or 0
    if not held or not seen:
        return 0
    missing = set(db.scalars(select(model.pk).where(model.pk.notin_(seen))).all())  # type: ignore[attr-defined]
    if not missing:
        return 0

    fraction = len(missing) / held
    if fraction > _MAX_PRUNE_FRACTION:
        # Loud, and does not delete. A truncated read and a genuine mass
        # deletion are indistinguishable from here, and only one of them is
        # safe to act on.
        log.error(
            "prune_refused",
            table=model.__tablename__,  # type: ignore[attr-defined]
            held=held,
            missing=len(missing),
            fraction=round(fraction, 3),
        )
        run.dropped(
            len(missing),
            reason=f"prune refused on {model.__tablename__}: "  # type: ignore[attr-defined]
            f"{len(missing)}/{held} rows absent, above the "
            f"{_MAX_PRUNE_FRACTION:.0%} guard",
        )
        return 0

    db.execute(delete(model).where(model.pk.in_(missing)))  # type: ignore[attr-defined]
    log.info(
        "pruned",
        table=model.__tablename__,  # type: ignore[attr-defined]
        removed=len(missing),
    )
    return len(missing)


def _mirror(
    api: OmnitronClient,
    run: SyncRun,
    model: type[Base],
    path: str,
    prune: bool = True,
) -> dict[str, int]:
    """Read a resource in full and reconcile the mirror against it."""
    inserted = updated = unchanged = 0
    seen: set[int] = set()
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal inserted, updated, unchanged
        if not buffer:
            return
        with session_scope() as db:
            i, u, s = _write(db, model, buffer)
        inserted += i
        updated += u
        unchanged += s
        buffer.clear()

    for record in api.iter_records(path, page_size=100):
        run.seen()
        pk = record.get("pk", record.get("id"))
        if pk is None:
            run.dropped(reason=f"{path}: record with no pk")
            continue
        record = {**record, "pk": pk}
        seen.add(int(pk))
        buffer.append(_to_row(model, record))
        if len(buffer) >= _WRITE_CHUNK:
            flush()
    flush()

    removed = 0
    if prune:
        with session_scope() as db:
            removed = _prune(db, model, seen, run)

    run.imported(inserted + updated)
    stats = {
        "read": len(seen),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
    }
    log.info("mirrored", table=model.__tablename__, **stats)  # type: ignore[attr-defined]
    return stats


# ── the stages ─────────────────────────────────────────────────────────────


def sync_attributes(api: OmnitronClient, run: SyncRun) -> dict[str, int]:
    """The 149 attribute definitions."""
    return _mirror(api, run, OmnitronAttribute, "/api/v1/attributes/")


def sync_attribute_values(api: OmnitronClient, run: SyncRun) -> dict[str, int]:
    """The 20,382 vocabulary options."""
    return _mirror(api, run, OmnitronAttributeValue, "/api/v1/attribute_value/")


def sync_attribute_sets(api: OmnitronClient, run: SyncRun) -> dict[str, int]:
    """The 25 attribute sets.

    The set endpoint embeds each set's configs under `attributeconfig_set`, but
    they are read from `/api/v1/attribute_config/` instead — the standalone
    endpoint is paginated properly, and taking configs from a nested list would
    make the config count depend on how many sets came back.
    """
    return _mirror(api, run, OmnitronAttributeSet, "/api/v1/attribute_set/")


def sync_attribute_configs(api: OmnitronClient, run: SyncRun) -> dict[str, int]:
    """The 795 set/attribute join rows carrying per-set overrides."""
    return _mirror(api, run, OmnitronAttributeConfig, "/api/v1/attribute_config/")
