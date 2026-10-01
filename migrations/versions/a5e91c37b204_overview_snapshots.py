"""the funnel's shape on one day, so next week can be compared to it

Revision ID: a5e91c37b204
Revises: f3d9b520c847
Create Date: 2026-08-25

The Overview page's headline strip reports what changed since last week. Until
now nothing could answer that. `sync/writer.py` has been careful since the
beginning to bump `updated_at` only on a genuine value change — precisely so
this question would stay answerable — but a timestamp per value says when
something last moved, not what the catalogue looked like before it did. A
funnel is a shape, and a shape can only be compared against another shape.

So: one row per day, holding the aggregates the page renders. Deliberately not
a per-value history. That would be four orders of magnitude larger, and it is a
different design problem — `ValueRetirement` already carries the one slice of
per-value history the liveness sync needs, and says of itself that it is not
this table.

Upserted by date rather than appended. The nightly build writes today's row,
and a manual rebuild through the panel refines it instead of adding a second.
Two rows for one day would make "a week ago" ambiguous, and the page would
quietly compare against whichever one sorted first.

`payload` is JSON in Text. Nothing queries into it — it is written whole and
read whole, by date — so JSONB would add a second code path between the SQLite
used in development and the Postgres used in production to buy an index nobody
would use. `ValueConflict` made the same call.

`version` is what stops a definition change reading as a week of progress. When
the funnel's stages change, last week's payload is not a baseline; it is a
differently-shaped object whose keys happen to match. A mismatch reads as "no
comparable baseline" rather than as a delta.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5e91c37b204"
down_revision: str | Sequence[str] | None = "f3d9b520c847"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "overview_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique, not merely indexed: one snapshot per day is the invariant the
    # baseline lookup rests on, and enforcing it in the schema means a second
    # writer racing the nightly build fails loudly instead of duplicating.
    op.create_index(
        "ix_overview_snapshots_snapshot_date",
        "overview_snapshots",
        ["snapshot_date"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_overview_snapshots_snapshot_date", table_name="overview_snapshots"
    )
    op.drop_table("overview_snapshots")
