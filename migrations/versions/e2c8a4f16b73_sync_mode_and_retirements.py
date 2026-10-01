"""a run records whether it was entitled to conclude absence

Revision ID: e2c8a4f16b73
Revises: d4b7c9e15a02
Create Date: 2026-08-20

The nightly sync becomes incremental, which makes one distinction load-bearing
that did not exist before: whether a given run read *everything* upstream holds
or only what changed since a watermark.

Only a full pass can conclude absence. If an incremental catalogue pass ran the
departure sweep, every membership it happened not to re-read would be marked as
having left — which on a quiet night is essentially all of them. So the sweeps
are refused in incremental mode, and `mode` is what the refusal keys on.

The column also has to survive the run, not just guard it. Without it a nightly
incremental and a weekly full reconciliation are the same green row on the runs
page, and "when was the catalogue last genuinely reconciled" has no answer.

`rows_retired` is kept apart from `rows_dropped` deliberately. Dropped means
"read and deliberately not written" and legitimately sits in the thousands;
retired means "we had this and upstream no longer does". A mass deletion hidden
inside a routinely-large number is exactly the shape of failure this project
keeps finding.

Existing rows backfill to `full`, which is what every one of them was — nothing
incremental existed before this revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2c8a4f16b73"
down_revision: str | Sequence[str] | None = "d4b7c9e15a02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default rather than a bare default: the backfill of existing rows
    # has to happen in the database, and a Python-side default would leave
    # every historical row NULL against a NOT NULL column.
    op.add_column(
        "sync_runs",
        sa.Column("mode", sa.String(length=12), nullable=False, server_default="full"),
    )
    op.add_column(
        "sync_runs",
        sa.Column("rows_retired", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sync_runs", "rows_retired")
    op.drop_column("sync_runs", "mode")
