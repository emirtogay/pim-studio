"""the brand library cookie, where the page can replace it

Revision ID: f2b6d3e08a41
Revises: e7a3c81f9d24
Create Date: 2026-08-26

A credential in the database, which is not how anything else here is
configured, and is forced by the library rather than chosen.

The brand library has no password flow and no API token on this instance —
reading the original downloader app settles it: auth is a browser session
cookie, pasted by a person. That cookie expires within hours.

A value in the server's `.env` would therefore be stale by the second week, and
every scheduled run after that would fail until somebody edited a file on the
host and redeployed. Weekly image publishing would quietly stop being weekly.

So the cookie lives here and the Images page replaces it. The run reads
whatever is current, and the cadence becomes "paste, then click" rather than
"raise a ticket". The right long-term fix is a permanent token from whoever
administers the library; this is what works without one.

One row, pinned to id 1. Not a history: a superseded cookie is a live
credential for as long as it has left, and a table of them would be a table of
ways into the brand's library. `rotated_at`/`rotated_by` are the audit trail —
the old value is not kept.

`last_ok_at` is separate from `rotated_at` because the two answer different
questions and diverge within the day: when it was pasted, and when it last
actually worked.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b6d3e08a41"
down_revision: str | Sequence[str] | None = "e7a3c81f9d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brandlibrary_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cookie", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_by", sa.String(length=255), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("brandlibrary_session")
