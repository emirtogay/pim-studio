"""Demo mode: the switch, the mocks behind it, and the seed that fills a database.

Three properties are defended. With `DEMO_MODE` off, every factory in
`integrations` builds exactly what its call site used to build — demo mode must
cost the real deployment nothing. With it on, every external system is a mock
and nothing reaches the network. And the seed is deterministic: two fresh
databases end up with the same rows, and a second run changes nothing.

Everything runs against the throwaway database from `conftest.temp_db` and a
temporary uploads folder. The global settings object is mutated by demo mode,
so every field it touches is registered with `monkeypatch` first and restored
after each test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from pim_studio import db as db_module
from pim_studio import demo, integrations
from pim_studio.brandlibrary.client import BrandLibraryClient
from pim_studio.brandlibrary.photoserver import PhotoServer
from pim_studio.config import DEMO_DEFAULTS, settings
from pim_studio.db import session_scope
from pim_studio.demo import catalog as cat
from pim_studio.models import AttributeValue, Base, Product, PublishedImage, User
from pim_studio.omnitron.client import OmnitronClient

#: Small enough to keep the seed quick, large enough that every shop lists
#: more options than the demo's storefront floor.
SIZE = 120


@pytest.fixture
def isolated(tmp_path: Path) -> Iterator[Path]:
    """Every setting demo mode can change, put back afterwards; files in tmp.

    Saved and restored through the model's own storage rather than with
    `monkeypatch.setattr`: assigning a field marks it as explicitly set, and
    demo mode treats an explicit value as the environment's choice.
    """
    saved = dict(settings.__dict__)
    explicit = set(settings.model_fields_set)
    settings.__dict__["uploads_root"] = tmp_path / "uploads"
    settings.__dict__["bundles_root"] = tmp_path / "bundles"
    demo.reset()
    try:
        yield tmp_path
    finally:
        demo.reset()
        settings.__dict__.clear()
        settings.__dict__.update(saved)
        object.__setattr__(settings, "__pydantic_fields_set__", explicit)


@pytest.fixture
def demo_on(isolated: Path) -> Path:
    settings.enable_demo_mode()
    return isolated


def _fresh_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `session_scope` at a new, empty SQLite file — `temp_db`, again."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )


def _row_counts() -> dict[str, int]:
    with session_scope() as db:
        return {
            table.name: db.scalar(select(func.count()).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }


def _sync(*stages: str) -> None:
    from pim_studio.sync.__main__ import STAGES

    with integrations.pim_client() as api:
        for stage in stages:
            fn, _ = STAGES[stage]
            assert fn(api, True) == 0, stage


# ── the switch ─────────────────────────────────────────────────────────────


def test_every_factory_builds_the_real_client_when_demo_mode_is_off(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "omnitron_base_url", "https://pim.example.test")
    monkeypatch.setattr(settings, "omnitron_username", "u")
    monkeypatch.setattr(settings, "omnitron_password", "p")
    assert not settings.demo_mode

    with integrations.pim_client() as api:
        assert type(api) is OmnitronClient
    library = integrations.brand_library_client(cookie="SESS=abc", domain="assets.example.test")
    assert type(library) is BrandLibraryClient
    server = integrations.photo_server(host="ftp.example.test", user="u", password="p")
    assert type(server) is PhotoServer
    with integrations.http_client() as client:
        assert not isinstance(client._transport, httpx.MockTransport)


def test_a_patched_class_at_the_call_site_still_wins(isolated: Path) -> None:
    """Call sites pass the name they import, so their tests can replace it."""
    sentinel = object()
    assert integrations.pim_client(real=lambda: sentinel) is sentinel  # type: ignore[arg-type,return-value]


def test_every_factory_builds_a_mock_in_demo_mode(demo_on: Path) -> None:
    with integrations.pim_client() as api:
        assert isinstance(api, demo.MockPim)
        # The real client underneath, with only the transport swapped.
        assert isinstance(api, OmnitronClient)
        assert api.get("/api/v1/catalogs/601/")["pk"] == 601
    library = integrations.brand_library_client()
    assert isinstance(library, demo.MockLibrary)
    assert library.test()[0] is True
    assert isinstance(integrations.photo_server(remote_dir="pim-studio"), demo.MockPhotoServer)
    with integrations.http_client() as client:
        assert isinstance(client._transport, httpx.MockTransport)


def test_demo_http_answers_the_demo_hosts_and_refuses_everything_else(demo_on: Path) -> None:
    listing = integrations.http_get(
        "https://northwind-pl.example.test/list",
        params={"format": "json", "page_size": 100, "page": 1},
    )
    assert listing.status_code == 200
    assert listing.json()["pagination"]["total_count"] > 0

    elsewhere = integrations.http_get("https://www.example.com/")
    assert elsewhere.status_code == 502


def test_demo_mode_fills_blank_settings_and_keeps_explicit_ones(isolated: Path) -> None:
    from pim_studio.config import Settings

    configured = Settings(demo_mode=True, omnitron_base_url="", omnitron_writes_enabled=False)
    assert configured.omnitron_base_url == DEMO_DEFAULTS["omnitron_base_url"]
    assert configured.omnitron_writes_enabled is False
    assert configured.storefront_listing_floor == DEMO_DEFAULTS["storefront_listing_floor"]
    assert Settings().storefront_listing_floor is None


# ── the mocks, driven by the application's own code ───────────────────────


@pytest.mark.usefixtures("temp_db")
def test_the_sync_against_the_mock_pim_fills_products(demo_on: Path) -> None:
    from pim_studio import seed as registry_seed

    registry_seed.run()
    world = demo.World(cat.build(SIZE), day=cat.CURRENT_DAY)
    with demo.use(world):
        _sync("registry", "catalogue", "products", "values")

    expected = {
        sku.sku
        for option in world.catalog.options
        if option.catalogs[cat.CURRENT_DAY]
        for sku in option.sizes
        if not sku.deleted_day2
    }
    with session_scope() as db:
        skus = set(db.scalars(select(Product.sku)))
        named = db.scalar(
            select(func.count()).select_from(AttributeValue).where(
                AttributeValue.state == "live"
            )
        )
    assert skus == expected
    assert named and named > len(expected)


@pytest.mark.usefixtures("temp_db")
def test_a_demo_publish_completes_and_lands_files_in_mock_ftp(demo_on: Path) -> None:
    from pim_studio import seed as registry_seed
    from pim_studio.admin import images_api
    from pim_studio.brandlibrary.targets import find_targets

    registry_seed.run()
    with demo.use(demo.World(cat.build(SIZE), day=1)):
        _sync("catalogue", "products", "images")
        with session_scope() as db:
            codes = [t.model_color for t in find_targets(db).targets]
        assert codes, "the catalogue leaves some options without a photo"
        images_api._publish_in_background(codes, "demo", dry_run=False)

    folder = demo_on / "uploads" / "mock-ftp" / settings.ftp_remote_dir
    files = sorted(p.name for p in folder.iterdir())
    with session_scope() as db:
        rows = list(db.scalars(select(PublishedImage)))
    assert files and len(files) == len(rows)
    assert {r.remote_name for r in rows} == set(files)
    # The public address answers with what was uploaded.
    first = rows[0]
    assert first.public_url.startswith("https://photos.example.test/")
    fetched = integrations.http_get(first.public_url)
    assert fetched.status_code == 200
    assert fetched.content == (folder / first.remote_name).read_bytes()
    assert fetched.content[:3] == b"\xff\xd8\xff", "a JPEG"


# ── the seed ───────────────────────────────────────────────────────────────


def test_the_seed_is_deterministic_and_a_second_run_changes_nothing(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pim_studio.demo import seed

    _fresh_database(isolated / "first.db", monkeypatch)
    report = seed.run(size=SIZE, out_dir=isolated / "static-a")
    assert report.seeded
    first = _row_counts()
    assert first["products"] and first["sync_runs"] > 300
    assert first["value_conflicts"] and first["catalogue_takedowns"] == 2
    assert first["push_batches"] == 1 and first["published_images"]
    assert {"products.json", "details.json", "coverage.json", "changelog.json"} <= {
        p.name for p in (isolated / "static-a").glob("*.json")
    }
    with session_scope() as db:
        assert db.scalar(select(User.role).where(User.email == seed.DEMO_LOGIN)) == "developer"
    # A web server started afterwards builds its mocks from the same catalogue.
    with integrations.pim_client() as api:
        assert len(api.world.catalog.options) == SIZE

    again = seed.run(size=cat.DEFAULT_SIZE, out_dir=isolated / "static-a")
    assert not again.seeded
    assert _row_counts() == first

    demo.reset()
    _fresh_database(isolated / "second.db", monkeypatch)
    seed.run(size=SIZE, out_dir=isolated / "static-b")
    assert _row_counts() == first


def test_the_seed_refuses_a_database_it_did_not_write(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pim_studio.demo import seed

    _fresh_database(isolated / "real.db", monkeypatch)
    with session_scope() as db:
        db.add(Product(sku="XX0001.001.M", base_code_format="XX0001.001.M",
                       model_color="XX0001.001", base_code="XX0001", size="M"))
    with pytest.raises(seed.SeedRefused):
        seed.run(size=SIZE, out_dir=isolated / "static")
