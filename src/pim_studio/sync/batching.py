"""Reading our own product set back out of Omnitron, in batches.

We hold 26,813 SKUs against the tenant's 288,990 products, and we pull only
ours (decided 2026-08-14). `sku__in` makes that ~270 calls rather than 26,813,
which is the difference between a sync that can run on a schedule and one that
cannot.

Two traps live in here, both verified against the live instance on 2026-08-14
and both silent:

**`sku__in` must be a comma-joined string.** Passed a Python list, httpx
encodes it as repeated `sku__in=` parameters, Omnitron reads only one of them,
and the response is a perfectly ordinary HTTP 200 containing a single product.
A batch of 100 quietly becomes a batch of 1 and the sync "succeeds" having
imported a hundredth of its input.

**A short response is not an error.** Ask for 100 SKUs and 97 come back; the
three missing ones are products the API does not have under that SKU. That is
a finding — not a failure, and definitely not something to pass over. The
caller gets them back explicitly so they land in the run's drop tally rather
than evaporating.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from ..logging import log
from ..omnitron.client import OmnitronClient

#: SKUs per request. Akinon's own `channel_app` never asks for more than 100
#: and no ceiling is documented, so this matches their working assumption
#: rather than probing for a limit we would then depend on.
SKU_BATCH_SIZE = 100


def chunked(items: Iterable[str], size: int = SKU_BATCH_SIZE) -> Iterator[list[str]]:
    """Yield successive lists of at most `size` items."""
    if size < 1:
        raise ValueError(f"chunk size must be >= 1, got {size}")
    batch: list[str] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


@dataclass
class SkuFetch:
    """What one pass over a SKU list actually returned.

    `missing` is the point of this type. A dict of results alone cannot
    distinguish "asked and it is not there" from "never asked", and those need
    different responses — the first is a data finding worth reporting, the
    second is a bug.
    """

    found: dict[str, dict] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def requested(self) -> int:
        return len(self.found) + len(self.missing)

    def __repr__(self) -> str:
        return (
            f"<SkuFetch requested={self.requested} found={len(self.found)} "
            f"missing={len(self.missing)}>"
        )


def _fetch_chunk(
    api: OmnitronClient,
    batch: list[str],
    path: str,
    batch_size: int,
    params: dict[str, object] | None,
    locale: str | None,
) -> SkuFetch:
    """One request-set for one chunk of SKUs, under one locale view."""
    query: dict[str, object] = {
        **(params or {}),
        # Comma-joined string, deliberately. See the module docstring — a
        # list here silently reduces the batch to one result.
        "sku__in": ",".join(batch),
        "limit": batch_size,
    }
    found: dict[str, dict] = {}
    for record in api.iter_records(path, params=query, page_size=batch_size, locale=locale):
        sku = record.get("sku")
        if sku is not None:
            found[str(sku)] = record
    return SkuFetch(found=found, missing=[s for s in batch if s not in found])


def fetch_by_sku(
    api: OmnitronClient,
    skus: Sequence[str],
    path: str = "/api/v1/products/",
    batch_size: int = SKU_BATCH_SIZE,
    params: dict[str, object] | None = None,
    *,
    locale: str | None = None,
) -> Iterator[SkuFetch]:
    """Fetch products for our SKUs, yielding one `SkuFetch` per batch.

    Yields per batch rather than accumulating so a 26,813-SKU sync holds one
    batch in memory at a time and can write as it goes — a run that dies at SKU
    20,000 should keep the first 20,000 rather than lose everything.

    `params` is merged into each request for callers that need extra filters;
    it cannot override `sku__in` or `limit`, since those are what makes the
    batching correct.

    `locale` becomes the `Accept-Language` header on every request. Omnitron
    renders localizable attribute values in that language; with no locale the
    tenant default answers, which for the per-market content columns means
    mostly empty — the exact defect `fetch_by_sku_locales` exists to fix.
    """
    for batch in chunked(skus, batch_size):
        fetch = _fetch_chunk(api, batch, path, batch_size, params, locale)
        if fetch.missing:
            log.info(
                "sku_batch_incomplete",
                requested=len(batch),
                found=len(fetch.found),
                missing=len(fetch.missing),
                # A sample, not the list: a batch where every SKU is missing
                # should not put a hundred codes in one log line.
                sample=fetch.missing[:5],
            )
        yield fetch


def fetch_by_sku_locales(
    api: OmnitronClient,
    skus: Sequence[str],
    locales: Sequence[str | None],
    path: str = "/api/v1/products/",
    batch_size: int = SKU_BATCH_SIZE,
    params: dict[str, object] | None = None,
) -> Iterator[dict[str | None, SkuFetch]]:
    """Fetch each chunk of SKUs once per locale, yielding the views together.

    A localizable attribute holds one value per locale and Omnitron serves
    exactly one of them per request, so a caller that needs the Polish and the
    Czech copies of one product needs the same product fetched twice. Yielding
    all views of one chunk together is what lets the caller pick per product —
    holding whole passes instead would keep every record in memory at once.

    `locales` must start with `None`: the locale-less baseline is the view the
    drop tally trusts (`missing` is only authoritative there), and the
    baseline is what an untranslated value silently echoes, so resolution
    needs it in hand before any named locale is judged.
    """
    if not locales or locales[0] is not None:
        raise ValueError("locales must start with None, the baseline view")
    for batch in chunked(skus, batch_size):
        views: dict[str | None, SkuFetch] = {
            locale: _fetch_chunk(api, batch, path, batch_size, params, locale)
            for locale in locales
        }
        base = views[None]
        if base.missing:
            log.info(
                "sku_batch_incomplete",
                requested=len(batch),
                found=len(base.found),
                missing=len(base.missing),
                sample=base.missing[:5],
            )
        for locale, view in views.items():
            # A finding, not a failure: the same filter answering differently
            # under a header is worth a line in the log, and nothing more.
            if locale is not None and set(view.found) != set(base.found):
                log.info(
                    "locale_view_incomplete",
                    locale=locale,
                    baseline=len(base.found),
                    view=len(view.found),
                    delta=len(set(view.found) ^ set(base.found)),
                )
        yield views
