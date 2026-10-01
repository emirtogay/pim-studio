"""Writing attribute values, idempotently.

This is the only place the sync touches `attribute_values`, which has held zero
rows since the schema was built. Three decisions are encoded here and each one
is the kind that is cheap now and expensive to retrofit.

**An unchanged value is not rewritten.** A sync that touches every row every
night makes `updated_at` mean "when we last looked" instead of "when this
changed", and Phase 9's "what changed this week" is then unanswerable from the
data. So each batch is read before it is written and only genuine differences
are persisted. The cost is one SELECT per batch; the alternative is a column
that quietly stops meaning anything.

**Provenance is not a change.** `source_ref` names the run that observed a
value, so it differs on every sync by construction. Counting it as a change
would mark every row dirty every night and defeat the paragraph above. It is
therefore excluded from the comparison — with the consequence that
`source_ref` records the run that last *changed* a value, not the run that last
confirmed it. If "when did we last see this confirmed" is needed, that is a
`last_seen_at` column and a migration, deliberately not smuggled in here.

**Values we own are not overwritten.** A row in the `current` lane sitting at
`approved` or `exported` is work in flight: a human accepted it, or it went out
in a batch and we are waiting to see it land. Omnitron reporting something
different does not make our value wrong — that is precisely the `stale`
condition the lifecycle was designed around. So those rows are protected, and
the divergence is returned to the caller rather than silently resolved in
Omnitron's favour. The mirror image of this rule already exists as a decision:
masterdata never silently overwrites Omnitron either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from ..models import MARKET_AGNOSTIC, AttributeValue, ValueChange, utcnow

#: The columns that identify a value. Matches the table's unique constraint
#: `uq_value_entity_attr_market_lane` exactly — if that constraint changes,
#: this must change with it.
KEY_FIELDS = ("entity_kind", "entity_key", "attribute_id", "market", "state_lane")

#: Fields whose difference means the underlying fact changed. `source_ref` and
#: `updated_by` are deliberately absent — see the module docstring.
MATERIAL_FIELDS = (
    "value",
    "state",
    "entry_grain",
    "source_kind",
    "suspect_flag",
    "suspect_reason",
)

#: Lifecycle states in the `current` lane that a sync must not overwrite.
#: `approved` is a human's decision not yet published; `exported` went out in a
#: batch and is awaiting confirmation. Overwriting either loses the fact that we
#: were mid-flight.
PROTECTED_STATES = frozenset({"approved", "exported"})

#: Records per existence query. The lookup binds one parameter per key column,
#: so this is 2,500 bind variables — comfortably under SQLite's
#: SQLITE_LIMIT_VARIABLE_NUMBER (32,766 on a current build, but 999 on older
#: ones) and PostgreSQL's 65,535. A realistic batch is 100 SKUs times ~40
#: attributes, so without this the query would be 20,000 binds and would fail
#: on exactly the deployment we cannot test locally.
_LOOKUP_CHUNK = 500


@dataclass
class Divergence:
    """Omnitron disagrees with a value we own. Reported, never resolved here."""

    entity_kind: str
    entity_key: str
    attribute_id: int
    market: str
    our_state: str
    our_value: str | None
    their_value: str | None

    def __str__(self) -> str:
        return (
            f"{self.entity_kind}:{self.entity_key} attr={self.attribute_id} "
            f"market={self.market} ours({self.our_state})={self.our_value!r} "
            f"theirs={self.their_value!r}"
        )


@dataclass
class UpsertResult:
    """Exactly what a write did, so a run can report it honestly."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    protected: list[Divergence] = field(default_factory=list)

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def merge(self, other: UpsertResult) -> UpsertResult:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.protected.extend(other.protected)
        return self

    def __repr__(self) -> str:
        return (
            f"<UpsertResult inserted={self.inserted} updated={self.updated} "
            f"unchanged={self.unchanged} protected={len(self.protected)}>"
        )


def _key_of(record: dict[str, Any]) -> tuple:
    return tuple(record[f] for f in KEY_FIELDS)


def normalise(record: dict[str, Any]) -> dict[str, Any]:
    """Fill the defaults a caller should not have to remember.

    Chiefly the market sentinel: a market-agnostic value stores `'*'`, never
    NULL, because SQL treats NULLs as distinct and a nullable market would
    leave the one-row-per-key constraint unenforced for most of the registry.
    Callers naturally produce `None` for "no market", so it is converted here
    rather than at 20 call sites.
    """
    out = dict(record)
    if out.get("market") is None:
        out["market"] = MARKET_AGNOSTIC
    out.setdefault("state_lane", "current")
    out.setdefault("entity_kind", "sku")
    for f in MATERIAL_FIELDS:
        out.setdefault(f, None)
    return out


