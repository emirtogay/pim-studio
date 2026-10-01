"""market sentinel on attribute_values

Revision ID: b1f4a7c93e02
Revises: 127f3c2467dd
Create Date: 2026-08-14

`uq_value_entity_attr_market_lane` includes `market`, and SQL treats NULLs as
distinct in a unique constraint. With `market` nullable, the one-row-per-key
invariant the whole model rests on was unenforced for every `per_market=False`
attribute — 19 of the 26 in the registry — so a re-run of the importer would
have inserted duplicate rows rather than updating.

This makes `market` NOT NULL and stores `*` for market-agnostic values. A
partial unique index (`... WHERE market IS NULL`) would restore the constraint
but not `ON CONFLICT`, which still would not match NULL rows, leaving the
importer with two write paths. The sentinel keeps both uniform.

`attribute_columns.market` is deliberately left nullable: there, NULL means the
source column carries no market, which the importer resolves to `*` on the way
in. Different question, different column.

Safe to run: `attribute_values` was empty at the time this was written, and the
UPDATE below covers the case where it isn't.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1f4a7c93e02"
down_revision: str | Sequence[str] | None = "127f3c2467dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Spelled out so SQLite's batch mode rebuilds the table with its constraints
# and indexes intact — reflection alone does not reliably recover them, and
# anything omitted here is silently dropped by the rebuild.
_ATTRIBUTE_VALUES = sa.Table(
    "attribute_values",
    sa.MetaData(),
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("entity_kind", sa.String(length=12), nullable=False),
    sa.Column("entity_key", sa.String(length=32), nullable=False),
    sa.Column("attribute_id", sa.Integer(), nullable=False),
    sa.Column("market", sa.String(length=4), nullable=True),
    sa.Column("state_lane", sa.String(length=8), nullable=False),
    sa.Column("value", sa.Text(), nullable=True),
    sa.Column("state", sa.String(length=10), nullable=False),
    sa.Column("entry_grain", sa.String(length=12), nullable=False),
    sa.Column("source_kind", sa.String(length=16), nullable=False),
    sa.Column("source_ref", sa.String(length=255), nullable=True),
    sa.Column("suspect_flag", sa.String(length=12), nullable=True),
    sa.Column("suspect_reason", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_by", sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(["attribute_id"], ["attributes.id"]),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint(
        "entity_kind", "entity_key", "attribute_id", "market", "state_lane",
        name="uq_value_entity_attr_market_lane",
    ),
    sa.Index("ix_attribute_values_attribute_id", "attribute_id"),
    sa.Index("ix_attribute_values_state", "state"),
    sa.Index("ix_values_attr_state", "attribute_id", "state"),
    sa.Index("ix_values_entity", "entity_kind", "entity_key"),
)


def upgrade() -> None:
    op.execute("UPDATE attribute_values SET market = '*' WHERE market IS NULL")
    with op.batch_alter_table(
        "attribute_values", schema=None, copy_from=_ATTRIBUTE_VALUES
    ) as batch_op:
        batch_op.alter_column(
            "market",
            existing_type=sa.String(length=4),
            nullable=False,
            server_default="*",
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "attribute_values", schema=None, copy_from=_ATTRIBUTE_VALUES
    ) as batch_op:
        batch_op.alter_column(
            "market",
            existing_type=sa.String(length=4),
            nullable=True,
            server_default=None,
        )
    op.execute("UPDATE attribute_values SET market = NULL WHERE market = '*'")
