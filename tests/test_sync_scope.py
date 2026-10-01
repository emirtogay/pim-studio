"""The four-channel hard line — and that it cannot be crossed by accident.

Decided 2026-08-14: we pull from PL, CZ, HU and SK and ignore the tenant's
other 22 channels. Like the read-only rule, it is only worth something because
it is enforced rather than agreed to. These tests are that enforcement's
enforcement.
"""

from __future__ import annotations

import pytest

from pim_studio.sync import scope
from pim_studio.sync.scope import MarketScopeViolation

# ── the allowlist is exactly four markets ──────────────────────────────────


def test_exactly_our_four_markets() -> None:
    assert set(scope.MARKETS) == {"PL", "CZ", "HU", "SK"}


def test_ids_read_from_the_live_tenant_on_2026_08_14() -> None:
    """Pinned so a careless edit to the table is a failing test, not a silent
    repoint to another market's data."""
    assert scope.MARKETS["PL"].catalog_pk == 4
    assert scope.MARKETS["CZ"].catalog_pk == 40
    assert scope.MARKETS["HU"].catalog_pk == 41
    assert scope.MARKETS["SK"].catalog_pk == 172
    # The pk space overlaps across resource types — SK's price list and HU's
    # stock list are both 167. Resolving by name would have produced a
    # plausible, wrong map; this is the case that proves it.
    assert scope.MARKETS["SK"].price_list_pk == 167
    assert scope.MARKETS["HU"].stock_list_pk == 167


def test_pl_channel_is_recorded_inactive() -> None:
    """`SHOP PL` is is_active:false upstream while the control table shows
    12,867 live PL SKUs. Recorded, not acted on — but recorded, so a query
    returning nothing reads as a fact about the tenant rather than our bug."""
    assert scope.MARKETS["PL"].channel_is_active is False
    assert all(m.channel_is_active for m in scope.MARKETS.values() if m.code != "PL")


# ── out-of-scope ids are refused ───────────────────────────────────────────


@pytest.mark.parametrize("catalog", [1, 3, 6, 304, 27])
def test_foreign_catalogues_are_refused(catalog: int) -> None:
    """TR (1), UA (3), N11 (6), Allegro PL (304) — all real catalogues in the
    tenant, none of them ours.

    601 used to be on this list. It was moved off on 2026-09-07 when PL and CZ
    turned out to be served from it, and HU and SK followed on 2026-09-16 —
    see `Market.shared_catalog_pks`.
    """
    with pytest.raises(MarketScopeViolation):
        scope.require_catalog(catalog)


def test_the_shared_catalogue_is_ours_and_belongs_to_no_single_market() -> None:
    """601 passes the scope guard, because a read of it is inside our markets.
    It still cannot answer "which market", and saying so is the point: picking
    the first match would be a silent wrong answer of exactly the kind this
    module exists to prevent."""
    assert scope.require_catalog(601) == 601
    assert [m.code for m in scope.markets_for_catalog(601)] == ["PL", "CZ", "HU", "SK"]
    with pytest.raises(MarketScopeViolation) as e:
        scope.market_for_catalog(601)
    assert "shared" in str(e.value) and "PL" in str(e.value)


def test_every_market_reads_the_shared_catalogue() -> None:
    """PL and CZ since 2026-09-07. HU and SK were held off it while their own
    catalogues matched their shops to 99.7%; on 2026-09-16 40 of 40 sampled
    options on each shop were in 601 and 4 HU and 3 SK were in 601 alone."""
    for code in ("PL", "CZ", "HU", "SK"):
        assert scope.MARKETS[code].shared_catalog_pks == (601,), code


def test_moving_onto_601_did_not_move_a_market_s_own_ids() -> None:
    """Additive, not a repoint: a market's own catalogue still defines its
    price list, stock list and category tree, and `verify_against_live` still
    checks them there."""
    assert (scope.MARKETS["HU"].catalog_pk, scope.MARKETS["HU"].price_list_pk) == (41, 135)
    assert (scope.MARKETS["SK"].catalog_pk, scope.MARKETS["SK"].price_list_pk) == (172, 167)
    # Recorded as the tenant has them. Like PL's flag, not acted on.
    assert scope.MARKETS["HU"].channel_is_active is True
    assert scope.MARKETS["SK"].channel_is_active is True


