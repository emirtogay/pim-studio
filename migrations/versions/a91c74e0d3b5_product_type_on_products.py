"""product_type, so a grouping row stops counting as a size

Revision ID: a91c74e0d3b5
Revises: b4e1c72a9f60
Create Date: 2026-09-07

`sync.catalogue` mints a `Product` for every catalogue item, and Omnitron puts
its **product_meta** rows in catalogues alongside real ones. A meta is the
grouping object behind a listing card: one per base code, `sku` equal to the
bare base code, no size, nothing sellable.

Measured on the 2026-08-20 extract, against all four live storefronts:

* 9,477 of 18,036 options carried one, and 4,207 of those showed a duplicated
  size label as a direct result — the meta's
  `integration_base_code_format` is `STYLE.COLOUR.SIZE` for one arbitrary
  colour and size, so it lands *inside* a real ModelColor.
* 4,449 options consisted of nothing else. None was live in any market; they
  sat in the grid and in the work queues as "No price" (3,187) and "Not in
  catalogue" (1,154), and in the denominator behind every percentage.
* 404 of the 780 options the panel banned in PL had no sellable SKU carrying a
  banning comment. `PL_comment` is registered at model_color grain, so when
  only the meta carried a value there was no disagreement for
  `sync.conflicts` to settle and it became the option's — killing the option
  in all four markets while its sibling colours stayed live.

The column is NULL here rather than backfilled. The shape is guessable
(`sku == base_code`) and guessing is what this fix is about: `refresh_products`
reads every row we hold and stamps the value from Omnitron, so one pass fills
it in from the source. Until then `Product.is_size()` treats NULL as a size,
which keeps a never-refreshed table rendering rather than empty.

See `Findings/Storefront reconciliation`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a91c74e0d3b5"
down_revision = "b4e1c72a9f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products", sa.Column("omnitron_product_type", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("products", "omnitron_product_type")
