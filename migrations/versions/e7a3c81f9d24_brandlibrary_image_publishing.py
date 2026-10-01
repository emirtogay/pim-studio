"""what was published, what could not be, and what was handed over

Revision ID: e7a3c81f9d24
Revises: d8f2a1c94b60
Create Date: 2026-08-26

Image publishing has run once, from a laptop, against a text file of targets,
and wrote its results to a CSV that lives in `scripts/`. It worked — 1,603
files for 102 options — and none of that is durable: the manifest is the
deliverable and it is on one machine, the targets were maintained by hand, and
nothing recorded the options the library simply does not hold.

Three tables, because a run produces three things that outlive it.

`published_images` replaces the CSV. Keyed on the remote filename, which is the
real identity here: it is what a second run would silently overwrite, and the
first run wasted 228 uploads discovering that 136 assets shared a name. The
unique constraint makes the collision a database error rather than a file
quietly replaced.

`landed_at` is the same `exported → live` hop `attribute_values` has, for the
same reason: the handoff is a file sent to another team with no acknowledgement
from Omnitron. Nullable, and null is the honest default — it means "we have not
seen this photo in Omnitron", not "Turkey did not upload it".

`brandlibrary_absences` records searching and finding nothing. This is the one
table here that exists to make a silence audible: publishing zero photos for an
option is indistinguishable from never having looked, and a weekly run reports
success either way. `attempts` is what turns it into a sentence somebody can
take to the brand.

`handoff_bundles` rows outlive their files deliberately. Bundles are pruned
from disk; what was handed over in August has to stay answerable after the ZIP
is gone, so the paths may dangle while the counts stay true.

Note what is *not* here: no write path into `attribute_values.image_0`. That
column mirrors Omnitron's own URL, and putting our FTP address in it would make
the mirror report a product as imaged while the site still shows nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7a3c81f9d24"
down_revision: str | Sequence[str] | None = "d8f2a1c94b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Bundles first: `published_images.bundle_id` points at it.
    op.create_table(
        "handoff_bundles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("built_by", sa.String(length=255), nullable=True),
        sa.Column("option_count", sa.Integer(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("zip_path", sa.String(length=512), nullable=True),
        sa.Column("xlsx_path", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "published_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_color", sa.String(length=24), nullable=False),
        sa.Column("brandlibrary_key", sa.String(length=32), nullable=False),
        sa.Column("asset_name", sa.String(length=128), nullable=False),
        sa.Column("edition", sa.String(length=16), nullable=False),
        sa.Column("remote_name", sa.String(length=160), nullable=False),
        sa.Column("public_url", sa.String(length=512), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=True),
        sa.Column("bundle_id", sa.Integer(), nullable=True),
        sa.Column("landed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"]),
        sa.ForeignKeyConstraint(["bundle_id"], ["handoff_bundles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_name", name="uq_published_remote_name"),
    )
    op.create_index("ix_published_images_model_color", "published_images", ["model_color"])
    op.create_index("ix_published_model_color", "published_images", ["model_color"])
    op.create_index("ix_published_landed", "published_images", ["landed_at"])
    op.create_index("ix_published_images_sync_run_id", "published_images", ["sync_run_id"])
    op.create_index("ix_published_images_bundle_id", "published_images", ["bundle_id"])

    op.create_table(
        "brandlibrary_absences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_color", sa.String(length=24), nullable=False),
        sa.Column("first_missed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_brandlibrary_absences_model_color",
        "brandlibrary_absences",
        ["model_color"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_brandlibrary_absences_model_color", table_name="brandlibrary_absences")
    op.drop_table("brandlibrary_absences")
    for name in (
        "ix_published_images_bundle_id",
        "ix_published_images_sync_run_id",
        "ix_published_landed",
        "ix_published_model_color",
        "ix_published_images_model_color",
    ):
        op.drop_index(name, table_name="published_images")
    op.drop_table("published_images")
    op.drop_table("handoff_bundles")
