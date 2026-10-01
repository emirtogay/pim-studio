"""localised vocabulary labels, with the untranslated ones marked

Revision ID: d4b7c9e15a02
Revises: c9e5a2f381d7
Create Date: 2026-08-18

Omnitron localises dropdown labels through the `Accept-Language` header, and
the panel was showing the tenant default (Turkish) everywhere: `Kısa Kollu`
where the Polish backoffice shows `Krótki Rękaw`.

`translated` is why this is a table rather than five extra columns on the
value. An untranslated term comes back as the Turkish one with no marker, so
the only moment the difference is knowable is while the baseline and the
locale response are both in hand. Recording that verdict is what keeps a
Turkish label from being stored as though it were Polish — and the gap is
customer-facing, because the storefront falls back the same way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b7c9e15a02"
down_revision: str | Sequence[str] | None = "c9e5a2f381d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vocabulary_labels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attribute_id", sa.Integer(), nullable=False),
        sa.Column("baseline", sa.String(length=255), nullable=False),
        sa.Column("locale", sa.String(length=5), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("translated", sa.Boolean(), nullable=False),
        sa.Column("ambiguous", sa.Boolean(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attribute_id"], ["attributes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attribute_id", "baseline", "locale", name="uq_vocab_label_attr_base_locale"
        ),
    )
    with op.batch_alter_table("vocabulary_labels", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_vocabulary_labels_attribute_id"), ["attribute_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_vocabulary_labels_baseline"), ["baseline"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("vocabulary_labels", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_vocabulary_labels_baseline"))
        batch_op.drop_index(batch_op.f("ix_vocabulary_labels_attribute_id"))

    op.drop_table("vocabulary_labels")