@pytest.mark.parametrize("channel", [1, 103, 299, 596])
def test_foreign_channels_are_refused(channel: int) -> None:
    with pytest.raises(MarketScopeViolation):
        scope.require_channel(channel)


def test_kz_price_list_is_refused_despite_being_declared_by_sk() -> None:
    """SK's catalogue lists KZ_Price_List (200) under extra_price_lists.
    Being reachable from one of our catalogues does not put it in scope."""
    assert 200 in scope.EXTRA_PRICE_LISTS_IGNORED
    assert 200 not in scope.ALLOWED_PRICE_LISTS
    with pytest.raises(MarketScopeViolation):
        scope.require_price_list(200)


def test_our_own_ids_pass() -> None:
    for m in scope.MARKETS.values():
        assert scope.require_catalog(m.catalog_pk) == m.catalog_pk
        assert scope.require_channel(m.channel_pk) == m.channel_pk
        assert scope.require_price_list(m.price_list_pk) == m.price_list_pk
        assert scope.require_stock_list(m.stock_list_pk) == m.stock_list_pk


# ── the missing-filter case, which is the dangerous one ────────────────────


def test_unscoped_query_is_refused() -> None:
    """The failure this guards is not a wrong id — it is no id at all.

    Omnitron ignores query parameters it does not recognise and returns the
    full tenant with HTTP 200, so a misspelled scope filter looks exactly like
    a successful query. Verified 2026-08-14: nine candidate product filters
    each returned all 288,990 rows.
    """
    with pytest.raises(MarketScopeViolation) as e:
        scope.require_scoped({"limit": 100}, scope_keys=("catalog",))
    assert "unscoped" in str(e.value)


def test_scoped_query_passes_through_unchanged() -> None:
    params = {"catalog": 4, "limit": 100}
    assert scope.require_scoped(params, scope_keys=("catalog",)) == params


def test_scoped_query_with_a_foreign_id_is_refused() -> None:
    with pytest.raises(MarketScopeViolation):
        scope.require_scoped({"catalog": 3}, scope_keys=("catalog",))


def test_comma_joined_ids_are_each_checked() -> None:
    """One good id must not launder a bad one riding alongside it."""
    assert scope.require_scoped({"catalog": "4,40"}, scope_keys=("catalog",))
    assert scope.require_scoped({"catalog": "4,601"}, scope_keys=("catalog",))
    with pytest.raises(MarketScopeViolation):
        scope.require_scoped({"catalog": "4,3"}, scope_keys=("catalog",))


# ── drift against the live tenant ──────────────────────────────────────────


class FakeApi:
    """Stands in for OmnitronClient, returning whatever the test wants.

    Carries channels as well as catalogues because the guard now asks two
    different questions — do our ids still mean what they meant, and has the
    tenant grown a live channel we have no model for.
    """

    def __init__(
        self, catalogs: dict[int, dict], channels: list[dict] | None = None
    ) -> None:
        self._catalogs = catalogs
        self._channels = _clean_channels() if channels is None else channels

    def get(self, path: str, params: dict | None = None) -> dict:
        pk = int(path.rstrip("/").rsplit("/", 1)[-1])
        return self._catalogs[pk]

    def iter_records(self, path: str, params=None, page_size: int = 100):
        assert "channels" in path, path
        return iter(self._channels)


def _clean_channels() -> list[dict]:
    """Our four, active, plus the marketplaces we deliberately ignore."""
    ours = [
        {"pk": m.channel_pk, "name": m.name, "catalog": m.catalog_pk,
         "is_active": True}
        for m in scope.MARKETS.values()
    ]
    foreign = [
        {"pk": pk, "name": note.split(" —")[0], "catalog": 999, "is_active": True}
        for pk, note in scope.KNOWN_FOREIGN_CHANNELS.items()
    ]
    return ours + foreign


