"""The image endpoints.

Two properties matter more than the rest and are asserted hardest:

`status` must render the day-one screen rather than a number. Every count here
is meaningless until something has read Omnitron's image column, and an
endpoint that answers 0 in that state would have the page reporting "nothing
needs a photo" about a catalogue nobody has looked at.

Mutating routes must be gated and recorded. Starting a publish moves a gigabyte
and hands a bundle to another company's team; both are things somebody should
be able to look up afterwards.
"""

from __future__ import annotations

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
    AuditLog,
    BrandLibraryAbsence,
    CatalogueMembership,
    HandoffBundle,
    Product,
    PublishedImage,
    SyncRun,
    User,
)

pytestmark = pytest.mark.usefixtures("temp_db")


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


def _stored_cookie() -> None:
    """A credential `session.build` can find. Without one it raises before the
    reachability check is reached."""
    from pim_studio.models import BrandLibrarySession

    with session_scope() as db:
        db.add(BrandLibrarySession(id=1, cookie="SESS=test"))


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


def _option(model_color: str, image: str | None = "") -> None:
    with session_scope() as db:
        product = Product(
            sku=f"{model_color}.T04",
            base_code_format=f"{model_color}.T04",
            model_color=model_color,
            base_code=model_color.split(".")[0],
        )
        db.add(product)
        db.flush()
        db.add(CatalogueMembership(product_id=product.id, market="PL", catalog_pk=1))
        if image is not None:
            attribute = db.query(Attribute).filter_by(key="image_0").one()
            db.add(
                AttributeValue(
                    entity_kind="model_color",
                    entity_key=model_color,
                    attribute_id=attribute.id,
                    market="*",
                    value=image,
                    state="live",
                    entry_grain="model_color",
                    source_kind="omnitron_api",
                )
            )


def _published(model_color: str, asset: str, bundle_id: int | None = None) -> None:
    with session_scope() as db:
        db.add(
            PublishedImage(
                model_color=model_color,
                brandlibrary_key=model_color.replace(".", "_"),
                asset_name=asset,
                edition="original",
                remote_name=f"{asset}.jpg",
                public_url=f"https://photos.example/pim-studio/{asset}.jpg",
                bytes=2048,
                bundle_id=bundle_id,
            )
        )


# ── the day-one screen ─────────────────────────────────────────────────────


def test_status_refuses_to_report_a_number_before_images_are_read(client) -> None:
    """A zero here would say "nothing needs a photo" about a catalogue nobody
    has looked at."""
    _registry()
    _option("AF0276.J2G")

    body = client.get("/api/images/status").json()

    assert body["blocked"] is not None
    assert "nobody has looked at" in body["blocked"]
    assert body["counts"]["missing"] is None
    assert body["imageSourceAt"] is None


def test_status_reports_counts_once_images_have_been_read(client) -> None:
    _registry()
    _image_sync_ran()
    _option("AF0276.J2G", image="")
    _option("TH2568.144", image="https://cdn/photo.jpg")

    body = client.get("/api/images/status").json()

    assert body["blocked"] is None
    assert body["counts"]["missing"] == 1
    assert body["counts"]["targets"] == 1
    assert body["imageSourceAt"] is not None


def test_what_is_with_turkey_is_the_manifest_not_the_skipped_targets(client) -> None:
    """Two numbers wearing similar names, and one of them is the card's label.

    `counts.published` is options this run skipped because they were already
    done — it reads 0 whenever the catalogue and the manifest do not overlap.
    A card labelled "with Turkey" showing 0 beside a manifest of 1,603 photos
    is simply wrong, so the card reads `withTurkey`, which counts options whose
    photos are published and not yet seen in Omnitron.
    """
    _registry()
    _image_sync_ran()
    _published("AF0276.J2G", "AF0276_J2G_32")
    _published("AF0276.J2G", "AF0276_J2G_31")
    _published("L3558.132", "L3558_132_01")

    body = client.get("/api/images/status").json()

    assert body["withTurkey"] == 2  # options, not photos
    assert body["photosPublished"] == 3
    assert body["counts"]["published"] == 0  # none of them are in a catalogue here


def test_a_photo_seen_in_omnitron_is_no_longer_with_turkey(client) -> None:
    """`landed_at` is the whole point of the exported-to-live hop."""
    from pim_studio.models import utcnow

    _registry()
    _image_sync_ran()
    _published("AF0276.J2G", "AF0276_J2G_32")
    with session_scope() as db:
        row = db.query(PublishedImage).one()
        row.landed_at = utcnow()

    body = client.get("/api/images/status").json()

    assert body["withTurkey"] == 0
    assert body["photosPublished"] == 1


