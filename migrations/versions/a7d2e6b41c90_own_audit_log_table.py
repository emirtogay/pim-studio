"""give this module its own audit log table

Revision ID: a7d2e6b41c90
Revises: c4a1e77b21f0
Create Date: 2026-08-18

The baseline created `audit_log`. Sharing a database with the price-comparison
app means that name is taken — by a table of the identical shape, with rows,
that the other app writes to. The first deploy against the shared database
aborted on it:

    psycopg.errors.DuplicateTable: relation "audit_log" already exists

The baseline now creates `pm_audit_log` directly, which is enough for any
database that has not run it yet. This migration is for the ones that have:
a dev database created before today still carries the table under the old
name, and renaming it keeps the rows.

The guard matters more than the rename. On the shared database `pm_audit_log`
already exists (the fixed baseline made it) and `audit_log` is the price app's
— so the condition is false and we leave their table alone. Renaming it there
would move another app's activity feed out from under it.

Identity is shared, per-module memory is not — the same line `module_user_state`
draws for the changelog bell (05 - Decisions Log, 2026-08-18).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d2e6b41c90"
down_revision: str | Sequence[str] | None = "c4a1e77b21f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD = "audit_log"
NEW = "pm_audit_log"
INDEXED = ("created_at", "user_email", "user_id")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # Only when ours is still under the old name and the new one is free.
    # Both halves are load-bearing: on the shared database NEW exists, so this
    # is a no-op and the price app's OLD table is never touched.
    if OLD not in tables or NEW in tables:
        return

    existing = {ix["name"] for ix in inspector.get_indexes(OLD)}
    for column in INDEXED:
        name = f"ix_{OLD}_{column}"
        if name in existing:
            op.drop_index(name, table_name=OLD)

    op.rename_table(OLD, NEW)

    for column in INDEXED:
        op.create_index(f"ix_{NEW}_{column}", NEW, [column], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # Refuse to rename back over a table the price app owns.
    if NEW not in tables or OLD in tables:
        return

    for column in INDEXED:
        op.drop_index(f"ix_{NEW}_{column}", table_name=NEW)

    op.rename_table(NEW, OLD)

    for column in INDEXED:
        op.create_index(f"ix_{OLD}_{column}", OLD, [column], unique=False)
