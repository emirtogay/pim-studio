"""Who needs a photo.

A database read with no network in it, which is the point: the expensive part
of this module is the catalogue scan, and knowing whether it is worth starting
should cost nothing.

**The guard is the first thing here and the reason the module has a shape.**
Nothing may ask "which options have no image" until something has actually read
Omnitron's image column. Before that, an option with no image on record and an
option nobody has looked at are the same row — and the honest-looking answer is
"all 18,036 of them", which would send a run at the entire catalogue. This
codebase has produced four separate rounds of confident, plausible, wrong
numbers exactly this way, phantom missing images among them. So the read raises
rather than returning a number it cannot stand behind.

Three exclusions, each for a different reason:

**Banned** options are deliberate. Merch removed them; a photo would be work
nobody wants done.

**Already published** options have had their turn — the photo is on the photo
server waiting for Turkey, and re-publishing it would upload the same bytes to
the same path.

**Not in any catalogue** is not an exclusion so much as the definition: the
product set is the union of live catalogue memberships, per the 2026-08-14
decision, and an option no market offers is not missing anything.

**The product set is `Product.is_size()` options, and nothing wider.** That is
the set `sync.liveness.sync_images` reads, and only inside it does "no image
row" mean "read and found empty". Omnitron's grouping rows (`product_type` 1,
one per style-colour) join catalogues too, but the image sync never visits
them, so without the filter every option that exists only as a grouping row
reads as missing a photo because nobody looked. Until 0.46.0 this module was the
one reader of the catalogue join that left the filter off, and the page said
4,348 options were missing a photo when 586 were. The guard above catches "no
image sync has ever run"; this is the same guard at the grain of the row.

Absences are deliberately *not* excluded. An option the library did not hold
last week may hold it this week, and the catalogue is read in one pass whether
that option is in the search or not — so re-checking is free, and charging
somebody's attention for it would be the only cost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    Attribute,
    AttributeValue,
    BanComment,
    BrandLibraryAbsence,
    CatalogueMembership,
    Product,
    PublishedImage,
)
from ..rules import identifiers
from ..rules.ban import BanVerdict, classify_comment
from ..sync.runs import last_successful_run

#: The attribute carrying the first product image. Read-only in the registry —
#: it mirrors Omnitron's own URL, and this module never writes it.
IMAGE_ATTRIBUTE = "image_0"

#: The attribute merch uses to mark an option removed.
COMMENT_ATTRIBUTE = "pl_comment"

#: Run kinds that prove the image column has been read. Two, because the
#: `liveness` stage syncs images alongside price and stock and the `images`
#: stage does it alone — both leave the column populated, and treating only one
#: as evidence would refuse to run on a perfectly well-synced database.
IMAGE_SOURCE_KINDS = ("omnitron_images", "omnitron_liveness")


class ImageSourceMissing(RuntimeError):
    """Nothing has read Omnitron's image column, so absence proves nothing.

    If you are reading this in a traceback, the fix is not to relax the guard.
    Run `python -m pim_studio.sync images` — or wait for the nightly sync — and
    ask again. Answering anyway would report every option in the catalogue as
    needing a photo, and the run that followed would try to publish one for
    each of them.
    """


@dataclass(frozen=True, slots=True)
class Target:
    """One option that needs a photo."""

    model_color: str
    #: How many times the library has been searched for it and come back
    #: empty. Zero for an option nobody has looked for yet.
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class TargetSet:
    """What a run would work on, and what it deliberately left out.

    The excluded counts are here so the page can say *why* a number is what it
    is. "412 options need a photo" and "412, having set aside 61 merch removed
    and 96 already with Turkey" are the same fact, and only the second one
    survives somebody asking where the other 157 went.
    """

    targets: tuple[Target, ...]
    #: Options with no image, before the exclusions below.
    missing: int
    #: Excluded: merch marked them removed.
    banned: int
    #: Excluded: already on the photo server, waiting for Turkey.
    published: int
    #: Options whose comment is on neither ban list. Not excluded — they are a
    #: request for somebody to classify a new comment, and silently treating
    #: them as fine is what the three-verdict rule exists to prevent.
    unclassified: int
    #: When the image column was last read. What makes the count's age visible.
    image_source_at: datetime

    def __len__(self) -> int:
        return len(self.targets)


#: What a ModelColor looks like, loosely: a style part and a colour part.
#:
#: Deliberately a shape test and nothing more. `identifiers.resolve_model_color`
#: refuses to reason from shape and it is right to — deciding where the colour
#: code starts is unreliable, and it has a catalogue to match against instead.
#: This is a different question. An uploaded list is searched against the brand
#: library, which holds photos for options Omnitron has never carried, so "is
#: this a product we hold" cannot be the test of whether to look. All this has
#: to do is separate a product code from a spreadsheet's stray heading or note,
#: and it is generous on purpose: a wrong exclusion is a photo nobody fetches,
#: while a wrong inclusion costs one lookup in a scan already running.
#:
#: The bounds come from the shapes in the README plus the ones an upload turned
#: up: `L3539.132`, `3196.130`, `760KFA0791.1T3`, `XF8672P.BZD`.
_MODEL_COLOR_SHAPE = re.compile(
    r"^(?=[A-Z0-9]*\d)[A-Z0-9]{3,12}\.[A-Z0-9]{2,5}(?:\.[A-Z0-9]{1,5})?$"
)


def as_model_color(token: str) -> str | None:
    """A written-down code in this module's spelling, or None if it is not one.

    Unlike `resolve_model_color` this asks nothing of the database. It is the
    entry point for codes somebody named explicitly, which the brand library
    may well hold photos for whether or not Omnitron has ever listed them.
    """
    raw = (token or "").strip()
    if not raw:
        return None
    # Tested whole, before the size suffix is dropped. Truncating first would
    # let any prose through: separators normalise to dots, so "notes for the
    # buyer" becomes NOTES.FOR.THE.BUYER and its first two parts are a
    # perfectly code-shaped NOTES.FOR. Matching the full string caps it at
    # three parts, and the digit lookahead finishes the job — every Northwind
    # style carries one, and no English word does.
    full = identifiers.dotted(raw)
    if not _MODEL_COLOR_SHAPE.match(full):
        return None
    # Split rather than `identifiers.model_color`. That helper strips a
    # trailing `.T…` from a two-part code, which is a size suffix for the
    # apparel it was written against and a genuine colour code everywhere
    # else — it turns `CH1327.TTF` into `CH1327`, quietly, and the result
    # still looks like a ModelColor. The shape above has already capped this
    # at three parts, so taking the first two needs no heuristic at all.
    return ".".join(full.split(".")[:2])


@dataclass(frozen=True, slots=True)
class SuppliedSet:
    """A list somebody uploaded, and what the automatic path would have skipped.

    Every code given is kept. The three tuples are a report, not a filter: they
    say which of the codes `find_targets` would have declined, so that "I gave
    you 400 and 380 came back" has an answer more useful than "some were
    skipped". A code can appear in more than one of them — merch can remove an
    option that no catalogue carries — and that is left as it is rather than
    ranked into a single reason, because both facts are true.
    """

    targets: tuple[Target, ...]
    #: Merch marked these removed. Searched anyway; the list said so.
    banned: tuple[str, ...]
    #: Already on the photo server. Searched, and `publish` will skip the
    #: files it has, so these cost a lookup and no bytes.
    published: tuple[str, ...]
    #: No live catalogue carries these. Often the point of an upload — a
    #: sneaker or a watch nobody has listed in PL yet.
    out_of_catalogue: tuple[str, ...]
    #: Given twice or more, and collapsed. Worth reporting: a list with
    #: duplicates usually means two sources were pasted together.
    duplicates: int

    def __len__(self) -> int:
        return len(self.targets)


def supplied_targets(db: Session, codes: list[str]) -> SuppliedSet:
    """Classify an uploaded list of ModelColors without excluding any of them.

    The counterpart to `find_targets`, and deliberately not a filter. That
    function answers "who needs a photo", a question about our own data, so it
    is entitled to set aside options merch removed and options already sent.
    This one is handed the answer — somebody named these codes — and its work
    is to carry the list out and then say which of them the weekly job would
    have left alone.

    Note what is *not* here: the `ImageSourceMissing` guard. That guard exists
    because "has no image on record" cannot be told from "nobody has looked"
    until something has read the image column. An explicit list never asks that
    question, so an upload works on a database that has never seen an image
    sync.
    """
    seen = [text for text in (str(code).strip().upper() for code in codes) if text]
    wanted = list(dict.fromkeys(seen))

    attribute_ids = {
        key: value for key, value in db.execute(select(Attribute.key, Attribute.id))
    }
    comments = _values(db, attribute_ids.get(COMMENT_ATTRIBUTE))
    ban_table = {
        comment.strip().lower(): bans
        for comment, bans in db.execute(select(BanComment.comment, BanComment.bans))
    } or None

    published = {row[0] for row in db.execute(select(PublishedImage.model_color).distinct())}
    attempts = {
        code: tries
        for code, tries in db.execute(
            select(BrandLibraryAbsence.model_color, BrandLibraryAbsence.attempts)
        )
    }
    # Asked about the uploaded codes only. `find_targets` reads every live
    # membership because it iterates the whole product set; here the list is
    # the scope, and on a 60,000-option catalogue that is the difference
    # between one indexed lookup and a full scan per upload.
    live = {
        row[0]
        for row in db.execute(
            select(Product.model_color)
            .join(CatalogueMembership, CatalogueMembership.product_id == Product.id)
            .where(
                CatalogueMembership.left_at.is_(None),
                Product.model_color.in_(wanted),
                # Same population as `find_targets`, so "in no live catalogue"
                # means the same thing on the list result as on the page.
                Product.is_size(),
            )
            .distinct()
        )
    }

    banned: list[str] = []
    already: list[str] = []
    outside: list[str] = []
    for code in wanted:
        if classify_comment(comments.get(code) or None, ban_table) is BanVerdict.BANNED:
            banned.append(code)
        if code in published:
            already.append(code)
        if code not in live:
            outside.append(code)

    return SuppliedSet(
        targets=tuple(Target(model_color=code, attempts=attempts.get(code, 0)) for code in wanted),
        banned=tuple(banned),
        published=tuple(already),
        out_of_catalogue=tuple(outside),
        duplicates=len(seen) - len(wanted),
    )


def image_source_at() -> datetime | None:
    """When the image column was last read, or None if it never has been.

    Always timezone-aware. SQLite hands back naive datetimes where Postgres
    returns aware ones, and this module runs against both — development on the
    file, production on the shared instance. Normalising here rather than at
    each caller keeps "how old is this number" from being a question about
    which database answered it.
    """
    seen = [
        stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
        for stamp in (last_successful_run(kind) for kind in IMAGE_SOURCE_KINDS)
        if stamp is not None
    ]
    return max(seen) if seen else None


def find_targets(db: Session, limit: int | None = None) -> TargetSet:
    """The options a publish run would go looking for.

    Raises `ImageSourceMissing` when nothing has read the image column yet.
    """
    read_at = image_source_at()
    if read_at is None:
        raise ImageSourceMissing(
            "no successful image sync on record, so 'has no image' cannot be "
            "distinguished from 'nobody has looked'. Run "
            "`python -m pim_studio.sync images` first."
        )

    attribute_ids = dict(db.execute(select(Attribute.key, Attribute.id)).all())
    image_id = attribute_ids.get(IMAGE_ATTRIBUTE)
    comment_id = attribute_ids.get(COMMENT_ATTRIBUTE)

    # The product set: options at least one market still offers, counted by
    # the sizes someone can buy. `is_size()` is not optional here — see the
    # module docstring for the 3,762 options it was inventing without it.
    in_catalogue = {
        row[0]
        for row in db.execute(
            select(Product.model_color)
            .join(CatalogueMembership, CatalogueMembership.product_id == Product.id)
            .where(CatalogueMembership.left_at.is_(None), Product.is_size())
            .distinct()
        )
    }

    # Both attributes live at model_color grain (see `seed.py`), so no
    # fan-out from SKUs is needed and `entity_key` is the ModelColor itself.
    imaged = _non_empty(db, image_id)
    comments = _values(db, comment_id)

    ban_table = {
        comment.strip().lower(): bans
        for comment, bans in db.execute(select(BanComment.comment, BanComment.bans))
    } or None

    published = {row[0] for row in db.execute(select(PublishedImage.model_color).distinct())}
    attempts = dict(
        db.execute(select(BrandLibraryAbsence.model_color, BrandLibraryAbsence.attempts)).all()
    )

    missing = banned = unclassified = already = 0
    found: list[Target] = []

    for model_color in sorted(in_catalogue):
        if model_color in imaged:
            continue
        missing += 1

        verdict = classify_comment(comments.get(model_color) or None, ban_table)
        if verdict is BanVerdict.BANNED:
            banned += 1
            continue
        if verdict is BanVerdict.UNKNOWN:
            # Counted, not excluded. An unclassified comment is a question for
            # a person, and answering it by not fetching a photo would be a
            # decision this module is not entitled to make.
            unclassified += 1

        if model_color in published:
            already += 1
            continue

        found.append(Target(model_color=model_color, attempts=attempts.get(model_color, 0)))

    return TargetSet(
        targets=tuple(found[:limit] if limit else found),
        missing=missing,
        banned=banned,
        published=already,
        unclassified=unclassified,
        image_source_at=read_at,
    )


def stale_absence() -> ColumnElement[bool]:
    """An absence row that no longer describes an option needing a photo.

    Two kinds, and both are rows the brand library was searched for and did
    not hold, which is true and no longer relevant:

    - **Omnitron has an image for it now.** Turkey uploaded one, or it came in
      from another route. Absences only cleared when *the library* later found
      the option, so an option that got its photo elsewhere stayed on the
      "not in the brand library" list indefinitely.
    - **It is a code we hold only as a grouping row, or no longer hold at all.**
      Before 0.46.0 the target population included Omnitron's grouping rows,
      and the 2026-08-27 runs searched 3,115 of them. They were never options.

    A code with no product row at all is *not* stale: that is an uploaded list
    searching for something Omnitron never carried, which is the reason uploads
    exist, and its absence is a real answer somebody asked for.

    Used both to count and list absences on the page and to delete them after
    a run, so the page and the table agree about what an absence is.
    """
    imaged = (
        select(AttributeValue.entity_key)
        .join(Attribute, Attribute.id == AttributeValue.attribute_id)
        .where(
            Attribute.key == IMAGE_ATTRIBUTE,
            AttributeValue.entity_kind == "model_color",
            AttributeValue.state_lane == "current",
            AttributeValue.value.is_not(None),
            func.trim(AttributeValue.value) != "",
        )
    )
    known = select(Product.model_color)
    sized = select(Product.model_color).where(Product.is_size())
    code = BrandLibraryAbsence.model_color
    return or_(code.in_(imaged), and_(code.in_(known), code.not_in(sized)))


def _values(db: Session, attribute_id: int | None) -> dict[str, str]:
    """Every current value of one model_color-grain attribute, by ModelColor."""
    if attribute_id is None:
        return {}
    return {
        key: (value or "")
        for key, value in db.execute(
            select(AttributeValue.entity_key, AttributeValue.value).where(
                AttributeValue.attribute_id == attribute_id,
                AttributeValue.entity_kind == "model_color",
                AttributeValue.state_lane == "current",
            )
        )
    }


def _non_empty(db: Session, attribute_id: int | None) -> set[str]:
    """ModelColors holding a genuine value for one attribute.

    A row that exists with an empty value is *not* in here, and that is the
    distinction the whole module turns on: it means the column was read and
    this option has nothing in it, which is exactly a target. Absence of the
    row means nothing either way, which is what the guard above covers.
    """
    if attribute_id is None:
        return set()
    return {
        key
        for key, in db.execute(
            select(AttributeValue.entity_key).where(
                AttributeValue.attribute_id == attribute_id,
                AttributeValue.entity_kind == "model_color",
                AttributeValue.state_lane == "current",
                AttributeValue.value.is_not(None),
                func.trim(AttributeValue.value) != "",
            )
        )
    }