def test_status_reports_which_halves_are_configured(client) -> None:
    """They fail separately and have different remedies."""
    body = client.get("/api/images/status").json()

    assert set(body["configured"]) >= {"library", "photoServer", "ready", "mode", "cookie"}
    assert body["configured"]["ready"] is False  # nothing set in a test env
    assert body["configured"]["cookie"] is None


# ── the lists ──────────────────────────────────────────────────────────────


def test_published_lists_the_manifest_with_its_urls(client) -> None:
    _published("AF0276.J2G", "AF0276_J2G_32")

    body = client.get("/api/images/published").json()

    assert body["total"] == 1
    (row,) = body["rows"]
    assert row["modelColor"] == "AF0276.J2G"
    assert row["url"].endswith("AF0276_J2G_32.jpg")
    assert row["landedAt"] is None


def test_published_is_paged(client) -> None:
    """The manifest runs to thousands; the page shows a handful."""
    for n in range(5):
        _published("AF0276.J2G", f"AF0276_J2G_{n:02d}")

    body = client.get("/api/images/published?limit=2").json()

    assert body["total"] == 5
    assert len(body["rows"]) == 2


def test_absent_lists_what_the_library_does_not_hold(client) -> None:
    with session_scope() as db:
        db.add(BrandLibraryAbsence(model_color="5H3389.CCS", attempts=4))

    body = client.get("/api/images/absent").json()

    (row,) = body["rows"]
    assert row["modelColor"] == "5H3389.CCS"
    assert row["attempts"] == 4


def test_the_absent_card_leaves_out_absences_that_no_longer_matter(client) -> None:
    """3,442 on the page, 225 real.

    An option Omnitron now has a photo for and a code held only as a grouping
    row are not photos the brand is missing. A code we never held stays: an
    uploaded list asked about it.
    """
    _registry()
    _image_sync_ran()
    _option("5H3389.CCS", image="")
    _option("TH2568.144", image="https://cdn/photo.jpg")
    _option("EJ1772.QDS", image="")
    with session_scope() as db:
        product = db.query(Product).filter_by(model_color="EJ1772.QDS").one()
        product.omnitron_product_type = PRODUCT_META
        for code in ("5H3389.CCS", "TH2568.144", "EJ1772.QDS", "3196.130"):
            db.add(BrandLibraryAbsence(model_color=code, attempts=1))

    status = client.get("/api/images/status").json()
    listed = client.get("/api/images/absent").json()

    assert status["absent"] == 2
    assert listed["total"] == 2
    assert sorted(r["modelColor"] for r in listed["rows"]) == ["3196.130", "5H3389.CCS"]


def test_an_absence_says_which_spellings_were_searched(client) -> None:
    """A wrong footwear rule otherwise reads as a photo nobody ever took.

    Our shoe codes carry a leading 7 the brand library drops. If that rule is
    ever wrong the shoe lands in this list looking exactly like a genuine
    absence, and somebody emails the brand about it. Both spellings on the row
    are what lets a reader tell the two apart.
    """
    with session_scope() as db:
        db.add(BrandLibraryAbsence(model_color="774RMA3517.255", attempts=1))
        db.add(BrandLibraryAbsence(model_color="AF0276.J2G", attempts=1))

    rows = {r["modelColor"]: r for r in client.get("/api/images/absent").json()["rows"]}

    assert rows["774RMA3517.255"]["searchedAs"] == ["774RMA3517.255", "74RMA3517.255"]
    # Apparel has one spelling, so the column stays quiet for it.
    assert rows["AF0276.J2G"]["searchedAs"] == ["AF0276.J2G"]


def test_bundles_do_not_offer_a_download_that_would_404(client) -> None:
    """A cleared bundle keeps its counts and loses its files."""
    with session_scope() as db:
        db.add(
            HandoffBundle(
                option_count=61,
                file_count=138,
                bytes=1,
                status="cleared",
                zip_path="/gone/x.zip",
                xlsx_path="/gone/x.xlsx",
            )
        )

    (row,) = client.get("/api/images/bundles").json()["rows"]

    assert row["status"] == "cleared"
    assert row["hasZip"] is False
    assert row["options"] == 61  # the counts are still true


def test_a_pruned_bundle_download_says_cleared_not_missing(client) -> None:
    """"Gone" and "never built" are different, and only one is a mistake."""
    with session_scope() as db:
        bundle = HandoffBundle(
            option_count=1, file_count=1, bytes=1, status="ready", xlsx_path="/gone/x.xlsx"
        )
        db.add(bundle)
        db.flush()
        bundle_id = bundle.id

    response = client.get(f"/api/images/bundles/{bundle_id}/xlsx")

    assert response.status_code == 410
    assert "cleared" in response.json()["detail"]


