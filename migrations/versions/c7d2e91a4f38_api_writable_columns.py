"""which source columns a push may write, and to which market

Revision ID: c7d2e91a4f38
Revises: b3e5f80c714a
Create Date: 2026-09-07

The panel is about to write attribute values back into Omnitron. An Omnitron
product is one object across all 26 channels and its attribute values carry no
catalogue dimension, so a write cannot be scoped by catalogue at all — the only
things that keep a Polish edit out of the Turkish store are the *key name*
(`urun_adi_pl`, not `urun_adi_ru` or the core `name`) and, for keys Omnitron
marks localizable, the locale header the write is sent under.

`attribute_columns` already records, per literal source column, which
(attribute, market) it feeds. This flag marks the columns a push may address,
and nothing outside the flagged set can be assembled into a payload
(`sync/targets.py`). The flag is per column rather than per attribute because
the same attribute reads from several columns — `description` from the shared
`integration_kalem_icerik` under four locales *and* from the market-suffixed
siblings — and only the siblings are safe to write: they are the ones the four
storefronts render, and each names its market.

`seed.run()` owns the set and re-applies it on every boot; the update below is
so a database that is migrated but not yet re-seeded holds the same answer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d2e91a4f38"
down_revision = "b3e5f80c714a"
branch_labels = None
depends_on = None

WRITABLE = (
    "attributes__urun_adi_pl",
    "attributes__urun_adi_cz",
    "attributes__urun_adi_sk",
    "attributes__urun_adi_hu",
    "attributes__urun_detay_pol",
    "attributes__urun_detay_cze",
    "attributes__urun_detay_sk",
    "attributes__urun_detay_hu",
    "attributes__integration_kalem_icerik_pol",
    "attributes__integration_kalem_icerik_cz",
    "attributes__integration_kalem_icerik_sk",
    "attributes__integration_kalem_icerik_hu",
    "attributes__PL_sezon",
    "attributes__PL_comment",
)


def upgrade() -> None:
    op.add_column(
        "attribute_columns",
        sa.Column(
            "api_writable", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    columns = sa.table(
        "attribute_columns",
        sa.column("source", sa.String),
        sa.column("column_name", sa.String),
        sa.column("api_writable", sa.Boolean),
    )
    op.execute(
        columns.update()
        .where(columns.c.source == "omnitron")
        .where(columns.c.column_name.in_(WRITABLE))
        .values(api_writable=True)
    )


def downgrade() -> None:
    op.drop_column("attribute_columns", "api_writable")
