"""Has the shop caught up — asked of a fake shop.

The push confirms against Omnitron seconds after writing. This stage confirms
against the storefront, which is a different question with a different answer:
on the first real push Omnitron had the value at 13:47 and example.com served it
at 15:52.

The properties defended here are the ones that make an unconfirmed value
harmless. Absence never marks anything, a market that cannot be read is skipped
rather than believed, and a crawl that came back short is refused outright —
because a short crawl and a shop that is not serving your value look identical
from here, and only one of them is news.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import (
    Attribute,
    AttributeColumn,
    OmnitronAttribute,
    PushBatch,
    PushItem,
)
from pim_studio.sync import publication
from pim_studio.sync.publication import confirm_published, read_served
from pim_studio.sync.storefront import STOREFRONTS, StorefrontReadFailed

pytestmark = pytest.mark.usefixtures("temp_db")

MC = "721RFA9028.TS1"
POLISH = "Elite Active damskie sneakersy niebieskie"


# ── a shop that serves what we tell it to ──────────────────────────────────


class FakeShop:
    """One market's `/list`, paged, with a `total_count` it keeps to.

    `serving` maps ModelColor to the attributes the shop returns. Anything not
    in it is simply absent, which is the case the stage must read as "not seen
    yet" rather than as a failure.
    """

    def __init__(
        self,
        serving: dict[str, dict[str, str]],
        *,
        filler: int = 0,
        understate: int = 0,
        fail: bool = False,
    ) -> None:
        self.serving = serving
        self.filler = filler
        #: Claim more rows than are returned, to fake a crawl that stopped.
        self.understate = understate
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def _rows(self) -> list[dict]:
        rows = [
            {"attributes": {"integration_model_renk": mc, **attrs}}
            for mc, attrs in self.serving.items()
        ]
        # Options the shop lists but that carry none of our keys. They pad the
        # count past the floor without ever matching anything.
        rows += [
            {"attributes": {"integration_model_renk": f"FILLER{i:06d}.X"}}
            for i in range(self.filler)
        ]
        return rows

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503, json={"detail": "down"})
        rows = self._rows()
        return httpx.Response(
            200,
            json={
                "pagination": {"num_pages": 1, "total_count": len(rows) + self.understate},
                "products": rows,
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def shop_for(*markets: str, **kw) -> dict[str, FakeShop]:
    return {m: FakeShop(**kw) for m in markets}


# ── the world ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def small_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production floor is 2,000 options; these shops sell three."""
    monkeypatch.setattr(publication, "_LISTING_FLOOR", 1)


def registry() -> int:
    with session_scope() as db:
        name = Attribute(
            key="product_name", label="Product name", write_channel="omnitron_import",
            value_grain="model_color", per_market=True,
        )
        db.add(name)
        db.flush()
        db.add_all([
            AttributeColumn(attribute_id=name.id, source="omnitron",
                            column_name="attributes__urun_adi_pl", market="PL",
                            api_writable=True),
            AttributeColumn(attribute_id=name.id, source="omnitron",
                            column_name="attributes__urun_adi_cz", market="CZ",
                            api_writable=True),
            OmnitronAttribute(pk=1, key="urun_adi_pl", name="x", data_type="text",
                              is_localizable=True),
            OmnitronAttribute(pk=2, key="urun_adi_cz", name="x", data_type="text",
                              is_localizable=True),
        ])
        return name.id


def global_registry() -> int:
    """`PL_sezon`: one copy the whole tenant reads, written under market `*`.

    Non-localizable and with no market on its column, which is what makes it
    global — and what used to make it unconfirmable, because no storefront is
    named `*`.
    """
    with session_scope() as db:
        season = Attribute(
            key="season", label="Season", write_channel="omnitron_import",
            value_grain="model_color", per_market=False,
        )
        db.add(season)
        db.flush()
        db.add_all([
            AttributeColumn(attribute_id=season.id, source="omnitron",
                            column_name="attributes__PL_sezon", market=None,
                            api_writable=True),
            OmnitronAttribute(pk=3, key="PL_sezon", name="x", data_type="text",
                              is_localizable=False),
        ])
        return season.id


def pushed(
    attr_id: int, *, market: str = "PL", key: str = "urun_adi_pl",
    value: str = POLISH, sizes: tuple[str, ...] = ("36", "37"),
    model_color: str = MC, status: str = "confirmed",
) -> int:
    """A batch that has been written, and is waiting on the shop."""
    with session_scope() as db:
        batch = PushBatch(source="ui", status="done", created_by="p@example.com")
        db.add(batch)
        db.flush()
        for size in sizes:
            db.add(PushItem(
                batch_id=batch.id, model_color=model_color, sku=f"{model_color}.{size}",
                attribute_id=attr_id, market=market, key=key, new_value=value,
                status=status,
            ))
        return batch.id