# ── gating and recording ───────────────────────────────────────────────────


def test_the_lists_require_a_session() -> None:
    app_module.app.dependency_overrides.clear()
    anonymous = TestClient(app_module.app, raise_server_exceptions=False)

    assert anonymous.get("/api/images/status").status_code == 401
    assert anonymous.post("/api/images/publish").status_code == 401


def test_a_manager_cannot_start_a_publish(client) -> None:
    """Same audience as Imports — whoever is responsible for content."""
    _as("manager")

    assert client.post("/api/images/publish").status_code == 403


def test_publishing_refuses_when_the_module_is_not_configured(client) -> None:
    _registry()
    _image_sync_ran()
    _option("AF0276.J2G", image="")

    response = client.post("/api/images/publish")

    assert response.status_code == 409
    assert "not fully configured" in response.json()["detail"]


def test_publishing_refuses_before_images_have_been_read(client, monkeypatch) -> None:
    """The same guard as the CLI, through the API."""
    from pim_studio.admin import images_api

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
    _registry()
    _option("AF0276.J2G", image="")

    response = client.post("/api/images/publish")

    assert response.status_code == 409
    assert "image sync" in response.json()["detail"]


def test_publishing_refuses_when_the_library_is_unreachable(client, monkeypatch) -> None:
    """The panel container usually cannot reach the library at all.

    Its outbound :443 is redirected by the same egress rule that makes the
    Omnitron sync run from host cron. Without this check the button spawns a
    thread that dies quietly, leaving a failed row on Sync Runs minutes later
    for somebody who has already walked away.
    """
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
        lambda self: (False, "could not reach the brand library"),
    )
    _stored_cookie()
    _registry()
    _image_sync_ran()
    _option("AF0276.J2G", image="")

    response = client.post("/api/images/publish")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "could not reach" in detail
    assert "pim-studio-images.sh" in detail


def test_a_reachable_library_starts_the_run(client, monkeypatch) -> None:
    from pim_studio.admin import images_api

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
    monkeypatch.setattr(images_api, "_publish_in_background", lambda *a, **k: None)
    _stored_cookie()
    _registry()
    _image_sync_ran()
    _option("AF0276.J2G", image="")

    response = client.post("/api/images/publish")

    assert response.status_code == 200
    assert response.json()["options"] == 1


def test_marking_a_bundle_sent_is_recorded_against_the_person(client) -> None:
    """It leaves the building. Somebody should be able to look that up."""
    with session_scope() as db:
        bundle = HandoffBundle(option_count=61, file_count=138, bytes=1, status="ready")
        db.add(bundle)
        db.flush()
        bundle_id = bundle.id

    assert client.post(f"/api/images/bundles/{bundle_id}/sent").status_code == 200

    with session_scope() as db:
        assert db.get(HandoffBundle, bundle_id).sent_at is not None
        rows = db.query(AuditLog).all()
        assert any("bundle" in (r.action or "").lower() for r in rows)


def test_marking_an_unknown_bundle_sent_is_a_404(client) -> None:
    assert client.post("/api/images/bundles/999/sent").status_code == 404


# ── the cookie ─────────────────────────────────────────────────────────────


def test_a_cookie_is_tested_before_it_is_stored(client, monkeypatch) -> None:
    """A bad cookie saved silently turns the next scheduled run into a failure
    nobody connects to this paste."""
    from pim_studio.admin import images_api
    from pim_studio.brandlibrary import session as session_store

    monkeypatch.setattr(
        images_api.BrandLibraryClient,
        "test",
        lambda self: (False, "the brand library refused our session"),
    )

    response = client.post("/api/images/cookie", json={"text": "SESS=dead"})

    assert response.status_code == 400
    with session_scope() as db:
        assert session_store.current(db) is None


def test_a_working_cookie_is_stored_against_the_person(client, monkeypatch) -> None:
    from pim_studio.admin import images_api
    from pim_studio.brandlibrary import session as session_store

    monkeypatch.setattr(
        images_api.BrandLibraryClient, "test", lambda self: (True, "48 210 photos visible")
    )

    response = client.post("/api/images/cookie", json={"text": "SESS=fresh"})

    assert response.status_code == 200
    with session_scope() as db:
        credential = session_store.current(db)
    assert credential is not None
    assert credential.cookie == "SESS=fresh"
    assert credential.rotated_by == "t@example.com"
    assert credential.last_ok_at is not None


