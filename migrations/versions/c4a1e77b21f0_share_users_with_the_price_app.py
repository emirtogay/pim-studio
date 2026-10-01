"""share users with the price app

Revision ID: c4a1e77b21f0
Revises: f17680a9e33e
Create Date: 2026-08-18

One login, pick a module, same account across both (05 - Decisions Log,
2026-08-18). That needs `users` to be genuinely one table, which needs both
apps in one database, which needed only `version_table` — the obstacle the
original separate-database decision was built around.

Two changes.

**This module stops owning `users`.** The baseline created it; from here the
price-comparison app's table is the one. Dropping ours is safe *in this
module's database* because nothing has ever written to it — there was no way
to create an account until today. Against the shared database this migration
runs after the price app already owns the table, so the drop is a no-op guarded
below rather than a data loss.

**`module_user_state` replaces `users.last_seen_changelog_version`.** Both
modules have their own changelog, so a shared column means reading one app's
release notes clears the other's unread flag — the bell goes dark on exactly
the releases nobody has seen. Identity is shared; per-module memory is not.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a1e77b21f0"
down_revision: str | Sequence[str] | None = "f17680a9e33e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "module_user_state",
        sa.Column("id", sa.Integer(), nullable=False),
        # No ForeignKey: it references a table this module does not own and
        # must not migrate. The constraint would also fail on a fresh database
        # where the price app has not run yet.
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("last_seen_changelog_version", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    with op.batch_alter_table("module_user_state", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_module_user_state_user_id"), ["user_id"], unique=True
        )

    # Carry anything already recorded, so a developer who read the notes before
    # this migration does not get the bell again.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("users")}
        if "last_seen_changelog_version" in columns:
            op.execute(
                "INSERT INTO module_user_state "
                "(user_id, last_seen_changelog_version, updated_at) "
                "SELECT id, last_seen_changelog_version, CURRENT_TIMESTAMP "
                "FROM users WHERE last_seen_changelog_version IS NOT NULL"
            )
        # Only drop a table this module created. On the shared database the
        # price app owns it and has rows; dropping it there would sign
        # everyone out of both modules permanently.
        if bind.scalar(sa.text("SELECT COUNT(*) FROM users")) == 0:
            op.drop_table("users")


def downgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_changelog_version", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.drop_table("module_user_state")