def published_count(batch_id: int) -> int:
    with session_scope() as db:
        return sum(
            1 for i in db.scalars(
                select(PushItem).where(PushItem.batch_id == batch_id)
            ) if i.published_at is not None
        )


def run(shops: dict[str, FakeShop], **kw) -> dict:
    """Run the stage with each market's shop behind its own transport."""
    real = dict(STOREFRONTS)
    clients = {m: s.client() for m, s in shops.items()}

    def fake_read(shop, keys, client=None):
        return read_served(shop, keys, client=clients[shop.market])

    with session_scope() as db:
        import pim_studio.sync.publication as mod

        original = mod.read_served
        mod.read_served = fake_read
        try:
            return confirm_published(db, **kw)
        finally:
            mod.read_served = original
            for c in clients.values():
                c.close()
            STOREFRONTS.clear()
            STOREFRONTS.update(real)


# ── what it confirms ───────────────────────────────────────────────────────


def test_a_value_the_shop_serves_is_marked_published() -> None:
    attr = registry()
    batch = pushed(attr)
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": POLISH}}, filler=2)}

    stats = run(shops)

    assert stats["published"] == 2          # both sizes of the one value
    assert stats["markets_read"] == 1
    assert published_count(batch) == 2


def test_a_value_the_shop_does_not_serve_stays_unpublished() -> None:
    """The two hours between Omnitron and the shop are this state, and it is
    not a failure — the shop is asked again next pass."""
    attr = registry()
    batch = pushed(attr)
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": "Elite Active Kadın Mavi Sneaker"}},
                            filler=2)}

    stats = run(shops)

    assert stats["published"] == 0
    assert stats["outstanding"] == 2
    assert published_count(batch) == 0


def test_whitespace_differences_still_count_as_published() -> None:
    """The shop re-wraps text. An exact match would report a published value
    as missing for ever."""
    attr = registry()
    batch = pushed(attr)
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": f"  {POLISH.replace(' ', '  ')} \n"}},
                            filler=2)}

    assert run(shops)["published"] == 2
    assert published_count(batch) == 2


def test_a_case_difference_is_not_published() -> None:
    """`urun_adi_pl` is rendered verbatim as the page title, so a value the
    shop is serving in different case is one it is not serving as we sent it."""
    attr = registry()
    batch = pushed(attr)
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": POLISH.upper()}}, filler=2)}

    assert run(shops)["published"] == 0
    assert published_count(batch) == 0


def test_only_the_market_that_wrote_is_asked_about_its_own_key() -> None:
    """A Polish value is confirmed by the Polish shop serving `urun_adi_pl`,
    never by the Czech shop serving something similar."""
    attr = registry()
    pl = pushed(attr, market="PL", key="urun_adi_pl")
    cz = pushed(attr, market="CZ", key="urun_adi_cz", value="Ceska bota")
    shops = {
        "PL": FakeShop({MC: {"urun_adi_pl": POLISH}}, filler=2),
        # The Czech shop is serving the *Polish* text under the Czech key.
        "CZ": FakeShop({MC: {"urun_adi_cz": POLISH}}, filler=2),
    }

    run(shops)

    assert published_count(pl) == 2
    assert published_count(cz) == 0


# ── a global key belongs to every shop ─────────────────────────────────────


#: Another option, carrying the key but not our value. Every shop serves it so
#: that a market serving nothing of ours is still a market that answered —
#: without it the listing floor would read an honest miss as a broken crawl,
#: which is a different test's job.
_DECOY = "764RFA0968.ZZ9"


def _four(serving: dict[str, dict[str, dict[str, str]]] | None = None) -> dict[str, FakeShop]:
    """All four markets, only the named ones serving anything of ours."""
    serving = serving or {}
    return {
        m: FakeShop({_DECOY: {"PL_sezon": "25SS"}, **serving.get(m, {})}, filler=2)
        for m in ("PL", "CZ", "SK", "HU")
    }


def test_a_global_value_is_confirmed_by_whichever_shop_serves_it() -> None:
    """`PL_sezon` is written under `*`, and `*` is not a storefront. The first
    version of this stage looked the market up in `STOREFRONTS`, found nothing
    and skipped the whole bucket on every pass — 0 confirmed out of 117
    written, lifetime, which read as a push that never landed."""
    attr = global_registry()
    batch = pushed(attr, market="*", key="PL_sezon", value="26AW")
    shops = _four({"CZ": {MC: {"PL_sezon": "26AW"}}})

    stats = run(shops)

    assert stats["global_outstanding"] == 2
    assert stats["published"] == 2
    assert published_count(batch) == 2
    assert "*" not in stats["skipped_markets"]


