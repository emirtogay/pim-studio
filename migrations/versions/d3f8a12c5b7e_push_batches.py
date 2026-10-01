"""every value pushed to Omnitron, and what became of it

Revision ID: d3f8a12c5b7e
Revises: c7d2e91a4f38
Create Date: 2026-09-07

A push writes attribute values into Omnitron through the API. `exported` on
the value says it went out; it cannot say when, in which batch, for which
sizes, whether the read-back confirmed it, or why one size was skipped. Those
are the questions the morning after asks, so they get tables of their own.

`push_batches` is one row per push somebody started: who, from where (a cell
on the product page, an upload, a bulk edit, the CLI), and how it ended.
`halted` is its own end state: the read-back found a key changed that the
push did not send, the run stopped before touching another product, and the
row says so in red until somebody has looked.

`push_items` is one row per (size, attribute, market) the batch carried —
the grain Omnitron writes at, not the grain we store at. `old_upstream` is
what Omnitron held before the write, kept because "what did we overwrite" is
the question a restore needs answered.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3f8a12c5b7e"
down_revision = "c7d2e91a4f38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sku_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("written_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sync_run_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )
    op.create_index("ix_push_batches_status", "push_batches", ["status"])
    op.create_table(
        "push_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "batch_id", sa.Integer(), sa.ForeignKey("push_batches.id"), nullable=False
        ),
        sa.Column("model_color", sa.String(24), nullable=False),
        sa.Column("sku", sa.String(32), nullable=False),
        sa.Column(
            "attribute_id", sa.Integer(), sa.ForeignKey("attributes.id"), nullable=False
        ),
        sa.Column("market", sa.String(4), nullable=False),
        sa.Column("locale", sa.String(8), nullable=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("old_upstream", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_push_items_batch_status", "push_items", ["batch_id", "status"])
    op.create_index("ix_push_items_model_color", "push_items", ["model_color"])


def downgrade() -> None:
    op.drop_index("ix_push_items_model_color", table_name="push_items")
    op.drop_index("ix_push_items_batch_status", table_name="push_items")
    op.drop_table("push_items")
    op.drop_index("ix_push_batches_status", table_name="push_batches")
    op.drop_table("push_batches")
