"""Who needs a photo — and the refusal that comes before the question.

The guard gets the most attention here because it is the only bug in this file
that would be invisible in production: every other mistake shows up as a wrong
count somebody notices, while a missing guard produces a plausible number and a
run that tries to publish a photo for the entire catalogue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from pim_studio import db as db_module
from pim_studio.brandlibrary import targets as targets_module
from pim_studio.brandlibrary.targets import ImageSourceMissing, as_model_color, find_targets
from pim_studio.models import (
    PRODUCT_META,
    Attribute,
    AttributeValue,
    BanComment,
    Base,
    BrandLibraryAbsence,
    CatalogueMembership,
    Product,
    PublishedImage,
    SyncRun,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'targets.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    session = factory()
    _registry(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _registry(db: Session) -> None:
    for key, label in (("image_0", "First image"), ("pl_comment", "Comment (PL team)")):
        db.add(
            Attribute(
                key=key,
                label=label,
                data_type="text",
                write_channel="read_only",
                value_grain="model_color",
            )
        )
    db.commit()


def _attr(db: Session, key: str) -> int:
    return db.query(Attribute).filter_by(key=key).one().id


def _option(db: Session, model_color: str, *, in_catalogue: bool = True) -> Product:
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
    db.commit()
    return product


def _value(db: Session, model_color: str, key: str, value: str | None) -> None:
    db.add(
        AttributeValue(
            entity_kind="model_color",
            entity_key=model_color,
            attribute_id=_attr(db, key),
            market="*",
            value=value,
            state="live",
            entry_grain="model_color",
            source_kind="omnitron_api",
        )
    )
    db.commit()


def _image_sync_happened(db: Session, kind: str = "omnitron_images") -> None:
    db.add(
        SyncRun(
            kind=kind,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            finished_at=datetime.now(UTC) - timedelta(hours=1),
            status="success",
        )
    )
    db.commit()


# ── the guard ──────────────────────────────────────────────────────────────


def test_refuses_before_anything_has_read_the_image_column(db: Session) -> None:
    """The failure this module exists to make impossible.

    Without a successful image sync, an option with no image on record and an
    option nobody looked at are the same row. Returning a count here would
    report the whole catalogue as needing a photo.
    """
    _option(db, "AF0276.J2G")

    with pytest.raises(ImageSourceMissing):
        find_targets(db)


def test_a_failed_image_sync_is_not_evidence(db: Session) -> None:
    db.add(
        SyncRun(
            kind="omnitron_images",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            status="failed",
        )
    )
    db.commit()
    _option(db, "AF0276.J2G")

    with pytest.raises(ImageSourceMissing):
        find_targets(db)


def test_the_liveness_stage_counts_as_having_read_images(db: Session) -> None:
    """`liveness` syncs images alongside price and stock.

    Accepting only `omnitron_images` would refuse to run against a database
    whose images are perfectly well synced by the other stage.
    """
    _image_sync_happened(db, kind="omnitron_liveness")
    _option(db, "AF0276.J2G")

    result = find_targets(db)

    assert [t.model_color for t in result.targets] == ["AF0276.J2G"]


# ── what is and is not a target ────────────────────────────────────────────


def test_an_option_with_an_image_is_not_a_target(db: Session) -> None:
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")
    _value(db, "AF0276.J2G", "image_0", "https://cdn/photo.jpg")

    assert find_targets(db).targets == ()


def test_a_row_read_and_found_empty_is_a_target(db: Session) -> None:
    """The distinction the whole module turns on.

    A row that exists with an empty value is evidence the column was read and
    this option has nothing in it — which is exactly the thing to go fix. It
    must not be confused with the absent row the guard above covers.
    """
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")
    _value(db, "AF0276.J2G", "image_0", "")

    assert [t.model_color for t in find_targets(db).targets] == ["AF0276.J2G"]


def test_an_option_no_market_offers_is_not_missing_anything(db: Session) -> None:
    _image_sync_happened(db)
    _option(db, "AF0276.J2G", in_catalogue=False)

    result = find_targets(db)

    assert result.targets == ()
    assert result.missing == 0


def test_a_departed_membership_does_not_keep_an_option_in_scope(db: Session) -> None:
    _image_sync_happened(db)
    product = _option(db, "AF0276.J2G")
    membership = db.query(CatalogueMembership).filter_by(product_id=product.id).one()
    membership.left_at = datetime.now(UTC)
    db.commit()

    assert find_targets(db).targets == ()


# ── the exclusions ─────────────────────────────────────────────────────────


def test_a_banned_option_is_counted_and_excluded(db: Session) -> None:
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")
    _value(db, "AF0276.J2G", "pl_comment", "REMOVED")

    result = find_targets(db)

    assert result.targets == ()
    assert result.missing == 1
    assert result.banned == 1


def test_the_ban_table_from_the_database_is_what_decides(db: Session) -> None:
    """Merch invents comment conventions; the table is editable for that reason.

    A comment nobody has classified must not ban, and a newly classified one
    must, without a deploy.
    """
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")
    _value(db, "AF0276.J2G", "pl_comment", "pulled for AW27")
    db.add(BanComment(comment="pulled for AW27", bans=True, note="classified today"))
    db.commit()

    result = find_targets(db)

    assert result.targets == ()
    assert result.banned == 1


def test_an_unclassified_comment_is_flagged_but_still_fetched(db: Session) -> None:
    """Unknown means *someone decide*, not *skip it*.

    Declining to fetch a photo is a decision, and this module is not the place
    to make it on the strength of a comment nobody has read.
    """
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")
    _value(db, "AF0276.J2G", "pl_comment", "ask Ola about this one")

    result = find_targets(db)

    assert [t.model_color for t in result.targets] == ["AF0276.J2G"]
    assert result.unclassified == 1
    assert result.banned == 0


def test_an_already_published_option_is_not_republished(db: Session) -> None:
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")
    db.add(
        PublishedImage(
            model_color="AF0276.J2G",
            brandlibrary_key="AF0276_J2G",
            asset_name="AF0276_J2G_32",
            edition="original",
            remote_name="AF0276_J2G_32.jpg",
            public_url="https://photos/AF0276_J2G_32.jpg",
            bytes=1024,
        )
    )
    db.commit()

    result = find_targets(db)

    assert result.targets == ()
    assert result.published == 1
    assert result.missing == 1


def test_an_absence_is_searched_again_and_carries_its_attempts(db: Session) -> None:
    """Re-checking costs nothing — the catalogue is read in one pass either way.

    Excluding these would mean an option the library gained a photo for last
    week is never found.
    """
    _image_sync_happened(db)
    _option(db, "5H3389.CCS")
    db.add(BrandLibraryAbsence(model_color="5H3389.CCS", attempts=4))
    db.commit()

    (target,) = find_targets(db).targets

    assert target.model_color == "5H3389.CCS"
    assert target.attempts == 4


# ── reporting ──────────────────────────────────────────────────────────────


def test_the_counts_account_for_every_option_set_aside(db: Session) -> None:
    """`missing` has to reconcile, or the page cannot say where the rest went."""
    _image_sync_happened(db)
    for code in ("A.1", "B.2", "C.3", "D.4"):
        _option(db, code)
    _value(db, "A.1", "image_0", "https://cdn/a.jpg")  # has one — not missing
    _value(db, "B.2", "pl_comment", "REMOVED")  # banned
    db.add(
        PublishedImage(
            model_color="C.3",
            brandlibrary_key="C_3",
            asset_name="C_3_01",
            edition="original",
            remote_name="C_3_01.jpg",
            public_url="https://photos/C_3_01.jpg",
            bytes=1,
        )
    )
    db.commit()

    result = find_targets(db)

    assert result.missing == 3
    assert result.banned == 1
    assert result.published == 1
    assert [t.model_color for t in result.targets] == ["D.4"]
    assert result.missing - result.banned - result.published == len(result)


def test_the_limit_caps_targets_without_distorting_the_counts(db: Session) -> None:
    _image_sync_happened(db)
    for code in ("A.1", "B.2", "C.3"):
        _option(db, code)

    result = find_targets(db, limit=2)

    assert len(result) == 2
    assert result.missing == 3


def test_the_age_of_the_image_data_is_reported(db: Session) -> None:
    """A count is as old as the sync behind it, and looks identical either way."""
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")

    result = find_targets(db)

    assert result.image_source_at is not None
    assert (datetime.now(UTC) - result.image_source_at).total_seconds() < 60 * 60 * 3


def test_image_source_at_is_none_when_nothing_has_run(db: Session) -> None:
    assert targets_module.image_source_at() is None

# ── recognising a code somebody typed ──────────────────────────────────────


# ── the population ─────────────────────────────────────────────────────────


def test_an_option_held_only_as_a_grouping_row_is_not_missing_a_photo(db: Session) -> None:
    """The 3,762 phantom options of 0.45.

    Omnitron's grouping rows join catalogues like any product, but the image
    sync never reads them, so "no image row" for one of them means nobody
    looked. Counting them put 4,348 on the page when 586 were real.
    """
    _image_sync_happened(db)
    grouping = _option(db, "EJ1772.QDS")
    grouping.omnitron_product_type = PRODUCT_META
    _option(db, "AF0276.J2G")
    db.commit()

    found = find_targets(db)

    assert found.missing == 1
    assert [t.model_color for t in found.targets] == ["AF0276.J2G"]


def test_a_delisted_option_is_not_missing_a_photo(db: Session) -> None:
    _image_sync_happened(db)
    gone = _option(db, "EJ1772.QDS")
    gone.delisted_at = datetime.now(UTC)
    db.commit()

    assert find_targets(db).missing == 0


def test_a_grouping_row_beside_a_real_size_keeps_the_option_in_scope(db: Session) -> None:
    """Most options carry both. The size is what counts, not the grouping row."""
    _image_sync_happened(db)
    _option(db, "AF0276.J2G")
    meta = Product(
        sku="AF0276.J2G",
        base_code_format="AF0276.J2G",
        model_color="AF0276.J2G",
        base_code="AF0276",
        omnitron_product_type=PRODUCT_META,
    )
    db.add(meta)
    db.flush()
    db.add(CatalogueMembership(product_id=meta.id, market="CZ", catalog_pk=2))
    db.commit()

    assert [t.model_color for t in find_targets(db).targets] == ["AF0276.J2G"]


def test_a_colour_code_beginning_with_t_survives() -> None:
    """`CH1327.TTF` is a real ModelColor, not a size-suffixed article.

    `identifiers.model_color` strips a trailing `.T…` from a two-part code —
    correct for the apparel sizes it was written against, and wrong for every
    colour code that happens to start with T. It turned this into `CH1327`,
    which still looks like a ModelColor, so the run would have searched a code
    no product has and recorded a photo the brand had never shot.
    """
    assert as_model_color("CH1327.TTF") == "CH1327.TTF"


def test_a_size_suffixed_article_loses_only_the_size() -> None:
    assert as_model_color("TH2568.144.T04") == "TH2568.144"


@pytest.mark.parametrize(
    "code",
    ["BH2292.166", "765RFA6851.21G", "760KFA0791.1T3", "3196.130", "XF8672P.BZD", "SH8564R.S86"],
)
def test_the_shapes_this_catalogue_actually_uses_are_accepted(code: str) -> None:
    """Apparel, footwear, watches and the odd letter-suffixed style.

    Taken from a real upload, whose codes were being skipped because Omnitron
    had never carried them — which is not the same question as whether they are
    codes.
    """
    assert as_model_color(code) == code


@pytest.mark.parametrize("junk", ["Option", "ModelColor", "notes for the buyer", "total", "12", ""])
def test_things_that_are_not_codes_are_refused(junk: str) -> None:
    """Separators normalise to dots, so prose can arrive looking structured.

    "notes for the buyer" becomes NOTES.FOR.THE.BUYER, whose first two parts
    are perfectly code-shaped — which is why the shape is tested whole, and why
    a digit is required in the style part.
    """
    assert as_model_color(junk) is None