def test_a_matched_global_value_stops_the_crawl_there() -> None:
    """One shop serving it is the value being served, so the markets after it
    are not asked. CZ is first in order and answers for all of them."""
    attr = global_registry()
    pushed(attr, market="*", key="PL_sezon", value="26AW")
    shops = _four({"CZ": {MC: {"PL_sezon": "26AW"}}})

    run(shops)

    assert shops["CZ"].requests != []
    assert shops["HU"].requests == []
    assert shops["PL"].requests == []
    assert shops["SK"].requests == []


def test_a_market_with_its_own_items_answers_the_global_ones_too() -> None:
    """A push of Polish text and a season on the same option costs the one
    crawl it always cost — the global row rides along on PL's read."""
    attr = registry()
    season = global_registry()
    pl = pushed(attr, market="PL", key="urun_adi_pl")
    both = pushed(season, market="*", key="PL_sezon", value="26AW")
    shops = _four({"PL": {MC: {"urun_adi_pl": POLISH, "PL_sezon": "26AW"}}})

    run(shops)

    assert published_count(pl) == 2
    assert published_count(both) == 2
    assert shops["CZ"].requests == []


def test_a_global_value_no_shop_serves_stays_unpublished_without_being_skipped() -> None:
    """Every market is asked and none of them serves it. That is "not seen
    yet", the same as any other market's miss — not a bucket nothing looked
    at, which is what it used to be."""
    attr = global_registry()
    batch = pushed(attr, market="*", key="PL_sezon", value="26AW")
    shops = _four()

    stats = run(shops)

    assert stats["published"] == 0
    assert stats["markets_read"] == 4
    assert stats["skipped_markets"] == {}
    assert published_count(batch) == 0


# ── what it refuses to conclude ────────────────────────────────────────────


def test_a_market_that_cannot_be_read_is_skipped_and_its_items_untouched() -> None:
    attr = registry()
    batch = pushed(attr)
    shops = {"PL": FakeShop({}, fail=True)}

    stats = run(shops)

    assert stats["published"] == 0
    assert stats["markets_read"] == 0
    assert "PL" in stats["skipped_markets"]
    assert published_count(batch) == 0


def test_a_short_crawl_is_refused_rather_than_read_as_absence() -> None:
    """The shop says it has 500 options and returns 3. Every value would read
    as unpublished, which is the wrong answer given confidently."""
    attr = registry()
    batch = pushed(attr)
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": POLISH}}, filler=2, understate=500)}

    stats = run(shops)

    assert stats["published"] == 0
    assert "PL" in stats["skipped_markets"]
    assert "too short" in stats["skipped_markets"]["PL"]
    assert published_count(batch) == 0


def test_a_wrong_cookie_sized_listing_is_refused(monkeypatch) -> None:
    """Below the floor means the market selector failed and another channel's
    smaller range came back — not that our range collapsed.

    The only test that wants the production floor, so it puts it back over the
    autouse fixture that lowers it for everything else."""
    monkeypatch.setattr(publication, "_LISTING_FLOOR", 2_000)
    attr = registry()
    pushed(attr)
    shop = FakeShop({MC: {"urun_adi_pl": POLISH}})
    with pytest.raises(StorefrontReadFailed, match="below the floor"), shop.client() as c:
        read_served(STOREFRONTS["PL"], {"urun_adi_pl"}, client=c)


def test_nothing_outstanding_asks_no_shop_at_all() -> None:
    """On a day nobody pushed this is one query and no network."""
    registry()
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": POLISH}}, filler=2)}

    stats = run(shops)

    assert stats == {
        "outstanding": 0, "published": 0, "markets_read": 0,
        "global_outstanding": 0, "skipped_markets": {},
    }
    assert shops["PL"].requests == []


def test_an_item_that_never_landed_is_not_asked_about() -> None:
    """A skipped or failed item was never written, so the shop serving
    something else about it means nothing."""
    attr = registry()
    batch = pushed(attr, status="skipped")
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": POLISH}}, filler=2)}

    assert run(shops)["outstanding"] == 0
    assert published_count(batch) == 0


def test_an_already_published_item_keeps_its_first_timestamp() -> None:
    attr = registry()
    batch = pushed(attr)
    shops = {"PL": FakeShop({MC: {"urun_adi_pl": POLISH}}, filler=2)}

    run(shops)
    with session_scope() as db:
        first = [i.published_at for i in db.scalars(
            select(PushItem).where(PushItem.batch_id == batch).order_by(PushItem.id)
        )]

    run(shops)  # asked again; nothing outstanding, nothing rewritten
    with session_scope() as db:
        again = [i.published_at for i in db.scalars(
            select(PushItem).where(PushItem.batch_id == batch).order_by(PushItem.id)
        )]

    assert first == again
