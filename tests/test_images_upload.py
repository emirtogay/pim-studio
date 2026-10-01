"""Publishing photos for a list somebody uploaded.

The property this file exists to defend: **an uploaded list is obeyed.**
`POST /publish` asks the database who needs a photo and is entitled to set
aside options merch removed and options Turkey already has. `POST
/publish/upload` is handed the answer, and filtering it would silently discard
a decision somebody made by hand — a season opening, a request from Turkey,
forty codes a manager sent. What the weekly run *would* have skipped is
reported instead, so a count that surprises has an explanation on screen.

The second property is that the list works when the counts do not. Everything
else on the Images page is blocked until something has read Omnitron's image
column, because until then "has no photo" cannot be told from "nobody looked".
A named list never asks that question.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import app as app_module
from pim_studio.admin.auth import current_user
from pim_studio.db import session_scope
from pim_studio.models import (
    PRODUCT_META,
    Attribute,
    AttributeValue,
    BanComment,
    BrandLibrarySession,
    CatalogueMembership,
    Product,
    PublishedImage,
    SyncRun,
    User,
)

pytestmark = pytest.mark.usefixtures("temp_db")

UPLOAD = "/api/images/publish/upload"


def _as(role: str = "planning") -> None:
    app_module.app.dependency_overrides[current_user] = lambda: User(
        id=1, email="t@example.com", role=role, name="T", is_active=True
    )


@pytest.fixture
def client() -> TestClient:
    _as()
    try:
        yield TestClient(app_module.app, raise_server_exceptions=False)
    finally:
        app_module.app.dependency_overrides.clear()


@pytest.fixture
def started(monkeypatch) -> list[dict]:
    """Stand in for everything the endpoint needs to get as far as a thread.

    Returns the list the background run would have been given. The run itself
    is replaced — it reads a thousand pages and moves a gigabyte, and none of
    that is what these tests are about.
    """
    from pim_studio.admin import images_api

    calls: list[dict] = []

    monkeypatch.setattr(
        images_api,
        "_configured",
        lambda db: {
            "library": True,
            "photoServer": True,
            "ready": True,
            "mode": "oauth",
            "cookie": None,
        },
    )
    monkeypatch.setattr(
        images_api.BrandLibraryClient, "test", lambda self: (True, "48 210 photos visible")
    )
    monkeypatch.setattr(
        images_api,
        "_publish_in_background",
        lambda codes, actor, dry_run, **kw: calls.append(
            {"codes": codes, "actor": actor, "dryRun": dry_run, **kw}
        ),
    )
    with session_scope() as db:
        db.add(BrandLibrarySession(id=1, cookie="SESS=test"))
    return calls


def _registry() -> None:
    with session_scope() as db:
        for key in ("image_0", "pl_comment"):
            db.add(
                Attribute(
                    key=key,
                    label=key,
                    data_type="text",
                    write_channel="read_only",
                    value_grain="model_color",
                )
            )


def _option(model_color: str, *, in_catalogue: bool = True, comment: str | None = None) -> None:
    with session_scope() as db:
        product = Product(
            sku=f"{model_color}.T04",
            base_code_format=f"{model_color}.T04",
            model_color=model_color,
            base_code=model_color.split(".")[0],
        )
        db.add(product)
        db.flush()
        if in_catalogue:
            db.add(CatalogueMembership(product_id=product.id, market="PL", catalog_pk=1))
        if comment is not None:
            attribute = db.query(Attribute).filter_by(key="pl_comment").one()
            db.add(
                AttributeValue(
                    entity_kind="model_color",
                    entity_key=model_color,
                    attribute_id=attribute.id,
                    market="*",
                    value=comment,
                    state="live",
                    entry_grain="model_color",
                    source_kind="omnitron_api",
                )
            )


def _banned_comment(text: str) -> None:
    with session_scope() as db:
        db.add(BanComment(comment=text, bans=True))


def _published(model_color: str) -> None:
    with session_scope() as db:
        db.add(
            PublishedImage(
                model_color=model_color,
                brandlibrary_key=model_color.replace(".", "_"),
                asset_name=f"{model_color}_01",
                edition="original",
                remote_name=f"{model_color}_01.jpg",
                public_url=f"https://photos.example/pim-studio/{model_color}_01.jpg",
                bytes=2048,
            )
        )


def _image_sync_ran() -> None:
    with session_scope() as db:
        db.add(
            SyncRun(
                kind="omnitron_images",
                started_at=datetime.now(UTC) - timedelta(hours=2),
                finished_at=datetime.now(UTC) - timedelta(hours=1),
                status="success",
            )
        )


def _workbook(header: str, codes: list[str]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Season"
    sheet.append(["Order", header, "Qty"])
    for index, code in enumerate(codes, start=1):
        sheet.append([index, code, 10])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _paste(client: TestClient, text: str, dry_run: bool = False):
    return client.post(UPLOAD, data={"text": text, "dry_run": str(dry_run).lower()})


# ── the list is obeyed ─────────────────────────────────────────────────────


def test_a_pasted_list_publishes_exactly_those_codes(client, started) -> None:
    _registry()
    _option("AF0276.J2G")
    _option("L3558.132")
    _option("TH2568.144")

    body = _paste(client, "AF0276.J2G\nL3558.132").json()

    assert body["started"] is True
    assert body["options"] == 2
    # TH2568.144 needs a photo too, and was not asked for.
    assert started[0]["codes"] == ["AF0276.J2G", "L3558.132"]


def test_codes_the_weekly_run_would_skip_are_published_and_reported(client, started) -> None:
    """The whole point. Three reasons to skip, all overridden, all named."""
    _registry()
    _banned_comment("wycofany")
    _option("AF0276.J2G", comment="wycofany")
    _option("L3558.132")
    _published("L3558.132")
    _option("3196.130", in_catalogue=False)

    body = _paste(client, "AF0276.J2G, L3558.132, 3196.130").json()

    assert body["options"] == 3
    assert sorted(started[0]["codes"]) == ["3196.130", "AF0276.J2G", "L3558.132"]
    assert body["overrides"]["banned"] == ["AF0276.J2G"]
    assert body["overrides"]["published"] == ["L3558.132"]
    assert body["overrides"]["outOfCatalogue"] == ["3196.130"]


def test_a_code_held_only_as_a_grouping_row_is_reported_out_of_catalogue(
    client, started
) -> None:
    """Same population as the page: a grouping row in a catalogue is not an
    option in one. It is still searched, because the list says so."""
    _registry()
    _option("EJ1772.QDS")
    with session_scope() as db:
        product = db.query(Product).filter_by(model_color="EJ1772.QDS").one()
        product.omnitron_product_type = PRODUCT_META

    body = _paste(client, "EJ1772.QDS").json()

    assert body["options"] == 1
    assert started[0]["codes"] == ["EJ1772.QDS"]
    assert body["overrides"]["outOfCatalogue"] == ["EJ1772.QDS"]


def test_a_list_works_before_any_image_sync_has_run(client, started) -> None:
    """`/status` is blocked in this state and this is not, deliberately.

    The guard exists because "has no image on record" is meaningless until the
    image column has been read. A list of codes never asks that question, so
    refusing here would be borrowing a guard for a question it does not answer.
    """
    _registry()
    _option("AF0276.J2G")

    assert client.get("/api/images/status").json()["blocked"] is not None

    body = _paste(client, "AF0276.J2G").json()

    assert body["started"] is True
    assert body["options"] == 1


def test_duplicates_are_collapsed_and_counted(client, started) -> None:
    """Two sources pasted together is the usual cause, and worth saying."""
    _registry()
    _option("AF0276.J2G")

    body = _paste(client, "AF0276.J2G\nAF0276.J2G\naf0276.j2g").json()

    assert body["options"] == 1
    assert body["duplicates"] == 2
    assert started[0]["codes"] == ["AF0276.J2G"]


# ── reading the file ───────────────────────────────────────────────────────


def test_a_workbook_resolves_the_column_by_what_matches(client, started) -> None:
    """The header is not read. `Ref` beats `Order` and `Qty` on evidence."""
    _registry()
    _option("AF0276.J2G")
    _option("L3558.132")
    data = _workbook("Ref", ["AF0276.J2G", "L3558.132"])

    response = client.post(
        UPLOAD,
        files={"file": ("season.xlsx", data, "application/vnd.ms-excel")},
        data={"dry_run": "false"},
    )
    body = response.json()

    assert body["options"] == 2
    assert body["source"] == "season.xlsx"
    assert body["column"] == "Ref"
    assert body["sheet"] == "Season"


def test_the_run_records_which_file_it_came_from(client, started) -> None:
    """`sync_runs.source_file` is how a surprising run is traced back later."""
    _registry()
    _option("AF0276.J2G")
    data = _workbook("Ref", ["AF0276.J2G"])

    client.post(UPLOAD, files={"file": ("turkey-request.xlsx", data, "application/vnd.ms-excel")})

    assert started[0]["source_file"] == "turkey-request.xlsx"


def test_codes_we_hold_no_product_for_are_searched_anyway(client, started) -> None:
    """The brand library, not our product table, decides what has a photo.

    Omnitron's catalogue is the right oracle for finding which column holds the
    codes and the wrong one for deciding what to look for: the library holds
    photos for options PL has never carried, which is frequently the whole
    reason somebody uploads a list.
    """
    _registry()
    _option("AF0276.J2G")

    body = _paste(client, "AF0276.J2G\nBH2292.166").json()

    assert body["options"] == 2
    assert body["notOnRecord"] == ["BH2292.166"]
    assert sorted(started[0]["codes"]) == ["AF0276.J2G", "BH2292.166"]
    # And reported once, not twice. A code with no product row is trivially in
    # no live catalogue, so listing it under both headings would say the weaker
    # thing louder.
    assert body["overrides"]["outOfCatalogue"] == []


def test_things_that_are_not_codes_are_dropped_and_named(client, started) -> None:
    """The shape test is the only exclusion, so it has to be visible.

    A stray heading in the codes column would otherwise be searched, fail, and
    be written down as a photo the brand has never shot.
    """
    _registry()
    _option("AF0276.J2G")

    body = _paste(client, "AF0276.J2G, notes for the buyer, Option").json()

    assert body["options"] == 1
    assert sorted(body["unreadable"]) == ["Option", "notes for the buyer"]
    assert started[0]["codes"] == ["AF0276.J2G"]


def test_a_size_suffixed_article_is_accepted_as_its_model_color(client, started) -> None:
    """Order files carry `STYLE.COLOUR.SIZE`; the photo is per ModelColor."""
    _registry()
    _option("AF0276.J2G")

    body = _paste(client, "TH2568.144.T04").json()

    assert body["options"] == 1
    assert started[0]["codes"] == ["TH2568.144"]


def test_a_file_with_no_recognisable_codes_is_refused(client, started) -> None:
    _registry()
    _option("AF0276.J2G")

    response = _paste(client, "banana\ncarrot")

    assert response.status_code == 400
    assert "matched a product we hold" in response.json()["detail"]


def test_sending_neither_a_file_nor_text_is_refused(client, started) -> None:
    _registry()
    _option("AF0276.J2G")

    response = client.post(UPLOAD, data={"dry_run": "false"})

    assert response.status_code == 400
    assert "Upload a file or paste" in response.json()["detail"]


def test_a_dry_run_is_carried_through(client, started) -> None:
    _registry()
    _option("AF0276.J2G")

    body = _paste(client, "AF0276.J2G", dry_run=True).json()

    assert body["dryRun"] is True
    assert started[0]["dryRun"] is True


# ── gating, and the failures worth naming ──────────────────────────────────


def test_uploading_requires_a_session() -> None:
    app_module.app.dependency_overrides.clear()
    anonymous = TestClient(app_module.app, raise_server_exceptions=False)

    assert anonymous.post(UPLOAD).status_code == 401


def test_a_manager_cannot_publish_a_list(client) -> None:
    """Same audience as `Publish now` — a list is not a lesser action."""
    _as("manager")

    assert client.post(UPLOAD, data={"text": "AF0276.J2G"}).status_code == 403


def test_an_unreachable_library_is_refused_before_the_thread(client, monkeypatch) -> None:
    from pim_studio.admin import images_api

    monkeypatch.setattr(
        images_api,
        "_configured",
        lambda db: {
            "library": True,
            "photoServer": True,
            "ready": True,
            "mode": "cookie",
            "cookie": {},
        },
    )
    monkeypatch.setattr(
        images_api.BrandLibraryClient,
        "test",
        lambda self: (
            False,
            "GET /api/v4/media/ failed: [SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate",
        ),
    )
    with session_scope() as db:
        db.add(BrandLibrarySession(id=1, cookie="SESS=test"))
    _registry()
    _option("AF0276.J2G")

    response = _paste(client, "AF0276.J2G")

    assert response.status_code == 502
    detail = response.json()["detail"]
    # Intercepted, not blocked — the two have different fixes.
    assert "intercepted" in detail
    assert "pim-studio-images.sh" in detail


def test_a_second_run_is_refused_while_one_is_in_flight(client, started) -> None:
    from pim_studio.admin import images_api

    _registry()
    _option("AF0276.J2G")
    images_api._running.acquire()
    try:
        response = _paste(client, "AF0276.J2G")
    finally:
        images_api._running.release()

    assert response.status_code == 409
    assert "already in progress" in response.json()["detail"]


def test_starting_a_list_run_is_recorded(client, started) -> None:
    """It moves a gigabyte and feeds a bundle another company's team uploads."""
    from pim_studio.models import AuditLog

    _registry()
    _option("AF0276.J2G")

    _paste(client, "AF0276.J2G")

    with session_scope() as db:
        entry = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert entry is not None
    assert entry.action == "started an image publish from a list"
    assert "1 options from pasted" in (entry.detail or "")
