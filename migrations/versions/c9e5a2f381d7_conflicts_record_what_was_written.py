"""conflicts record what was written

Revision ID: c9e5a2f381d7
Revises: b8c3f1d420ae
Create Date: 2026-08-18

Policy reversal, same day: when a ModelColor's own SKUs disagree the importer
now writes a best value and flags it, rather than abstaining. Emir's call
after seeing 5H5154.I90 — a full PRODUCT DETAIL POL column in Omnitron and an
empty cell in the panel reads as the panel being broken, not as caution.

The queue therefore stops being a list of holes and becomes a review list:
each row records the value that was written and the rule that picked it, next
to the candidates it beat. Correcting one writes an `approved` value, which
the sync writer never overwrites.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e5a2f381d7"
down_revision: str | Sequence[str] | None = "b8c3f1d420ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("value_conflicts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resolved_value", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolved_rule", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("value_conflicts", schema=None) as batch_op:
        batch_op.drop_column("resolved_rule")
        batch_op.drop_column("resolved_value")
