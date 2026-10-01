"""Where a value may be written in Omnitron, and under which locale.

The push writes attribute values back into Omnitron, and the one property it
has to hold is that a write for Poland cannot reach the Turkish, Russian or
Kazakh stores. An Omnitron product is a single object across all 26 channels
and its attribute values carry no catalogue dimension, so the catalogue cannot
scope a write. Two things can:

**The key name.** Per-market content lives in market-named keys — `urun_adi_pl`,
`urun_detay_cze`, `integration_kalem_icerik_hu` — with `_ru`, `_kz`, `_ua`
siblings beside them and, for description, a shared `integration_kalem_icerik`
that every store reads. A payload may only carry keys whose registry column
names the market being written (`attribute_columns.market`), which is exactly
what `require_market_keys` enforces.

**The locale header.** Some of those keys are localizable
(`omnitron_attributes.is_localizable`): Omnitron holds one copy per language
and a POST writes the copy named by `Accept-Language`, or the tenant default —
the Turkish store's — when the header is absent. So a localizable key is
written under its market's designated locale and never without one.

Both facts are registry data. `attribute_columns.api_writable` marks the
columns a push may address (owned by `seed.API_WRITABLE_COLUMNS`), and this
module turns those rows into `WriteTarget`s. Nothing else in the codebase
decides what may be written; the client's own guard (`omnitron.client.post`)
takes the allowed keys from here too, so the two checks cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MARKET_AGNOSTIC, Attribute, AttributeColumn, OmnitronAttribute
from .scope import MarketScopeViolation

#: The locale a market's localizable keys are written under. Designated only —
#: `values.LOCALE_CHAINS` lets a *read* of CZ fall back to `hu` because this
#: tenant misfiles Czech content there, but a write must never follow it.
WRITE_LOCALES: dict[str, str] = {"PL": "pl", "CZ": "cs", "SK": "sk", "HU": "hu"}

_FLAT = "attributes__"


class WriteTargetError(RuntimeError):
    """The registry flags a column the push cannot safely write.

    Raised at load time rather than at write time so a bad seed fails the
    first push outright, before any product is touched.
    """


@dataclass(frozen=True)
class WriteTarget:
    """One (attribute, market) pair and the Omnitron key it is written to."""

    attribute_id: int
    attribute_key: str
    #: PL|CZ|SK|HU, or `*` for a global key only our team uses (PL_sezon).
    market: str
    #: The Omnitron attribute key, without the `attributes__` prefix.
    key: str
    localizable: bool

    @property
    def locale(self) -> str | None:
        """The header this key is written under. `None` means send none."""
        if not self.localizable:
            return None
        return WRITE_LOCALES[self.market]


def write_targets(db: Session) -> dict[tuple[int, str], WriteTarget]:
    """Every (attribute_id, market) a push may write, from the registry.

    Only `attributes__<key>` columns qualify. A flagged `market_attributes__`
    or `localized_attributes__` column is a seed mistake — those address the
    shared key or Omnitron's translation store, neither of which is isolated
    by name — and raises rather than being skipped.

    Localizability comes from the mirrored registry. A key the mirror does not
    know is treated as localizable, because sending a locale header to a
    non-localizable key is harmless and omitting it from a localizable one
    writes the Turkish copy; a global (`*`) key that would need a locale has
    nowhere to send one and raises.
    """
    localizable = dict(
        db.execute(select(OmnitronAttribute.key, OmnitronAttribute.is_localizable)).all()
    )
    rows = db.execute(
        select(AttributeColumn, Attribute)
        .join(Attribute, Attribute.id == AttributeColumn.attribute_id)
        .where(AttributeColumn.source == "omnitron")
        .where(AttributeColumn.api_writable.is_(True))
        .order_by(AttributeColumn.id)
    ).all()

    targets: dict[tuple[int, str], WriteTarget] = {}
    for column, attribute in rows:
        name = column.column_name
        if not name.startswith(_FLAT):
            raise WriteTargetError(
                f"{name} is flagged api_writable but only attributes__<key> "
                "columns may be written: shared and localized paths are not "
                "isolated by name."
            )
        key = name[len(_FLAT):]
        market = column.market or MARKET_AGNOSTIC
        if market != MARKET_AGNOSTIC and market not in WRITE_LOCALES:
            raise WriteTargetError(f"{name} names market {market!r}, which is not ours.")
        is_localizable = bool(localizable.get(key, True))
        if market == MARKET_AGNOSTIC and is_localizable:
            raise WriteTargetError(
                f"{name} is localizable (or unknown to the mirror) and has no "
                "market, so there is no locale to write it under."
            )
        pair = (attribute.id, market)
        if pair in targets:
            raise WriteTargetError(
                f"{attribute.key} [{market}] has two writable columns: "
                f"{targets[pair].key} and {key}."
            )
        targets[pair] = WriteTarget(
            attribute_id=attribute.id,
            attribute_key=attribute.key,
            market=market,
            key=key,
            localizable=is_localizable,
        )
    return targets


def keys_for_market(
    targets: dict[tuple[int, str], WriteTarget], market: str
) -> dict[str, WriteTarget]:
    """The Omnitron keys one market's payloads may carry, by key."""
    return {t.key: t for t in targets.values() if t.market == market}


def require_market_keys(
    targets: dict[tuple[int, str], WriteTarget], market: str, keys: Iterable[str]
) -> str | None:
    """Refuse a payload that names a key outside this market; return its locale.

    The locale is the one every key in the group must be written under: the
    market's designated locale if any key is localizable, else none. A group
    mixes markets only by a bug upstream, and that bug raises here rather than
    producing a request.
    """
    keys = list(keys)
    allowed = keys_for_market(targets, market)
    foreign = sorted(k for k in keys if k not in allowed)
    if foreign:
        raise MarketScopeViolation(
            f"{foreign} may not be written for market {market}: not among its "
            f"targets {sorted(allowed)}."
        )
    if any(allowed[k].localizable for k in keys):
        return WRITE_LOCALES[market]
    return None