def upsert_attribute_values(
    db: Session,
    records: list[dict[str, Any]],
    protect: bool = True,
    sync_run_id: int | None = None,
    changed_by: str | None = None,
) -> UpsertResult:
    """Write a batch of attribute values, skipping those that have not changed.

    `records` are dicts carrying the five key fields plus whichever payload
    fields apply. Everything in one batch is read back in a single query, so
    batch size drives the query count — a few thousand rows per call is the
    intended shape.

    Set `protect=False` only for a deliberate rebuild that is meant to discard
    in-flight state. It is not the path a scheduled sync should take.

    `sync_run_id` and `changed_by` are stamped onto the `value_changes` rows
    this writes. They are optional because history is a record of what happened,
    not a precondition for it: a caller that has no run to name should still
    write the change rather than skip it, and an unattributed change is far more
    useful than a missing one.
    """
    result = UpsertResult()
    if not records:
        return result

    prepared = [normalise(r) for r in records]

    # Read the batch's existing rows up front — a row-at-a-time existence check
    # would be thousands of round trips per batch. Chunked because the lookup
    # binds one parameter per key column and a full batch would exceed the
    # driver's variable limit.
    keys = [_key_of(r) for r in prepared]
    existing: dict[tuple, AttributeValue] = {}
    for start in range(0, len(keys), _LOOKUP_CHUNK):
        rows = db.scalars(
            select(AttributeValue).where(
                tuple_(
                    AttributeValue.entity_kind,
                    AttributeValue.entity_key,
                    AttributeValue.attribute_id,
                    AttributeValue.market,
                    AttributeValue.state_lane,
                ).in_(keys[start : start + _LOOKUP_CHUNK])
            )
        ).all()
        existing.update(
            {
                (r.entity_kind, r.entity_key, r.attribute_id, r.market, r.state_lane): r
                for r in rows
            }
        )

    to_insert: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    # One timestamp for the batch rather than one per row. A single upstream
    # edit that lands on six SKUs is one event, and six timestamps milliseconds
    # apart would render as six lines on the product timeline.
    now = utcnow()

    for record in prepared:
        row = existing.get(_key_of(record))

        if row is None:
            to_insert.append(record)
            result.inserted += 1
            continue

        if protect and row.state_lane == "current" and row.state in PROTECTED_STATES:
            if row.value != record.get("value"):
                result.protected.append(
                    Divergence(
                        entity_kind=row.entity_kind,
                        entity_key=row.entity_key,
                        attribute_id=row.attribute_id,
                        market=row.market,
                        our_state=row.state,
                        our_value=row.value,
                        their_value=record.get("value"),
                    )
                )
            else:
                # Same value, protected state — Omnitron agreeing with us is
                # not a divergence, and on the exported→live hop it is the
                # confirmation the lifecycle is waiting for. Promoting it is a
                # separate stage's job, not a side effect of writing.
                result.unchanged += 1
            continue

        changes = {
            f: record[f] for f in MATERIAL_FIELDS if getattr(row, f) != record.get(f)
        }
        if not changes:
            result.unchanged += 1
            continue

        # Captured before the writes below, which is the whole trick: one line
        # further down the old value no longer exists anywhere. Only `value` is
        # recorded — a lifecycle hop is an audit event, and mixing the two would
        # make "what did this say" a query with a filter on it.
        if "value" in changes:
            history.append(
                {
                    "entity_kind": row.entity_kind,
                    "entity_key": row.entity_key,
                    "attribute_id": row.attribute_id,
                    "market": row.market,
                    "old_value": row.value,
                    "new_value": changes["value"],
                    "changed_at": now,
                    "sync_run_id": sync_run_id,
                    "changed_by": changed_by,
                }
            )

        for f, v in changes.items():
            setattr(row, f, v)
        # Provenance is refreshed alongside a real change, never on its own.
        if record.get("source_ref") is not None:
            row.source_ref = record["source_ref"]
        if record.get("updated_by") is not None:
            row.updated_by = record["updated_by"]
        row.updated_at = utcnow()
        result.updated += 1

    if to_insert:
        db.bulk_insert_mappings(AttributeValue, to_insert)
    if history:
        # In the caller's transaction on purpose. If the value write rolls back
        # the history of it must roll back too, or the table would claim changes
        # that never landed — which is worse than no history, because it reads
        # exactly like the real thing.
        db.bulk_insert_mappings(ValueChange, history)

    db.flush()
    return result