def test_a_copy_as_curl_paste_is_accepted(client, monkeypatch) -> None:
    """It is what the browser's network tab produces, and nobody types a
    cookie by hand."""
    from pim_studio.admin import images_api
    from pim_studio.brandlibrary import session as session_store

    monkeypatch.setattr(images_api.BrandLibraryClient, "test", lambda self: (True, "ok"))
    curl = "curl 'https://x/api/v4/media/' -H 'cookie: SESS=abc' -H 'user-agent: Moz/5'"

    assert client.post("/api/images/cookie", json={"text": curl}).status_code == 200

    with session_scope() as db:
        credential = session_store.current(db)
    assert credential.cookie == "SESS=abc"
    assert credential.user_agent == "Moz/5"


def test_a_paste_with_no_cookie_in_it_says_where_to_get_one(client) -> None:
    response = client.post("/api/images/cookie", json={"text": "hello"})

    assert response.status_code == 400
    assert "network tab" in response.json()["detail"]


def test_a_manager_cannot_replace_the_cookie(client) -> None:
    _as("manager")

    assert client.post("/api/images/cookie", json={"text": "SESS=x"}).status_code == 403


def test_the_status_reports_the_cookies_age(client, monkeypatch) -> None:
    """The library has no password flow, so a session that expires within hours
    is the whole of authentication — when it was last pasted is a question the
    page has to answer without anyone running a query."""
    from pim_studio.admin import images_api

    monkeypatch.setattr(images_api.BrandLibraryClient, "test", lambda self: (True, "ok"))
    client.post("/api/images/cookie", json={"text": "SESS=fresh"})

    cookie = client.get("/api/images/status").json()["configured"]["cookie"]

    assert cookie["rotatedBy"] == "t@example.com"
    assert cookie["rotatedAt"] is not None
    assert cookie["lastOkAt"] is not None
    assert cookie["fromEnv"] is False


# ── OAuth ──────────────────────────────────────────────────────────────────


def test_oauth_credentials_make_the_module_ready_without_a_cookie(client, monkeypatch) -> None:
    """The whole point of having them: nobody pastes anything each week."""
    from pim_studio.brandlibrary import session as session_store

    monkeypatch.setattr(session_store, "oauth_configured", lambda: True)

    configured = client.get("/api/images/status").json()["configured"]

    assert configured["library"] is True
    assert configured["mode"] == "oauth"
    assert configured["cookie"] is None


def test_pasting_a_cookie_is_refused_when_oauth_is_configured(client, monkeypatch) -> None:
    """Storing one would be storing a credential nothing consults, and the
    page would then report an age for something that is not in use."""
    from pim_studio.admin import images_api

    monkeypatch.setattr(images_api.session_store, "oauth_configured", lambda: True)

    response = client.post("/api/images/cookie", json={"text": "SESS=x"})

    assert response.status_code == 409
    assert "OAuth" in response.json()["detail"]


def test_the_mode_is_cookie_when_one_is_stored(client, monkeypatch) -> None:
    from pim_studio.admin import images_api

    monkeypatch.setattr(images_api.BrandLibraryClient, "test", lambda self: (True, "ok"))
    client.post("/api/images/cookie", json={"text": "SESS=fresh"})

    assert client.get("/api/images/status").json()["configured"]["mode"] == "cookie"


def test_publishing_says_the_credential_is_gone_rather_than_erroring(client, monkeypatch) -> None:
    """Config said ready, the credential is not there. A 500 would read as a
    broken panel rather than as something to go and replace."""
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
    _registry()
    _image_sync_ran()
    _option("AF0276.J2G", image="")

    response = client.post("/api/images/publish")

    assert response.status_code == 409
    assert "brand library" in response.json()["detail"]


def test_a_cookie_paste_that_cannot_reach_the_library_names_the_host_route(
    client, monkeypatch
) -> None:
    """On the server this is the normal outcome, not an edge case.

    The app container's outbound :443 is redirected, so the paste box can never
    verify a cookie there and can never store one. Saying only "could not reach
    the brand library" rejects a perfectly good cookie from a panel that looks
    broken, and hides the one route that works.
    """
    from pim_studio.admin import images_api

    monkeypatch.setattr(
        images_api.BrandLibraryClient,
        "test",
        lambda self: (False, "could not reach the brand library"),
    )

    response = client.post("/api/images/cookie", json={"text": "SESS=fine"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "outbound network is blocked" in detail
    assert "pim_studio.brandlibrary cookie" in detail


def test_an_expired_cookie_still_says_it_expired(client, monkeypatch) -> None:
    """The other case must not be swallowed by the egress explanation."""
    from pim_studio.admin import images_api

    monkeypatch.setattr(
        images_api.BrandLibraryClient,
        "test",
        lambda self: (False, "answered text/html instead of JSON — that is its login page"),
    )

    detail = client.post("/api/images/cookie", json={"text": "SESS=dead"}).json()["detail"]

    assert "login page" in detail
    assert "outbound network is blocked" not in detail
