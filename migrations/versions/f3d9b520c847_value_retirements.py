"""record what went dark, not just how much of it

Revision ID: f3d9b520c847
Revises: e2c8a4f16b73
Create Date: 2026-08-20

The retirement sweep added in e2c8a4f16b73 deletes the values a full pass
proves upstream has dropped, and counts them on the run. The count turned out
not to be enough: a run could report "retired 500 rows" and nothing anywhere
could say *which* 500, so "which products went dark last night" had no answer.

Deleting is still right. The model's invariant is that an absent row is the
absence — `missing` is computed, never stored — and keeping retired rows behind
a flag would put that filter in every read, where the one that forgot it would
count sold-out products as live.

So the row goes and the event stays. Append-only: a product that sells out,
restocks and sells out again is two events, because the sequence is the
interesting part.

`last_value` is what makes this a queue rather than a notification. "Lost its PL
price" and "lost its PL price, which was 349.00" are different amounts of help.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3d9b520c847"
down_revision: str | Sequence[str] | None = "e2c8a4f16b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "value_retirements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_kind", sa.String(length=12), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("attribute_id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("last_value", sa.Text(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["attribute_id"], ["attributes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_retirement_when", "value_retirements", ["retired_at"])
    op.create_index(
        "ix_retirement_entity", "value_retirements", ["entity_kind", "entity_key"]
    )
    op.create_index(
        "ix_value_retirements_attribute_id", "value_retirements", ["attribute_id"]
    )
    op.create_index(
        "ix_value_retirements_sync_run_id", "value_retirements", ["sync_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_value_retirements_sync_run_id", table_name="value_retirements")
    op.drop_index("ix_value_retirements_attribute_id", table_name="value_retirements")
    op.drop_index("ix_retirement_entity", table_name="value_retirements")
    op.drop_index("ix_retirement_when", table_name="value_retirements")
    op.drop_table("value_retirements")
