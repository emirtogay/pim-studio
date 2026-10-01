"""record the conflicts the importer abstains from

Revision ID: b8c3f1d420ae
Revises: a7d2e6b41c90
Create Date: 2026-08-18

The value importer refuses to guess when a ModelColor's own SKUs disagree, and
the first full sync abstained on 12,193 of them. That is the right call -
measured, roughly seven in eight are genuine editorial disagreements, and a
majority vote picks the un-updated generic value as often as the right one.

But the abstentions existed only as log lines. Nobody could list them, count
them per attribute, or work through them, which makes "we abstained"
indistinguishable from "there was no data" to anyone looking at the panel.

A snapshot, not a history: the values stage replaces these rows wholesale on
every run. A conflict resolved upstream should stop appearing on its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c3f1d420ae"
down_revision: str | Sequence[str] | None = "a7d2e6b41c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "value_conflicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_kind", sa.String(length=16), nullable=False),
        sa.Column("entity_key", sa.String(length=64), nullable=False),
        sa.Column("attribute_id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("candidates", sa.Text(), nullable=False),
        sa.Column("sku_count", sa.Integer(), nullable=False),
        sa.Column("sources", sa.String(length=255), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("value_conflicts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_value_conflicts_entity_key"), ["entity_key"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_value_conflicts_attribute_id"), ["attribute_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_value_conflicts_sync_run_id"), ["sync_run_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("value_conflicts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_value_conflicts_sync_run_id"))
        batch_op.drop_index(batch_op.f("ix_value_conflicts_attribute_id"))
        batch_op.drop_index(batch_op.f("ix_value_conflicts_entity_key"))

    op.drop_table("value_conflicts")