def _live_shape() -> dict[int, dict]:
    shape = {
        m.catalog_pk: {
            "price_list": m.price_list_pk,
            "stock_list": m.stock_list_pk,
            "category_tree": m.category_tree_pk,
            "extra_price_lists": list(m.extra_price_list_pks),
        }
        for m in scope.MARKETS.values()
    }
    # The shared catalogue, as the live tenant has it: one catalogue carrying
    # every market's price list among its extras, which is what makes it serve
    # them at all.
    for pk in scope.SHARED_CATALOGS:
        shape[pk] = {
            "price_list": 1,
            "stock_list": 68,
            "category_tree": 629,
            "extra_price_lists": [134, 35, 2, 200, 135, 167],
        }
    return shape


def test_no_drift_when_the_tenant_matches() -> None:
    assert scope.verify_against_live(FakeApi(_live_shape())) == []  # type: ignore[arg-type]


def test_drift_is_reported_when_a_catalogue_is_repointed() -> None:
    """The scenario: someone repoints SK at a different price list. Every id in
    scope.py still resolves, so every stage would keep succeeding — while
    reading another market's prices. This is the check that makes it loud."""
    shape = _live_shape()
    shape[172]["price_list"] = 999
    drift = scope.verify_against_live(FakeApi(shape))  # type: ignore[arg-type]
    assert len(drift) == 1
    assert "SK" in drift[0] and "999" in drift[0]


def test_drift_is_reported_when_an_extra_price_list_appears() -> None:
    shape = _live_shape()
    shape[4]["extra_price_lists"] = [200]
    drift = scope.verify_against_live(FakeApi(shape))  # type: ignore[arg-type]
    assert len(drift) == 1
    assert "PL" in drift[0]


# ── the channel nobody modelled ────────────────────────────────────────────


def test_a_new_active_channel_is_reported_as_drift() -> None:
    """The eight-month miss, as a test.

    `Northwind Global` was created in January 2026, pointed at catalogue 601, and
    carried most of what example.com sells. Every id in scope.py still resolved,
    so `verify_against_live` reported clean every night — it only ever re-read
    the four catalogues we already named.
    """
    channels = _clean_channels() + [
        {"pk": 596, "name": "Northwind Global", "catalog": 601, "is_active": True}
    ]
    drift = scope.verify_against_live(FakeApi(_live_shape(), channels))  # type: ignore[arg-type]
    assert len(drift) == 1
    assert "596" in drift[0] and "601" in drift[0]


def test_an_inactive_unknown_channel_is_not_drift() -> None:
    """`SHOP PL` and `SHOP CZE` are themselves inactive and are still the
    catalogues we read, so inactivity is not evidence by itself. Reporting on
    it would make the guard cry wolf about our own markets."""
    channels = _clean_channels() + [
        {"pk": 900, "name": "Something retired", "catalog": 901, "is_active": False}
    ]
    assert scope.verify_against_live(FakeApi(_live_shape(), channels)) == []  # type: ignore[arg-type]


def test_the_marketplaces_are_classified_rather_than_reported_every_night() -> None:
    """A guard that fires on twelve known-irrelevant channels every run is a
    guard nobody reads. They are named in KNOWN_FOREIGN_CHANNELS with reasons."""
    assert scope.verify_against_live(FakeApi(_live_shape())) == []  # type: ignore[arg-type]
    assert 1 in scope.KNOWN_FOREIGN_CHANNELS, "SHOP TR"
    assert 103 in scope.KNOWN_FOREIGN_CHANNELS, "SHOP UKR is out of scope by decision"
    assert not (set(scope.KNOWN_FOREIGN_CHANNELS) & scope.ALLOWED_CHANNELS), (
        "a channel cannot be both ours and deliberately foreign"
    )


def test_drift_when_the_shared_catalogue_stops_carrying_a_market() -> None:
    """601 serves PL because it lists PL's price list among its extras. Drop
    that and PL is no longer sold from it — while every id still resolves and
    every stage keeps succeeding against a membership that has become
    fiction."""
    shape = _live_shape()
    shape[601]["extra_price_lists"] = [134, 135, 167]  # PL's list 2 removed
    drift = scope.verify_against_live(FakeApi(shape))  # type: ignore[arg-type]
    assert len(drift) == 1
    assert "PL" in drift[0] and "601" in drift[0]
