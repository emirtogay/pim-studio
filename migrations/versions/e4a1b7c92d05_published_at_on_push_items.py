"""push_items.published_at — when the shop actually started serving it

Revision ID: e4a1b7c92d05
Revises: d3f8a12c5b7e
Create Date: 2026-09-09

A pushed value reaches `live` seconds after the write, on the strength of
reading it back from Omnitron. That is the right meaning for `live` — every
mirrored row in this database uses it for "Omnitron's current value", and
stock, price and image rows are stored that way too — but it is not the
question a merchandiser is asking. They want to know whether a customer can
see it.

Measured on the first real push (2026-09-08, vault Findings): Omnitron had the
value at 13:47:03 and the Polish storefront served it at 15:52. For those two
hours the panel said `live` about text nobody could read. The same product was
published by one channel in 27 seconds and another in two hours, so the delay
is not a constant anybody could hard-code either.

So publication is recorded separately rather than folded into the state
machine: `live` keeps meaning what it means everywhere else, and this column
answers the different question. Null means the shop has not been seen serving
it yet — never that it failed.
"""

import sqlalchemy as sa
from alembic import op

revision = "e4a1b7c92d05"
down_revision = "d3f8a12c5b7e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "push_items",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The confirmation pass asks "which items are still unpublished, by
    # market" on every run. Without this it is a scan of every item ever
    # pushed, and that table only grows.
    op.create_index(
        "ix_push_items_unpublished",
        "push_items",
        ["market", "published_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_push_items_unpublished", table_name="push_items")
    op.drop_column("push_items", "published_at")
