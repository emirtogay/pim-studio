"""retire the market-agnostic icerik mapping

Revision ID: d8f2a1c94b60
Revises: a5e91c37b204
Create Date: 2026-08-25

`attributes__integration_kalem_icerik` was mapped to `description` with no
market, so the importer read it in the tenant default language and wrote
Turkish into a `market='*'` row. Nothing renders that row — `description` is
per-market, so every reader asks for PL/CZ/SK/HU — which is exactly why it went
unnoticed. The key is now mapped per market through
`market_attributes__<market>__integration_kalem_icerik` (0.20.0), so the
agnostic mapping has no job left.

`seed.run()` cannot do this: it only ever adds rows, so a mapping that stops
being correct stays in every database that already has it. Removals are a
migration's work.

The values it wrote go with it. Scoped to `state='live'` and
`source_kind='omnitron_api'`: anything a person approved or exported is theirs,
and this has no business touching it — even though a per-market attribute
should never have collected one at `*`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f2a1c94b60"
down_revision: str | Sequence[str] | None = "a5e91c37b204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "attributes__integration_kalem_icerik"


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT id, attribute_id FROM attribute_columns "
            "WHERE source = 'omnitron' AND column_name = :c AND market IS NULL"
        ),
        {"c": _COLUMN},
    ).fetchone()
    if row is None:
        return

    bind.execute(
        sa.text(
            "DELETE FROM attribute_values WHERE attribute_id = :a AND market = '*' "
            "AND state = 'live' AND source_kind = 'omnitron_api'"
        ),
        {"a": row[1]},
    )
    bind.execute(
        sa.text("DELETE FROM attribute_columns WHERE id = :i"), {"i": row[0]}
    )


def downgrade() -> None:
    # Restores the mapping, not the Turkish values it produced: the next values
    # run rewrites those, and inventing rows here would date them wrongly.
    bind = op.get_bind()
    attribute_id = bind.scalar(
        sa.text("SELECT id FROM attributes WHERE key = 'description'")
    )
    if attribute_id is None:
        return
    exists = bind.scalar(
        sa.text(
            "SELECT 1 FROM attribute_columns "
            "WHERE source = 'omnitron' AND column_name = :c"
        ),
        {"c": _COLUMN},
    )
    if exists:
        return
    bind.execute(
        sa.text(
            "INSERT INTO attribute_columns "
            "(attribute_id, source, column_name, market, read_only) "
            "VALUES (:a, 'omnitron', :c, NULL, true)"
        ),
        {"a": attribute_id, "c": _COLUMN},
    )
