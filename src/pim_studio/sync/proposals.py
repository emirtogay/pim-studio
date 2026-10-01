"""Values that are in the wrong language, and a candidate for each.

A PL/CZ/SK/HU content field holding English or Turkish is not missing content —
it is content nobody translated, or the tenant default leaking through an
unlocalised read. `rules.language.foreign_language` identifies both with
confidence and refuses to guess between the four market languages, which is
exactly the shape needed here: an unsure answer must mean "leave it alone".

Measured on the mirror, 2026-09-09:

    field            values   in the wrong language
    product_name     15,366                      45
    product_detail   35,239                   1,318
    description      27,542                   3,713

So the backlog is descriptions, not names — 27% of Hungarian descriptions read
as English — and the 45 names are a morning's work for a person. That
proportion is why this is built around a source interface rather than a single
translator: the cheap, free source is worth having and cannot be the whole
answer.

## Why a proposal and not a correction

Nothing here writes to the `current` lane. A proposal lands in the `proposal`
lane beside the live value and waits for a person, who accepts, edits or
rejects it in the cell inspector. Only accepting moves it across, as `approved`
— which is to say into the push queue, through the same door as a hand-typed
value. A machine translation of a garment description is a suggestion, and the
lifecycle already had a word for that.

## The sources, and why the free one is small

`SiblingSource` costs nothing and proposes only what the catalogue already
proves. Colours of one base code usually share a description, so a sibling's
Polish text is often exactly the missing Polish text. But not always, and the
failure is silent and confident: `product_detail` routinely names the colour —
`koszulka polo N.10.10 męska classic fit brązowa` against `...khaki` on the
same base code — so copying a sibling would describe a brown polo as khaki.

The guard is the catalogue's own evidence. A sibling's text is proposed only
when **two or more distinct colours carry it identically**. Text naming a
colour cannot pass that test, because the other colour would say something
else. It is a narrow rule and the measurements say so: 840 of 5,076, 17%.

A second guard, added after the first production run: the sibling must itself
be usable as a translation. `reads_foreign` answers "not *certainly* foreign"
at 0.90, and short English does not reach it — so that run happily proposed
`Northwind Men's 3-Pack Boxer` as a Polish product detail. `usable_as_translation`
rejects a sibling at 0.25 English confidence, because the two mistakes are not
equally costly: a wrong flag wastes a glance, while English offered *as* a
translation is a plausible-looking lie a reviewer may accept.

A third guard, added after the second: **a base code is not a style.**
`CH9883` holds an Oxford regular-fit shirt in four colours and a slim-fit
checked shirt in four more, and the run proposed the Oxford text for a checked
shirt — the colours genuinely agreed and the Czech was genuinely Czech, so
neither guard above could see anything wrong. A sibling must now also agree on
what it *is*: `same_garment` compares fit, pattern, sleeve, collar, category,
gender and composition, on whatever both declare.

`GroupingRowSource` is the second free one. Omnitron keeps a grouping row per
style — the object behind a listing card, never a size, and never what the
shop renders (measured on example.com, 2026-09-09: card and page show the
size's text, six of six) — and it is where a translator most naturally typed:
5H1299's grouping row holds `3 db elasztikus mintás boxeralsó` while every
size holds `3-Pack Printed Stretch Trunks`. The nightly reads those rows apart
from the sizes under `entity_kind="base_code"` (see `sync/values`), and this
source hands the text back for the sizes to carry. It names the style rather
than a colour, which is most of what the sibling rule guards against — though
nothing stops a person typing a colour into it, which is one more reason it is
a proposal and not a write. It is held to `usable_as_translation` like a
sibling: a grouping row that says `Northwind Men's 3-Pack Boxer` is not a
Polish detail either.

The remaining 4,236 need a translator, which is roughly 1.2M characters —
pennies through a small model, and nothing at all until somebody sets a key.
`MachineSource` is that seam; with no key configured it proposes nothing and
says so, rather than failing a run.

## The shared description key, offered rather than shown

Omnitron's `integration_kalem_icerik` holds a description in every language,
and no shop renders it: example.com, .cz, .sk and .hu show the market-suffixed
key and nothing else — measured 2026-09-07 where the two differ, and again
2026-09-16 where the suffixed key is *empty*, when the page shows no
description at all even with good Polish sitting in the shared key. Until
0.47.0 the panel stored the shared key as the description and the customer read
something else (784RMA0950.2A9: Turkish in the panel, English on example.com).

So the panel now stores what the shop shows, and the shared key becomes a
source like the two above. It is read by `sync/values`, which already fetches
every locale view, and offered through `offer_shared_key` — where the shop's
text is English or Turkish, or where an option the shop is selling shows none.
Unlike the others it also answers cells with no live value, because an empty
description on a listed option is the same defect in a quieter form.

A proposal outlives the reason for it unless something takes it back, and
after the switch many did: they were raised against shared-key text the panel
no longer holds. `withdraw_settled` removes a waiting proposal once its cell
holds text in a market language.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..logging import log
from ..models import Attribute, AttributeValue, StorefrontListing
from ..rules.language import english_confidence, foreign_language
from .targets import write_targets
from .writer import PROTECTED_STATES, upsert_attribute_values

#: Markup is stripped before the language test. A description is a list of
#: `<li>` fragments, and tag names are English in every market.
_HTML_TAG = re.compile(r"<[^>]+>")

#: The lane a candidate lands in, and the state it carries there.
PROPOSAL_LANE = "proposal"
PROPOSED = "proposed"

#: The source a shared-key proposal names in its `source_ref`. The values stage
#: owns these rows — it writes them and it takes them back.
SHARED_KEY = "shared_key"

#: How many proposals one pass will write. A cap because a source may cost
#: money per item, and because thousands of unreviewed suggestions is not a
#: queue anybody works through.
#:
#: It caps the *proposals*, not the candidates examined. Capping candidates was
#: the first version and it was quietly bad: `find_untranslated` returns rows
#: in whatever order the database hands them over, so taking the first 500 of
#: 7,781 was an arbitrary slice, not a sample. The first production run under
#: that rule offered 8 proposals when 651 were available — not because the
#: sources could not answer, but because they were never asked about the right
#: rows.
DEFAULT_LIMIT = 500


def reads_foreign(value: str | None) -> str | None:
    """`"ENGLISH"`, `"TURKISH"` or None, over text with its markup removed."""
    return foreign_language(_HTML_TAG.sub(" ", value or ""))


#: A sibling is rejected at this English confidence, far below the 0.90 that
#: *flags* a value. The asymmetry is the point and it is measured: the first
#: production run proposed `Northwind Men's 3-Pack Boxer` as a Polish detail and
#: `Northwind Men's T-Shirt` as another, because short English scores 0.40-0.44
#: and 0.90 let it through as "not certainly foreign". Every correct proposal
#: in that run scored 0.11 or below, so this separates them with room.
#:
#: Flagging a value wrongly wastes a glance. Proposing English *as* a
#: translation is a plausible-looking lie that a reviewer may well accept.
_SIBLING_ENGLISH_CEILING = 0.25

#: Below this many letters there is no text, whatever the detector says. The
#: same run proposed the literal string `0.0` as a Slovak description.
_MIN_LETTERS = 3

#: What makes two colours the same garment rather than the same base code.
#:
#: A base code is not a style. `CH9883` is an Oxford regular-fit shirt in four
#: colours and a slim-fit checked shirt in four others, and the second
#: production run proposed the Oxford wording for a checked shirt: two Czech
#: colours agreed on it, and the text really was Czech, so neither existing
#: guard could see anything wrong. These attributes separate them —
#: `fit` reads Regular Fit against Slim Fit, `pattern` reads Düz against
#: Kareli.
#:
#: Colour, season and imagery are deliberately absent. Colours of one garment
#: differ in exactly those and agree in these.
_GARMENT_KEYS = frozenset({
    "fit", "pattern", "sleeve_type", "collar_type",
    "sub_category", "main_category", "gender", "composition",
})


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def usable_as_translation(value: str | None) -> bool:
    """Whether a sibling's text may stand in for a missing market value.

    Stricter than "not certainly foreign", which is all `reads_foreign`
    answers. See `_SIBLING_ENGLISH_CEILING`.
    """
    text = _HTML_TAG.sub(" ", value or "").strip()
    if sum(c.isalpha() for c in text) < _MIN_LETTERS:
        return False
    if reads_foreign(value):
        return False
    return english_confidence(text) < _SIBLING_ENGLISH_CEILING


def _base_code(model_color: str) -> str:
    return model_color.split(".")[0]


def same_garment(
    a: dict[str, str], b: dict[str, str], *, need: int = 1
) -> bool:
    """Whether two options describe the same garment, on what both declare.

    Only attributes both carry are compared, because absence is ordinary in
    this catalogue and requiring a full set would refuse almost everything.
    All of those must agree, and there must be at least `need` of them — no
    shared attributes at all is not evidence of sameness, it is no evidence.
    """
    shared = [k for k in _GARMENT_KEYS if a.get(k) and b.get(k)]
    if len(shared) < need:
        return False
    return all(_norm(a[k]) == _norm(b[k]) for k in shared)


@dataclass(frozen=True)
class Candidate:
    """One live value that reads as the wrong language."""

    entity_key: str
    attribute_id: int
    market: str
    #: What the shop holds now, and what a person will compare against.
    current: str
    #: `ENGLISH` or `TURKISH`.
    language: str

    @property
    def cell(self) -> tuple[str, int, str]:
        return (self.entity_key, self.attribute_id, self.market)


class Source(Protocol):
    """Somewhere a replacement can come from."""

    name: str

    def propose(self, candidates: list[Candidate]) -> dict[tuple[str, int, str], str]:
        """Return `{cell: proposed text}`, omitting anything it cannot answer.

        Omission is the normal case and must stay cheap to express: a source
        that cannot help with a cell says nothing about it rather than
        returning the original or an empty string, either of which would be
        written as a proposal and wasted somebody's review.
        """
        ...


class SiblingSource:
    """Text another colour of the same base code already carries.

    Free, and limited on purpose — see the module docstring. Only text that at
    least two distinct colours carry identically is offered, which is what
    makes it safe against the colour named inside a detail line.
    """

    name = "sibling"

    def __init__(self, db: Session) -> None:
        self._db = db

    def propose(self, candidates: list[Candidate]) -> dict[tuple[str, int, str], str]:
        wanted = {(c.attribute_id, c.market) for c in candidates}
        bases = {_base_code(c.entity_key) for c in candidates}
        if not wanted or not bases:
            return {}

        # One read for every sibling that might answer any candidate.
        rows = self._db.execute(
            select(
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.market,
                AttributeValue.value,
            ).where(
                AttributeValue.state_lane == "current",
                AttributeValue.entity_kind == "model_color",
                AttributeValue.attribute_id.in_({a for a, _ in wanted}),
                AttributeValue.market.in_({m for _, m in wanted}),
                AttributeValue.value.is_not(None),
            )
        ).all()

        # What each option says it is, so a sibling can be asked to prove it
        # is the same garment and not merely the same base code.
        garment = self._garment_attributes(
            {c.entity_key for c in candidates}
            | {entity_key for entity_key, _a, _m, _v in rows}
        )

        # (base, attribute, market) -> normalised text -> the colours carrying it
        seen: dict[tuple[str, int, str], dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        original: dict[str, str] = {}
        for entity_key, attribute_id, market, value in rows:
            if not usable_as_translation(value):
                continue
            key = _norm(value)
            if not key:
                continue
            seen[(_base_code(entity_key), attribute_id, market)][key].add(entity_key)
            original.setdefault(key, value)

        out: dict[tuple[str, int, str], str] = {}
        for candidate in candidates:
            group = seen.get(
                (_base_code(candidate.entity_key), candidate.attribute_id, candidate.market)
            )
            if not group:
                continue
            # Two distinct colours agreeing is the whole guard. One colour
            # proves nothing: its text may name that colour.
            mine = garment.get(candidate.entity_key, {})
            agreed = [
                text
                for text, colours in group.items()
                if len({
                    c
                    for c in colours
                    if c != candidate.entity_key
                    and same_garment(mine, garment.get(c, {}))
                })
                >= 2
            ]
            if len(agreed) != 1:
                # None, or several rival texts — in both cases the catalogue
                # is not telling us one answer, so it does not get to guess.
                continue
            out[candidate.cell] = original[agreed[0]]
        return out

    def _garment_attributes(
        self, entity_keys: set[str]
    ) -> dict[str, dict[str, str]]:
        """`{model_color: {attribute key: value}}` over `_GARMENT_KEYS`."""
        if not entity_keys:
            return {}
        ids = {
            row.id: row.key
            for row in self._db.scalars(
                select(Attribute).where(Attribute.key.in_(_GARMENT_KEYS))
            )
        }
        if not ids:
            return {}
        out: dict[str, dict[str, str]] = defaultdict(dict)
        for entity_key, attribute_id, value in self._db.execute(
            select(
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.value,
            ).where(
                AttributeValue.state_lane == "current",
                AttributeValue.entity_kind == "model_color",
                AttributeValue.entity_key.in_(entity_keys),
                AttributeValue.attribute_id.in_(ids),
                AttributeValue.value.is_not(None),
            )
        ).all():
            out[entity_key][ids[attribute_id]] = value
        return out




class GroupingRowSource:
    """Text the style's own grouping row carries, in the market's language.

    Free, and the only reader of the `base_code` rows `sync/values` writes.
    The grouping row is not a size and the storefront never renders it, so
    text found there and not on the sizes is text no customer has seen —
    which is exactly what a proposal exists to change: a person accepts it,
    the push writes it onto the sizes, and the site follows.
    """

    name = "grouping_row"

    def __init__(self, db: Session) -> None:
        self._db = db

    def propose(self, candidates: list[Candidate]) -> dict[tuple[str, int, str], str]:
        wanted = {(c.attribute_id, c.market) for c in candidates}
        if not wanted:
            return {}

        # Filtered on attribute and market, not on base code: eighteen
        # thousand keys is past SQLite's parameter ceiling, and the base_code
        # rows are few — one per style per field per market.
        rows = self._db.execute(
            select(
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.market,
                AttributeValue.value,
            ).where(
                AttributeValue.state_lane == "current",
                AttributeValue.entity_kind == "base_code",
                AttributeValue.attribute_id.in_({a for a, _ in wanted}),
                AttributeValue.market.in_({m for _, m in wanted}),
                AttributeValue.value.is_not(None),
            )
        ).all()

        held: dict[tuple[str, int, str], str] = {}
        for base_code, attribute_id, market, value in rows:
            # The sibling rule's guard, for the sibling rule's reason: a
            # grouping row holding short English is the same defect one level
            # up, and offered *as* a translation it is a plausible lie.
            if not usable_as_translation(value):
                continue
            held[(base_code, attribute_id, market)] = value

        out: dict[tuple[str, int, str], str] = {}
        for candidate in candidates:
            text = held.get(
                (_base_code(candidate.entity_key), candidate.attribute_id, candidate.market)
            )
            if text is not None:
                out[candidate.cell] = text
        return out


class MachineSource:
    """A translator behind a key. Inert, and honest about it, without one.

    Kept as a seam rather than a decision. The free source reaches 17% of the
    backlog; the rest is about 1.2M characters, which is pennies through a
    small model and nothing at all while `translator` is None.
    """

    name = "machine"

    def __init__(self, translator: Any | None = None) -> None:
        self._translator = translator

    def propose(self, candidates: list[Candidate]) -> dict[tuple[str, int, str], str]:
        if self._translator is None:
            if candidates:
                log.info("proposals_no_translator", candidates=len(candidates))
            return {}
        return self._translator(candidates)


def find_untranslated(
    db: Session, *, markets: set[str] | None = None, limit: int | None = None
) -> list[Candidate]:
    """Live values in a writable field that read as English or Turkish.

    Restricted to cells the push could actually carry — a proposal for a field
    this panel cannot write is a suggestion nobody can act on. Values that
    already have a proposal waiting are left out, so a second pass does not
    re-ask a source for an answer somebody has not looked at yet.
    """
    targets = write_targets(db)
    writable = set(targets)
    if not writable:
        return []

    pending = {
        (entity_key, attribute_id, market)
        for entity_key, attribute_id, market in db.execute(
            select(
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.market,
            ).where(AttributeValue.state_lane == PROPOSAL_LANE)
        ).all()
    }

    # Narrowed in SQL, not after. Production holds ~750k attribute values and
    # only a handful of attributes are writable; filtering in Python meant
    # loading the lot to discard most of it, and the language test is the
    # expensive part per row that survives.
    wanted_attributes = {attribute_id for attribute_id, _market in writable}
    wanted_markets = {market for _attribute, market in writable}
    if markets:
        wanted_markets &= markets
    if not wanted_markets:
        return []

    rows = db.execute(
        select(
            AttributeValue.entity_key,
            AttributeValue.attribute_id,
            AttributeValue.market,
            AttributeValue.value,
        ).where(
            AttributeValue.state_lane == "current",
            AttributeValue.entity_kind == "model_color",
            AttributeValue.attribute_id.in_(wanted_attributes),
            AttributeValue.market.in_(wanted_markets),
            AttributeValue.value.is_not(None),
        )
    ).all()

    out: list[Candidate] = []
    for entity_key, attribute_id, market, value in rows:
        # The pair still has to be checked: the sets above are the union of
        # each side, so they admit combinations no target actually allows.
        if (attribute_id, market) not in writable:
            continue
        if (entity_key, attribute_id, market) in pending:
            continue
        language = reads_foreign(value)
        if language is None:
            continue
        out.append(
            Candidate(
                entity_key=entity_key, attribute_id=attribute_id, market=market,
                current=value, language=language,
            )
        )
        if limit and len(out) >= limit:
            break
    return out


def offer_shared_key(
    db: Session,
    offered: dict[tuple[str, int, str], str],
    attribute_ids: set[int],
    *,
    sync_run_id: int | None = None,
) -> dict[str, int]:
    """Propose the shared key's text where the shop shows the wrong words or none.

    `offered` is `{(model_color, attribute_id, market): text}` — what the shared
    key holds under each market's locale, already settled across sizes.
    `attribute_ids` is every attribute a shared key is read for, so this pass
    can take back its own proposals even on a night that offers nothing.

    Called by `sync/values` after the live values are written, so "what the
    shop shows" is this run's reading and not last night's. A cell is proposed
    only when all of these hold:

    - the panel can push it (`write_targets`) — otherwise nobody can act on it;
    - no person's value is in flight on it (`approved`/`exported`);
    - its live text is certainly English or Turkish, or it has none and the
      option is listed on that market's shop — an unlisted option's empty
      description is not something a customer is reading;
    - the shared key's text is `usable_as_translation` — it holds English and
      Turkish too, and those are the defect, not the cure;
    - no other source's proposal is already waiting for review there.

    The cheap tests run first and the detector last, because most cells stop
    at "the shop's text is already in the market's language".

    Proposals this source made earlier that no longer meet those terms are
    withdrawn. Nothing here touches the `current` lane or commits.
    """
    stats: Counter[str] = Counter()
    if not attribute_ids:
        return dict(stats)
    writable = set(write_targets(db))
    labels = {
        a.id: a.label for a in db.scalars(select(Attribute).where(Attribute.id.in_(attribute_ids)))
    }
    held = {
        (entity_key, attribute_id, market): (value, state)
        for entity_key, attribute_id, market, value, state in db.execute(
            select(
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.market,
                AttributeValue.value,
                AttributeValue.state,
            ).where(
                AttributeValue.state_lane == "current",
                AttributeValue.entity_kind == "model_color",
                AttributeValue.attribute_id.in_(attribute_ids),
            )
        )
    }
    waiting = {
        (row.entity_key, row.attribute_id, row.market): row
        for row in db.scalars(
            select(AttributeValue).where(
                AttributeValue.state_lane == PROPOSAL_LANE,
                AttributeValue.entity_kind == "model_color",
                AttributeValue.attribute_id.in_(attribute_ids),
            )
        )
    }
    listed = {
        (model_color, market)
        for model_color, market in db.execute(
            select(StorefrontListing.model_color, StorefrontListing.market).where(
                StorefrontListing.left_at.is_(None)
            )
        )
    }

    records: list[dict[str, Any]] = []
    kept: set[tuple[str, int, str]] = set()
    for cell, text in sorted(offered.items()):
        entity_key, attribute_id, market = cell
        if (attribute_id, market) not in writable:
            stats["not_writable"] += 1
            continue
        value, state = held.get(cell, (None, None))
        if state in PROTECTED_STATES:
            stats["in_flight"] += 1
            continue
        mine = waiting.get(cell)
        if mine is not None and not (mine.source_ref or "").startswith(SHARED_KEY):
            stats["other_source_waiting"] += 1
            continue
        shown = (value or "").strip()
        label = labels.get(attribute_id, "value").lower()
        if shown:
            language = reads_foreign(shown)
            if language is None or _norm(shown) == _norm(text):
                stats["shop_text_is_local"] += 1
                continue
            why = f"the {market} shop shows this {label} in {language.title()}"
        else:
            if (entity_key, market) not in listed:
                stats["not_listed"] += 1
                continue
            language = None
            why = f"the {market} shop sells this option and shows no {label}"
        if not usable_as_translation(text):
            stats["not_a_translation"] += 1
            continue
        kept.add(cell)
        records.append({
            "entity_kind": "model_color",
            "entity_key": entity_key,
            "attribute_id": attribute_id,
            "market": market,
            "state_lane": PROPOSAL_LANE,
            "value": text,
            "state": PROPOSED,
            "entry_grain": "model_color",
            "source_kind": "proposal",
            "source_ref": f"{SHARED_KEY}:{(language or 'missing').lower()}",
            "suspect_flag": {"ENGLISH": "EN?", "TURKISH": "TR?"}.get(language or ""),
            "suspect_reason": (
                f"{why}; Omnitron's shared key holds this text under the {market} "
                "locale, where no shop reads it"
            ),
        })

    for cell, row in waiting.items():
        if (
            cell not in kept
            and row.state == PROPOSED
            and (row.source_ref or "").startswith(SHARED_KEY)
        ):
            db.delete(row)
            stats["withdrawn"] += 1

    if records:
        result = upsert_attribute_values(db, records, sync_run_id=sync_run_id)
        stats["new"] = result.inserted
        stats["changed"] = result.updated
    stats["proposed"] = len(records)
    db.flush()
    log.info("shared_key_offered", offered=len(offered), **stats)
    return dict(stats)


def withdraw_settled(db: Session) -> int:
    """Remove waiting proposals whose cell now holds text in a market language.

    Every proposal is raised against a live value in English or Turkish, or
    against no value at all. When the live value later reads as neither — a
    person fixed it in Omnitron, or the panel started reading the key the shop
    renders — the proposal would offer to replace good text, and accepting it
    would push that replacement to the shop. Checked with the same
    `reads_foreign` that raised it, so an unchanged value keeps its proposal.
    """
    waiting = db.scalars(
        select(AttributeValue).where(
            AttributeValue.state_lane == PROPOSAL_LANE,
            AttributeValue.entity_kind == "model_color",
            AttributeValue.state == PROPOSED,
        )
    ).all()
    if not waiting:
        return 0
    held = {
        (entity_key, attribute_id, market): value
        for entity_key, attribute_id, market, value in db.execute(
            select(
                AttributeValue.entity_key,
                AttributeValue.attribute_id,
                AttributeValue.market,
                AttributeValue.value,
            ).where(
                AttributeValue.state_lane == "current",
                AttributeValue.entity_kind == "model_color",
                AttributeValue.attribute_id.in_({row.attribute_id for row in waiting}),
            )
        )
    }
    gone = 0
    for row in waiting:
        shown = (held.get((row.entity_key, row.attribute_id, row.market)) or "").strip()
        if shown and reads_foreign(shown) is None:
            db.delete(row)
            gone += 1
    if gone:
        db.flush()
        log.info("proposals_withdrawn", count=gone)
    return gone


def generate(
    db: Session,
    sources: list[Source],
    *,
    markets: set[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    changed_by: str | None = None,
    sync_run_id: int | None = None,
) -> dict[str, Any]:
    """Find wrong-language values, ask each source, write what comes back.

    Sources are asked in order and the first answer for a cell wins, so the
    cheap and certain source goes first and a paid one is only asked what the
    free one could not settle.

    Every wrong-language value is offered to the sources; `limit` caps what is
    *written*. Detection over the whole set is the cost of that and it is a
    minute or so, which is the right trade for a stage a person runs: the
    alternative examined an arbitrary slice and reported the sources as unable
    to help when they had simply not been asked.

    Proposals whose cell no longer needs one are withdrawn first, so the queue
    this pass adds to is one worth reviewing — see `withdraw_settled`.
    """
    withdrawn = withdraw_settled(db)
    if withdrawn:
        db.commit()
    candidates = find_untranslated(db, markets=markets)
    stats: dict[str, Any] = {
        "candidates": len(candidates),
        "proposed": 0,
        "withdrawn": withdrawn,
        "by_source": {},
        "by_language": dict(Counter(c.language for c in candidates)),
    }
    if not candidates:
        return stats

    answered: dict[tuple[str, int, str], tuple[str, str]] = {}
    for source in sources:
        remaining = [c for c in candidates if c.cell not in answered]
        if not remaining:
            break
        got = source.propose(remaining)
        for cell, text in got.items():
            if cell in answered or not (text or "").strip():
                continue
            answered[cell] = (text, source.name)
        stats["by_source"][source.name] = len(got)

    by_cell = {c.cell: c for c in candidates}
    stats["answerable"] = len(answered)
    records = []
    for cell, (text, source_name) in list(answered.items())[:limit]:
        candidate = by_cell[cell]
        if _norm(text) == _norm(candidate.current):
            # The source handed back what is already there. Not a proposal.
            continue
        entity_key, attribute_id, market = cell
        records.append({
            "entity_kind": "model_color",
            "entity_key": entity_key,
            "attribute_id": attribute_id,
            "market": market,
            "state_lane": PROPOSAL_LANE,
            "value": text,
            "state": PROPOSED,
            "entry_grain": "model_color",
            "source_kind": "proposal",
            "source_ref": f"{source_name}:{candidate.language.lower()}",
            "suspect_flag": "TR?" if candidate.language == "TURKISH" else "EN?",
            "suspect_reason": (
                f"the live {market} value reads as {candidate.language.title()}; "
                f"this replacement came from `{source_name}`"
            ),
        })

    if records:
        upsert_attribute_values(
            db, records, sync_run_id=sync_run_id, changed_by=changed_by
        )
        db.commit()
    stats["proposed"] = len(records)
    stats["held_back"] = max(0, len(answered) - len(records))
    log.info("proposals_generated", **{k: v for k, v in stats.items() if k != "by_language"})
    return stats
