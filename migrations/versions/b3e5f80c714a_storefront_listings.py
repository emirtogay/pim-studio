"""what the shop is actually showing, per market

Revision ID: b3e5f80c714a
Revises: a91c74e0d3b5
Create Date: 2026-09-07

Every other source of liveness in this system is derived from Omnitron. That
was survivable while there was one derivation; it stopped being survivable when
two of them agreed with each other and both were wrong.

Measured 2026-09-07, at ModelColor grain:

    market   storefront   control table   our panel
    PL            4,592           3,995       3,931
    CZ            4,567           4,287       4,036
    SK            4,292           4,292       3,979
    HU            4,360           4,361       4,092

SK and HU match the storefront exactly. PL and CZ do not, in either derived
source, because `SHOP PL` and `SHOP CZE` are `is_active: false` and their
markets are served by catalogue 601 "Northwind Global" — confirmed as a
deliberate migration. `sync/scope.py` recorded the PL flag in 2026-08-14 and
argued it away on the authority of the control table, which reads the same
switched-off catalogue.

A public listing is enumerable — `/list?format=json&page_size=100`, 46 requests
a market, about three minutes for all four — so the shop can simply be asked.
Churn measured over two hours on PL: 7 options left, 1 joined, 0.17% of the
set. That is what sets the cadence at hours rather than minutes.

Grain is ModelColor because a listing page shows one card per option and says
nothing about which sizes are behind it. There is deliberately no foreign key
to `products`: 57 options were selling in PL that our tables did not hold, and
a foreign key would have hidden precisely those.

See `Findings/Storefront reconciliation`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3e5f80c714a"
down_revision = "a91c74e0d3b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storefront_listings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_color", sa.String(length=24), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_color", "market", name="uq_listing_option_market"),
    )
    op.create_index(
        "ix_storefront_listings_model_color", "storefront_listings", ["model_color"]
    )
    op.create_index("ix_listing_market", "storefront_listings", ["market", "left_at"])


def downgrade() -> None:
    op.drop_index("ix_listing_market", table_name="storefront_listings")
    op.drop_index("ix_storefront_listings_model_color", table_name="storefront_listings")
    op.drop_table("storefront_listings")
