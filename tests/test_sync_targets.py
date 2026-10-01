"""Write targets — the registry decides what a push may write, and where.

The property under test is isolation: a payload for one market cannot carry a
key that belongs to another store, a shared key, or a localizable key without
that market's locale. Each of those is a request that would land in the
Turkish, Russian or Kazakh catalogue while looking like a Polish edit.
"""

from __future__ import annotations

import pytest

from pim_studio.db import session_scope
from pim_studio.models import Attribute, AttributeColumn, OmnitronAttribute
from pim_studio.sync.scope import MarketScopeViolation
from pim_studio.sync.targets import (
    WriteTargetError,
    keys_for_market,
    require_market_keys,
    write_targets,
)

pytestmark = pytest.mark.usefixtures("temp_db")


def an_attribute(key: str, per_market: bool = True) -> int:
    with session_scope() as db:
        a = Attribute(
            key=key, label=key, write_channel="omnitron_import",
            value_grain="model_color", per_market=per_market,
        )
        db.add(a)
        db.flush()
        return a.id


def a_column(
    attr_id: int, column_name: str, market: str | None = None, *, writable: bool = True
) -> None:
    with session_scope() as db:
        db.add(
            AttributeColumn(
                attribute_id=attr_id, source="omnitron", column_name=column_name,
                market=market, api_writable=writable,
            )
        )


def mirror(key: str, is_localizable: bool, pk: int) -> None:
    with session_scope() as db:
        db.add(
            OmnitronAttribute(
                pk=pk, key=key, name=key, data_type="text", is_localizable=is_localizable,
            )
        )


def seeded() -> None:
    name = an_attribute("product_name")
    desc = an_attribute("description")
    season = an_attribute("pl_sezon", per_market=False)
    a_column(name, "attributes__urun_adi_pl", "PL")
    a_column(name, "attributes__urun_adi_cz", "CZ")
    a_column(name, "attributes__urun_adi_ru", "PL", writable=False)
    a_column(desc, "attributes__integration_kalem_icerik_pol", "PL")
    a_column(desc, "market_attributes__PL__integration_kalem_icerik", "PL", writable=False)
    a_column(season, "attributes__PL_sezon")
    mirror("urun_adi_pl", True, 1)
    mirror("urun_adi_cz", True, 2)
    mirror("integration_kalem_icerik_pol", False, 3)
    mirror("PL_sezon", False, 4)


def test_only_flagged_flat_columns_become_targets() -> None:
    seeded()
    with session_scope() as db:
        targets = write_targets(db)
    keys = sorted(t.key for t in targets.values())
    assert keys == ["PL_sezon", "integration_kalem_icerik_pol", "urun_adi_cz", "urun_adi_pl"]


def test_a_localizable_key_writes_under_its_markets_designated_locale() -> None:
    seeded()
    with session_scope() as db:
        by_key = {t.key: t for t in write_targets(db).values()}
    assert by_key["urun_adi_pl"].locale == "pl"
    # Czech is `cs` to Omnitron — never the `hu` fallback the read chain allows.
    assert by_key["urun_adi_cz"].locale == "cs"
    assert by_key["integration_kalem_icerik_pol"].locale is None
    assert by_key["PL_sezon"].market == "*"
    assert by_key["PL_sezon"].locale is None


def test_a_key_the_mirror_does_not_know_is_treated_as_localizable() -> None:
    name = an_attribute("product_name")
    a_column(name, "attributes__urun_adi_sk", "SK")
    with session_scope() as db:
        (target,) = write_targets(db).values()
    assert target.localizable and target.locale == "sk"


def test_a_global_key_that_would_need_a_locale_is_refused() -> None:
    season = an_attribute("pl_sezon", per_market=False)
    a_column(season, "attributes__PL_sezon")  # not in the mirror, so unknown
    with session_scope() as db, pytest.raises(WriteTargetError, match="no locale"):
        write_targets(db)


@pytest.mark.parametrize(
    "column",
    ["market_attributes__PL__integration_kalem_icerik", "localized_attributes__pl__l_name"],
)
def test_a_shared_or_localized_path_flagged_writable_is_a_seed_error(column: str) -> None:
    desc = an_attribute("description")
    a_column(desc, column, "PL")
    with session_scope() as db, pytest.raises(WriteTargetError, match="isolated by name"):
        write_targets(db)


def test_a_foreign_market_on_a_writable_column_is_refused() -> None:
    name = an_attribute("product_name")
    a_column(name, "attributes__urun_adi_ua", "UA")
    mirror("urun_adi_ua", True, 9)
    with session_scope() as db, pytest.raises(WriteTargetError, match="not ours"):
        write_targets(db)


def test_two_writable_columns_for_one_pair_are_refused() -> None:
    desc = an_attribute("description")
    a_column(desc, "attributes__integration_kalem_icerik_pol", "PL")
    a_column(desc, "attributes__integration_kalem_icerik", "PL")
    mirror("integration_kalem_icerik_pol", False, 3)
    mirror("integration_kalem_icerik", True, 5)
    with session_scope() as db, pytest.raises(WriteTargetError, match="two writable"):
        write_targets(db)


def test_a_payload_may_only_carry_its_own_markets_keys() -> None:
    seeded()
    with session_scope() as db:
        targets = write_targets(db)
    assert sorted(keys_for_market(targets, "PL")) == [
        "integration_kalem_icerik_pol", "urun_adi_pl",
    ]
    with pytest.raises(MarketScopeViolation, match="urun_adi_cz"):
        require_market_keys(targets, "PL", ["urun_adi_pl", "urun_adi_cz"])
    with pytest.raises(MarketScopeViolation, match="integration_kalem_icerik"):
        require_market_keys(targets, "PL", ["integration_kalem_icerik"])
    with pytest.raises(MarketScopeViolation):
        require_market_keys(targets, "PL", ["PL_sezon"])


def test_the_group_locale_follows_the_localizable_key() -> None:
    seeded()
    with session_scope() as db:
        targets = write_targets(db)
    both = ["urun_adi_pl", "integration_kalem_icerik_pol"]
    assert require_market_keys(targets, "PL", both) == "pl"
    assert require_market_keys(targets, "PL", ["integration_kalem_icerik_pol"]) is None
    assert require_market_keys(targets, "CZ", ["urun_adi_cz"]) == "cs"
    assert require_market_keys(targets, "*", ["PL_sezon"]) is None
