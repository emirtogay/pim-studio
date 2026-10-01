"""What a product's values used to be.

The one endpoint here reads the database directly, and that is the difference
between it and everything else hanging off `/api/products`. The detail page is
served from `details.json`, a generated extract, so that the page and the
nightly numbers cannot drift. History cannot come from there: the extract
carries the current state of things and is rebuilt from scratch each night, so
by construction it has no memory.

That is fine, and it is not the drift the extract rule guards against. The rule
exists so two *numbers* about the same population cannot disagree. This adds no
numbers — it reports events that already happened, each with its own timestamp,
and there is nothing here for the extract to disagree with.

## Both grains, one timeline

Values live at whichever grain they belong to: a name at `model_color`, a price
and a stock count at `sku`. A person looking at a product does not care which,
so the query gathers the option's own changes and those of every SKU under it
and interleaves them by time. The row says which it was, because "the price
changed" and "the price changed on one size" are different events and the
difference is usually the point.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Attribute, Product, User, ValueChange
from .auth import current_user

router = APIRouter(prefix="/api/products", tags=["history"])

Db = Annotated[Session, Depends(get_session)]

#: How much of a value the timeline carries. Descriptions run to 1,716
#: characters in the mirror, and a timeline is a list of what happened rather
#: than a place to read one — the full text is on the product itself.
MAX_SHOWN = 300

#: A page of history. High enough that a normal product's whole life fits in
#: one response, bounded because a product that flaps nightly for two years
#: should not be able to make this endpoint expensive.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _shown(text: str | None) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    if len(text) <= MAX_SHOWN:
        return text, False
    return text[:MAX_SHOWN], True


@router.get("/{model_color}/history")
def product_history(
    model_color: str,
    user: Annotated[User, Depends(current_user)],
    db: Db,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Every recorded change to this option's values, newest first.

    An empty list is a real answer and not the same as "nothing ever changed":
    the table starts from the day it was added, so an option can be years old
    and have no history. The page says so rather than drawing a blank panel,
    which is why `recordingSince` travels with the rows.
    """
    del user

    skus = list(
        db.scalars(select(Product.sku).where(Product.model_color == model_color))
    )
    # The option's own key and every size under it, because a person looking at
    # a product wants one timeline rather than two.
    keys = [model_color, *skus]

    rows = db.execute(
        select(
            ValueChange.changed_at,
            ValueChange.entity_kind,
            ValueChange.entity_key,
            ValueChange.market,
            ValueChange.old_value,
            ValueChange.new_value,
            ValueChange.sync_run_id,
            ValueChange.changed_by,
            Attribute.key,
            Attribute.label,
        )
        .join(Attribute, Attribute.id == ValueChange.attribute_id)
        .where(ValueChange.entity_key.in_(keys))
        # `id` breaks the tie: one upstream edit across six sizes shares a
        # timestamp by design, and without a second key their order would
        # shuffle between requests.
        .order_by(ValueChange.changed_at.desc(), ValueChange.id.desc())
        .limit(limit + 1)
    ).all()

    more = len(rows) > limit
    changes: list[dict[str, Any]] = []
    for row in rows[:limit]:
        old, old_cut = _shown(row.old_value)
        new, new_cut = _shown(row.new_value)
        changes.append(
            {
                "changedAt": row.changed_at.isoformat() if row.changed_at else None,
                "attribute": row.key,
                "label": row.label,
                "market": row.market,
                "entityKind": row.entity_kind,
                # Which size, when it was a size. Null at option grain rather
                # than repeating the ModelColor, so the page can tell them apart
                # without comparing strings.
                "sku": row.entity_key if row.entity_kind == "sku" else None,
                "oldValue": old,
                "newValue": new,
                "truncated": old_cut or new_cut,
                "syncRunId": row.sync_run_id,
                "changedBy": row.changed_by,
            }
        )

    # The oldest change anywhere, not on this product. "Nothing changed" and
    # "we only started keeping this on Monday" are different answers and the
    # page must not present the second as the first.
    since = db.scalar(select(func.min(ValueChange.changed_at)))

    return {
        "modelColor": model_color,
        "changes": changes,
        "recordingSince": since.isoformat() if since else None,
        # So the page can say "no changes recorded" rather than drawing an
        # empty panel that looks like a failed request.
        "more": more,
        "skusCovered": len(skus),
    }
