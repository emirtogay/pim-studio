"""record what a value used to be, not just what it is

Revision ID: b4e1c72a9f60
Revises: f2b6d3e08a41
Create Date: 2026-09-07

`state_lane` is `current|proposal`. There is no history lane, so when the
nightly sync overwrote a value the previous one was not archived or superseded
— it was gone. "What did this say last week" was unanswerable by anything in
the system, and the only evidence a change had happened at all was a count on
the run: `updated: 412`, with no way to ask which 412 or from what.

The sibling of `value_retirements`, and the pair is the point: that table
records a value *disappearing*, this one records it being *replaced*. Between
them, every way a value can stop being what it was is on the record.

Written from inside `upsert_attribute_values`, which is the only place that
holds the old value and the new one at the same instant — one line further on,
the old one does not exist anywhere. In the caller's transaction, so a rolled
back write takes its history with it; a log that claims changes which never
landed reads exactly like a real one, which makes it worse than none.

Insertions are deliberately not recorded. A first value has no "before", and
recording them would make the first sync against an empty database write a row
per value to say nothing happened.

## A note for whoever hits the branch

This revises `f2b6d3e08a41`, the committed head at the time of writing. There
was an uncommitted `a91c74e0d3b5` (product_type_on_products) in the working
tree claiming the same parent. If both land, alembic will see two heads and
refuse to run — the fix is to set one of their `down_revision`s to the other,
not to merge them, and the later of the two to land is the one that should
move.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4e1c72a9f60"
down_revision: str | Sequence[str] | None = "f2b6d3e08a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "value_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_kind", sa.String(length=12), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("attribute_id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        # Both nullable: a row can exist holding nothing and still be evidence
        # the column was read, so "was blank, now says X" is a change and must
        # not be mistaken for an insertion.
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=True),
        sa.Column("changed_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["attribute_id"], ["attributes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_change_when", "value_changes", ["changed_at"])
    # Time inside the entity, so the product timeline is a range scan rather
    # than a scan and a sort. That page is the reason this table exists.
    op.create_index(
        "ix_change_entity",
        "value_changes",
        ["entity_kind", "entity_key", "changed_at"],
    )
    op.create_index(
        "ix_value_changes_attribute_id", "value_changes", ["attribute_id"]
    )
    op.create_index("ix_value_changes_sync_run_id", "value_changes", ["sync_run_id"])


def downgrade() -> None:
    op.drop_index("ix_value_changes_sync_run_id", table_name="value_changes")
    op.drop_index("ix_value_changes_attribute_id", table_name="value_changes")
    op.drop_index("ix_change_entity", table_name="value_changes")
    op.drop_index("ix_change_when", table_name="value_changes")
    op.drop_table("value_changes")
