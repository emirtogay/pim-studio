"""catalogue takedowns and their removals

Revision ID: a91c47e5d2b8
Revises: e4a1b7c92d05
Create Date: 2026-09-09

Taking a product off a market's website means deleting its `catalog_items`
rows upstream: there is no per-market product flag, and `is_active` would pull
it from every channel including Turkey's. Those rows are the only record that
the product was in the catalogue, so once deleted the information needed to put
them back exists nowhere else. These two tables are that record — the undo,
not the log.

Additive: creates two tables, touches nothing existing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a91c47e5d2b8'
down_revision: str | Sequence[str] | None = 'e4a1b7c92d05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'catalogue_takedowns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('model_color', sa.String(length=24), nullable=False),
        sa.Column('markets', sa.String(length=32), nullable=False),
        sa.Column('catalogs', sa.String(length=64), nullable=False),
        sa.Column('reason', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('restored_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('restored_by', sa.String(length=255), nullable=True),
        sa.Column('planned_count', sa.Integer(), nullable=False),
        sa.Column('removed_count', sa.Integer(), nullable=False),
        sa.Column('failed_count', sa.Integer(), nullable=False),
        sa.Column('error_summary', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('catalogue_takedowns', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_catalogue_takedowns_model_color'), ['model_color'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_catalogue_takedowns_status'), ['status'], unique=False
        )
        batch_op.create_index(
            'ix_takedown_model_color', ['model_color', 'status'], unique=False
        )

    op.create_table(
        'catalogue_removals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('takedown_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('sku', sa.String(length=32), nullable=False),
        sa.Column('omnitron_product_pk', sa.Integer(), nullable=False),
        sa.Column('catalog_pk', sa.Integer(), nullable=False),
        sa.Column('catalog_item_pk', sa.Integer(), nullable=True),
        sa.Column('state', sa.String(length=10), nullable=False),
        sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('restored_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['takedown_id'], ['catalogue_takedowns.id'], ),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('catalogue_removals', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_catalogue_removals_takedown_id'), ['takedown_id'], unique=False
        )
        batch_op.create_index('ix_removal_takedown', ['takedown_id', 'state'], unique=False)
        batch_op.create_index(
            'ix_removal_product', ['omnitron_product_pk', 'catalog_pk'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('catalogue_removals', schema=None) as batch_op:
        batch_op.drop_index('ix_removal_product')
        batch_op.drop_index('ix_removal_takedown')
        batch_op.drop_index(batch_op.f('ix_catalogue_removals_takedown_id'))

    op.drop_table('catalogue_removals')

    with op.batch_alter_table('catalogue_takedowns', schema=None) as batch_op:
        batch_op.drop_index('ix_takedown_model_color')
        batch_op.drop_index(batch_op.f('ix_catalogue_takedowns_status'))
        batch_op.drop_index(batch_op.f('ix_catalogue_takedowns_model_color'))

    op.drop_table('catalogue_takedowns')
