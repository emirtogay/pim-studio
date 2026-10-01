"""Publishing photos, and the mistakes that cost the first run an afternoon.

Everything here runs against stand-ins for the library and the photo server.
That is not a compromise: the failures worth asserting — a filename collision,
a resumed run re-uploading, an absence going unrecorded — are decided by this
module's own logic and would be no more true against a real FTP session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from pim_studio.brandlibrary.client import ZALANDO_KEY, Asset
from pim_studio.brandlibrary.publish import publish
from pim_studio.brandlibrary.targets import Target
from pim_studio.models import (
    PRODUCT_META,
    Attribute,
    AttributeValue,
    Base,
    BrandLibraryAbsence,
    Product,
    PublishedImage,
)

BASE_URL = "https://photos.example/photo_colection"
REMOTE_DIR = "pim-studio"
YEARS = ["2026"]


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'publish.db'}", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class FakeLibrary:
    """Answers with a fixed asset list and writes a byte when asked to fetch."""

    def __init__(self, assets: list[Asset], fail: set[str] | None = None) -> None:
        self.assets = assets
        self.fail = fail or set()
        self.downloaded: list[tuple[str, str]] = []
        self.scans = 0

    def catalog_index(self, years, on_progress=None):  # noqa: ANN001, ARG002
        self.scans += 1
        return self.assets

    def download_edition(self, asset: Asset, edition: str, dest: Path) -> int:
        if asset.name in self.fail:
            raise RuntimeError("the library refused this one")
        self.downloaded.append((asset.name, edition))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpegbytes")
        return 9


class FakeServer:
    def __init__(self, existing: set[str] | None = None) -> None:
        self._existing = existing or set()
        self.uploaded: list[str] = []

    def existing(self) -> set[str]:
        return set(self._existing)

    def upload(self, local: Path, remote: str) -> int:
        self.uploaded.append(remote)
        return local.stat().st_size

    def close(self) -> None:  # pragma: no cover - nothing to close
        pass


class FakeRun:
    id = 1

    def __init__(self) -> None:
        self.rows_seen = self.rows_imported = self.rows_dropped = 0

    def seen(self, n: int = 1) -> None:
        self.rows_seen += n

    def imported(self, n: int = 1) -> None:
        self.rows_imported += n

    def dropped(self, n: int = 1, reason: str = "") -> None:  # noqa: ARG002
        self.rows_dropped += n


def asset(name: str, *, zalando: bool = False, ident: str = "") -> Asset:
    return Asset(
        id=ident or name,
        name=name,
        extension="jpg",
        original=f"https://cdn/{name}.jpg",
        thumbnails={ZALANDO_KEY: f"https://cdn/{name}_zal.jpg"} if zalando else {},
    )


def run_publish(db, library, server, codes, **kwargs):
    return publish(
        db=db,
        client=library,
        server=server,
        targets=[Target(model_color=c) for c in codes],
        run=kwargs.pop("run", FakeRun()),
        base_url=BASE_URL,
        remote_dir=REMOTE_DIR,
        years=YEARS,
        **kwargs,
    )


# ── the happy path, and what it records ────────────────────────────────────


def test_a_matched_option_is_uploaded_and_recorded(db) -> None:
    library = FakeLibrary([asset("AF0276_J2G_32")])
    server = FakeServer()

    result = run_publish(db, library, server, ["AF0276.J2G"])

    assert result.uploaded == 1
    assert server.uploaded == ["AF0276_J2G_32.jpg"]

    (row,) = db.scalars(select(PublishedImage)).all()
    assert row.model_color == "AF0276.J2G"
    assert row.brandlibrary_key == "AF0276_J2G"
    assert row.edition == "original"
    assert row.public_url == f"{BASE_URL}/{REMOTE_DIR}/AF0276_J2G_32.jpg"
    assert row.landed_at is None  # Turkey has not uploaded it yet


def test_the_url_is_recorded_because_the_url_is_the_deliverable(db) -> None:
    """A file on the server that nothing points at is work to be redone."""
    library = FakeLibrary([asset("AF0276_J2G_32")])

    run_publish(db, library, FakeServer(), ["AF0276.J2G"])

    (url,) = db.execute(select(PublishedImage.public_url)).one()
    assert url.startswith("https://") and url.endswith("AF0276_J2G_32.jpg")


def test_both_editions_are_published_when_the_asset_has_both(db) -> None:
    library = FakeLibrary([asset("AF0276_J2G_32", zalando=True)])
    server = FakeServer()

    run_publish(db, library, server, ["AF0276.J2G"])

    assert sorted(server.uploaded) == ["AF0276_J2G_32.jpg", "AF0276_J2G_32_ZAL.jpg"]


def test_an_asset_without_a_zalando_crop_publishes_only_the_original(db) -> None:
    """Most assets have no marketplace edition; that is not a failure."""
    library = FakeLibrary([asset("AF0276_J2G_32")])
    server = FakeServer()

    result = run_publish(db, library, server, ["AF0276.J2G"])

    assert server.uploaded == ["AF0276_J2G_32.jpg"]
    assert result.failed == 0


def test_the_catalogue_is_read_once_however_many_options_are_wanted(db) -> None:
    """The whole reason the module is shaped this way.

    A search per product costs ~40s against this instance; four hundred of them
    would outlive the session that authorises them.
    """
    library = FakeLibrary([asset("A_1_01"), asset("B_2_01"), asset("C_3_01")])

    run_publish(db, library, FakeServer(), ["A.1", "B.2", "C.3"])

    assert library.scans == 1


# ── the collision that cost 228 uploads ────────────────────────────────────


def test_two_assets_sharing_a_name_produce_one_upload(db) -> None:
    """136 filenames collided on the first run.

    The remote name derives from the asset name, so without deduping the
    second upload silently overwrites the first — same bytes on the wire, same
    path, no error anywhere.
    """
    library = FakeLibrary(
        [asset("AF0276_J2G_32", ident="a"), asset("AF0276_J2G_32", ident="b", zalando=True)]
    )
    server = FakeServer()

    run_publish(db, library, server, ["AF0276.J2G"])

    assert server.uploaded.count("AF0276_J2G_32.jpg") == 1
    # And the record kept is the one that could serve both outputs.
    assert "AF0276_J2G_32_ZAL.jpg" in server.uploaded


def test_the_manifest_cannot_hold_the_same_remote_file_twice(db) -> None:
    """The unique constraint is the last line of defence on the same mistake."""
    from sqlalchemy.exc import IntegrityError

    for _ in range(2):
        db.add(
            PublishedImage(
                model_color="AF0276.J2G",
                brandlibrary_key="AF0276_J2G",
                asset_name="AF0276_J2G_32",
                edition="original",
                remote_name="AF0276_J2G_32.jpg",
                public_url="https://photos/AF0276_J2G_32.jpg",
                bytes=1,
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()


# ── resuming ───────────────────────────────────────────────────────────────


def test_a_file_already_on_the_server_is_skipped(db) -> None:
    """A pasted cookie expires within hours and a full run is longer.

    Restarting has to be cheap, or an expired session means starting over.
    """
    library = FakeLibrary([asset("AF0276_J2G_32")])
    server = FakeServer(existing={"AF0276_J2G_32.jpg"})

    result = run_publish(db, library, server, ["AF0276.J2G"])

    assert server.uploaded == []
    assert result.skipped == 1
    assert result.uploaded == 0


def test_a_file_already_in_the_manifest_is_skipped(db) -> None:
    db.add(
        PublishedImage(
            model_color="AF0276.J2G",
            brandlibrary_key="AF0276_J2G",
            asset_name="AF0276_J2G_32",
            edition="original",
            remote_name="AF0276_J2G_32.jpg",
            public_url="https://photos/AF0276_J2G_32.jpg",
            bytes=1,
        )
    )
    db.commit()
    library = FakeLibrary([asset("AF0276_J2G_32")])
    server = FakeServer()

    result = run_publish(db, library, server, ["AF0276.J2G"])

    assert server.uploaded == []
    assert result.skipped == 1


def test_each_file_is_recorded_as_it_lands_not_at_the_end(db) -> None:
    """A run that dies at 80% must keep the 80%.

    The second asset fails; the first must already be on record.
    """
    library = FakeLibrary(
        [asset("AF0276_J2G_31"), asset("AF0276_J2G_32")], fail={"AF0276_J2G_32"}
    )

    result = run_publish(db, library, FakeServer(), ["AF0276.J2G"])

    assert result.uploaded == 1
    assert result.failed == 1
    assert [r.remote_name for r in db.scalars(select(PublishedImage))] == [
        "AF0276_J2G_31.jpg"
    ]


def test_one_bad_asset_does_not_end_the_run(db) -> None:
    library = FakeLibrary(
        [asset("A_1_01"), asset("B_2_01"), asset("C_3_01")], fail={"B_2_01"}
    )

    result = run_publish(db, library, FakeServer(), ["A.1", "B.2", "C.3"])

    assert result.uploaded == 2
    assert result.failed == 1
    assert not result.ok


# ── absences ───────────────────────────────────────────────────────────────


def test_an_option_the_library_does_not_hold_is_recorded(db) -> None:
    """Publishing nothing for an option is otherwise indistinguishable from
    never having looked — and the run reports success either way."""
    library = FakeLibrary([asset("AF0276_J2G_32")])

    result = run_publish(db, library, FakeServer(), ["AF0276.J2G", "5H3389.CCS"])

    assert result.absent == 1
    (absence,) = db.scalars(select(BrandLibraryAbsence)).all()
    assert absence.model_color == "5H3389.CCS"
    assert absence.attempts == 1


def test_searching_again_increments_the_attempt_count(db) -> None:
    """"Searched four times since 12 August" is a sentence somebody can act on."""
    library = FakeLibrary([])

    run_publish(db, library, FakeServer(), ["5H3389.CCS"])
    run_publish(db, library, FakeServer(), ["5H3389.CCS"])
    run_publish(db, library, FakeServer(), ["5H3389.CCS"])

    (absence,) = db.scalars(select(BrandLibraryAbsence)).all()
    assert absence.attempts == 3


def test_an_absence_that_resolves_stops_being_one(db) -> None:
    """Otherwise somebody chases the brand for a photo that arrived last week."""
    db.add(BrandLibraryAbsence(model_color="AF0276.J2G", attempts=4))
    db.commit()
    library = FakeLibrary([asset("AF0276_J2G_32")])

    run_publish(db, library, FakeServer(), ["AF0276.J2G"])

    assert db.scalars(select(BrandLibraryAbsence)).all() == []


def _product(db, model_color: str, *, meta: bool = False) -> None:
    db.add(
        Product(
            sku=f"{model_color}.T04",
            base_code_format=f"{model_color}.T04",
            model_color=model_color,
            base_code=model_color.split(".")[0],
            omnitron_product_type=PRODUCT_META if meta else 0,
        )
    )


def _omnitron_image(db, model_color: str) -> None:
    attribute = db.scalar(select(Attribute).where(Attribute.key == "image_0"))
    if attribute is None:
        attribute = Attribute(
            key="image_0",
            label="image_0",
            data_type="text",
            write_channel="read_only",
            value_grain="model_color",
        )
        db.add(attribute)
        db.flush()
    db.add(
        AttributeValue(
            entity_kind="model_color",
            entity_key=model_color,
            attribute_id=attribute.id,
            market="*",
            value=f"https://cdn/{model_color}.jpg",
            state="live",
            entry_grain="model_color",
            source_kind="omnitron_api",
        )
    )


def _absent_codes(db) -> list[str]:
    return sorted(a.model_color for a in db.scalars(select(BrandLibraryAbsence)))


def test_an_absence_omnitron_has_since_filled_is_cleared(db) -> None:
    """The library finding it is not the only way an absence ends.

    102 rows sat on the page as photos the brand never shot, for options
    Omnitron already showed a photo for.
    """
    _product(db, "5H3389.CCS")
    _omnitron_image(db, "5H3389.CCS")
    db.add(BrandLibraryAbsence(model_color="5H3389.CCS", attempts=2))
    db.commit()

    run_publish(db, FakeLibrary([]), FakeServer(), ["3196.130"])

    assert _absent_codes(db) == ["3196.130"]


def test_an_absence_for_a_grouping_row_is_cleared(db) -> None:
    """3,115 of these were searched on 2026-08-27 and were never options."""
    _product(db, "EJ1772.QDS", meta=True)
    db.add(BrandLibraryAbsence(model_color="EJ1772.QDS", attempts=1))
    db.commit()

    run_publish(db, FakeLibrary([]), FakeServer(), ["3196.130"])

    assert _absent_codes(db) == ["3196.130"]


def test_an_absence_for_a_real_option_still_missing_a_photo_stays(db) -> None:
    _product(db, "5H3389.CCS")
    db.add(BrandLibraryAbsence(model_color="5H3389.CCS", attempts=2))
    db.commit()

    run_publish(db, FakeLibrary([]), FakeServer(), ["5H3389.CCS"])

    (absence,) = db.scalars(select(BrandLibraryAbsence)).all()
    assert absence.model_color == "5H3389.CCS"
    assert absence.attempts == 3


def test_an_absence_for_a_code_we_never_held_survives(db) -> None:
    """An uploaded list searches for codes Omnitron never carried. That answer
    is the reason the upload exists, so it is not pruned as a phantom."""
    run_publish(db, FakeLibrary([]), FakeServer(), ["3196.130"])

    assert _absent_codes(db) == ["3196.130"]


# ── dry run ────────────────────────────────────────────────────────────────


def test_a_dry_run_resolves_everything_and_writes_nothing(db) -> None:
    """A genuine rehearsal: it proves auth, the scan and the matching."""
    library = FakeLibrary([asset("AF0276_J2G_32")])
    server = FakeServer()

    result = run_publish(db, library, server, ["AF0276.J2G"], dry_run=True)

    assert result.matched == 1
    assert result.planned == ["AF0276_J2G_32.jpg"]
    assert result.uploaded == 0
    assert server.uploaded == []
    assert library.downloaded == []
    assert db.scalars(select(PublishedImage)).all() == []


def test_a_dry_run_still_records_what_the_library_lacks(db) -> None:
    """The absence is a fact the rehearsal genuinely established."""
    result = run_publish(db, FakeLibrary([]), FakeServer(), ["5H3389.CCS"], dry_run=True)

    assert result.absent == 1
    assert db.scalars(select(BrandLibraryAbsence)).all() != []


def test_a_dry_run_needs_no_photo_server(db) -> None:
    result = run_publish(db, FakeLibrary([asset("A_1_01")]), None, ["A.1"], dry_run=True)

    assert result.planned == ["A_1_01.jpg"]


def test_a_real_run_refuses_without_a_photo_server(db) -> None:
    from pim_studio.brandlibrary.client import BrandLibraryError

    with pytest.raises(BrandLibraryError):
        run_publish(db, FakeLibrary([asset("A_1_01")]), None, ["A.1"])


# ── run bookkeeping ────────────────────────────────────────────────────────


def test_the_run_counts_what_it_read_and_wrote(db) -> None:
    """A row of zeros on the Sync Runs page is indistinguishable from a run
    that did nothing."""
    run = FakeRun()
    library = FakeLibrary([asset("AF0276_J2G_32"), asset("OTHER_ZZ_01")])

    run_publish(db, library, FakeServer(), ["AF0276.J2G"], run=run)

    assert run.rows_seen == 2
    assert run.rows_imported == 1


def test_a_failure_is_counted_as_dropped(db) -> None:
    run = FakeRun()
    library = FakeLibrary([asset("A_1_01")], fail={"A_1_01"})

    run_publish(db, library, FakeServer(), ["A.1"], run=run)

    assert run.rows_dropped == 1


def test_nothing_to_do_is_not_a_scan(db) -> None:
    """Reading 1,200 pages to publish nothing is eight wasted minutes."""
    library = FakeLibrary([asset("A_1_01")])

    result = run_publish(db, library, FakeServer(), [])

    assert library.scans == 0
    assert result.uploaded == 0


# ── the photo server's TLS posture ─────────────────────────────────────────
#
# An earlier version disabled certificate verification unconditionally, with a
# comment claiming it matched the downloader app. It did not: that app defaults
# to verifying and its own .env sets FTP_TLS_VERIFY=true. Only the throwaway
# script turned it off. These pin the corrected behaviour, because a silent
# downgrade is exactly the kind of thing that survives a rewrite.


def test_certificates_are_verified_by_default() -> None:
    from pim_studio.brandlibrary.photoserver import PhotoServer

    server = PhotoServer(host="h", user="u", password="p")

    assert server.verify is True
    assert server.tls == "auto"


def test_verification_can_be_turned_off_but_has_to_be_asked_for() -> None:
    from pim_studio.brandlibrary.photoserver import PhotoServer

    assert PhotoServer(host="h", user="u", password="p", verify=False).verify is False


def test_an_empty_remote_dir_does_not_mean_the_account_root() -> None:
    """The downloader app's own FTP_REMOTE_DIR is blank — it uploads to the
    root. Copying that value across would scatter photos beside a year of
    somebody else's files and break every URL already published.
    """
    from pim_studio.brandlibrary.photoserver import PhotoServer

    assert PhotoServer(host="h", user="u", password="p", remote_dir="").remote_dir == "pim-studio"
    assert PhotoServer(host="h", user="u", password="p", remote_dir="/x/").remote_dir == "x"


def test_the_configured_defaults_match_the_app_that_has_been_working() -> None:
    from pim_studio.config import settings

    assert settings.ftp_tls == "auto"
    assert settings.ftp_tls_verify is True
    assert settings.ftp_remote_dir == "pim-studio"
