"""Attribute values, read from the mirror for the Values sheet.

The one module in `reports/` that reads value *text*. `reports/coverage.py` is
deliberately the opposite — "only presence travels, never values" — and it is
imported by the extract generator, so a values reader living there would blur
the single boundary this codebase is most careful about. Here it is a filename.

## Why this may read the database when the workbook may not

`admin/coverage_api` builds its counts from `coverage.json` so the file and the
screen cannot drift. The extract carries presence only, so values can come from
nowhere else than the mirror — but the invariant survives, because what the
extract still decides is *which options the sheet is about*.

`load_values` is handed the ModelColors the report already resolved. It fills
cells for those and can neither add an option nor drop one. So a download can
never disagree with the page about how many options there are, only about what
one of them says — and the sheet prints both read times so that much is stated
rather than discovered.

## The grain, and what is invisible here

Values are keyed by entity kind. A `sku`-grain attribute (price, stock, EAN) is
rolled up to its ModelColor the same way `load_catalogue` does it, first
non-empty wins, and where two sizes disagree the count is reported rather than
the disagreement hidden — that is the known name-versus-detail defect, not a
tie to be broken quietly.

A `base_code`-grain row is skipped, which matches `load_catalogue` exactly. It
is a real blind spot in both, and it should stay one shared blind spot: a second
mapping here would put a value on the Values sheet for an option the Summary
sheet counts as having none.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Attribute, AttributeValue, Product
from ..rules.coverage import MARKETS, SHARED

#: How many columns one sheet may carry. A wide sheet is unreadable long before
#: it is slow; this is a usability bound, not a performance one.
MAX_VALUE_COLUMNS = 20

#: Options × columns. Checked before a single row is read, because it is pure
#: arithmetic and refusing early costs one small SELECT.
MAX_VALUE_CELLS = 50_000

#: The same, at size grain, where it has to be a different number.
#:
#: A per-option sheet is something you read; 50,000 cells is generous for that.
#: A per-SKU sheet is something you *import from* — Omnitron writes by SKU — so
#: the natural request is the whole catalogue with a handful of columns, and the
#: whole catalogue is 47,135 SKUs. Under the old number that request was refused
#: at two columns, which made the sheet useless for the thing it is for.
#:
#: 200,000 fits the catalogue at four value columns with room to grow. It is not
#: a memory bound and is not trying to be — `MAX_VALUE_CHARS` still is, and it
#: bites first on anything long. This one only stops a request that is obviously
#: a mistake, and at size grain "obviously a mistake" starts much higher.
MAX_SIZE_CELLS = 200_000

#: Total characters across every cell, checked after the read and before the
#: build. **This is the limit that actually bounds memory.** Fifty thousand
#: cells of `sub_category` is nothing; fifty thousand descriptions is thirteen
#: megabytes of text and a several-hundred-megabyte openpyxl peak. A cell count
#: cannot tell those two apart.
MAX_VALUE_CHARS = 8_000_000

#: Excel refuses a longer cell. openpyxl does not enforce it — it will happily
#: write 32,768 characters and produce a file Excel then rejects — so the guard
#: has to live here. The longest description in the mirror is 1,716 characters,
#: so this should never fire; it exists so that the day it does, the result is
#: a note on a sheet rather than a file nobody can open.
EXCEL_CELL_LIMIT = 32_767


class TooManyValues(ValueError):
    """The selection is larger than one download can carry.

    Carries the numbers rather than a bare message so the caller can put the
    actual figures in front of the person who has to narrow the scope.
    """

    #: How to narrow it. The same sentence whichever limit was hit, because
    #: the answer is the same: fewer options, or fewer columns.
    REMEDY = (
        "Narrow the scope — a season, a sub-category, or an uploaded list of "
        "codes — or untick some markets."
    )

    def __init__(
        self,
        *,
        options: int,
        columns: int,
        limit: int,
        measured: int | None = None,
        measuring: str = "values",
    ) -> None:
        self.options = options
        self.columns = columns
        self.limit = limit
        self.measured = options * columns if measured is None else measured
        self.measuring = measuring
        super().__init__(
            f"That selection would export {self.measured:,} {measuring} across "
            f"{options:,} options and {columns:,} columns, which is more than "
            f"one download can carry. {self.REMEDY} The limit is {limit:,} "
            f"{measuring}."
        )


@dataclass(frozen=True, slots=True)
class ValueSelection:
    """One attribute the reader asked to see, and the markets they ticked.

    Empty `markets` on a per-market attribute means all four, matching
    `Requirement.scopes` and the picker's `scopesFor`. On a market-agnostic
    attribute it means what it always means: there is one value, not four.
    """

    attribute: str
    markets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValueColumn:
    """One column on the sheet — an attribute, in one market."""

    attribute: str
    label: str
    market: str
    per_market: bool
    data_type: str
    value_grain: str


@dataclass(frozen=True, slots=True)
class SizeRow:
    """One SKU, when the sheet is at size grain.

    `sku` is the key values are stored against; `base_code_format` is the
    MODEL.COLOR.SIZE code the reader asked to see. They are separate columns on
    `Product` and are not interchangeable — Omnitron's SKU is its own string,
    and keying the sheet on the MCS would silently drop any SKU whose MCS is
    blank or duplicated.
    """

    sku: str
    base_code_format: str
    model_color: str
    size: str


@dataclass
class ValueBlock:
    """Everything the Values sheet needs, and everything it should admit to."""

    columns: list[ValueColumn]
    #: Row key → column index → text. Sparse: an absent index is a blank cell,
    #: and a row with no values at all holds no dict. The key is a ModelColor
    #: normally and a SKU when `size_rows` is set — `row()` is the only thing
    #: that needs to know which.
    cells: dict[str, dict[int, str]] = field(default_factory=dict)
    read_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Asked for, but no such attribute. Reported, never raised — an unknown
    #: key is a bug in the request, not a gap in the product.
    unknown_keys: tuple[str, ...] = ()
    #: The SKUs this sheet is about, in sheet order. Empty means the sheet is
    #: at ModelColor grain, which is the default and the shape every other
    #: sheet in the workbook uses.
    size_rows: tuple[SizeRow, ...] = ()
    #: Column indexes whose values were rolled up from SKU grain. Always empty
    #: at size grain: there is nothing to roll up when the row *is* the size,
    #: which is the entire reason that sheet exists.
    rolled_up: tuple[int, ...] = ()
    #: Rolled-up cells whose SKUs did not agree. One is a defect worth naming.
    disagreements: int = 0
    #: Cells that held control characters Excel cannot store.
    scrubbed: int = 0
    #: Cells cut at `EXCEL_CELL_LIMIT`.
    truncated: int = 0

    @property
    def by_size(self) -> bool:
        """Whether this block is one row per SKU rather than per option."""
        return bool(self.size_rows)

    def row(self, key: str) -> list[str]:
        """The cells for one row, aligned to `columns`.

        `key` is a ModelColor normally and a SKU at size grain. The one place
        that alignment lives: the sheet writer never indexes by name, so a
        column added to the header and forgotten here would be a visible hole
        rather than a silent shift.
        """
        held = self.cells.get(key, {})
        return [held.get(index, "") for index in range(len(self.columns))]

    def filled(self, index: int) -> int:
        """How many options carry a value in this column.

        Zero is worth drawing differently: a column nobody filled looks exactly
        like a column that failed to load, and only one of those is news.
        """
        return sum(1 for held in self.cells.values() if index in held)

    @property
    def cell_count(self) -> int:
        return sum(len(held) for held in self.cells.values())


def columns_for(
    selections: Sequence[ValueSelection],
    registry: dict[str, Attribute],
) -> tuple[list[ValueColumn], tuple[str, ...]]:
    """The columns a selection asks for, in the order they should appear.

    Selection order first, then `MARKETS` order within an attribute, so a
    reader pasting a VLOOKUP against the sheet never has to guess where a
    market landed.
    """
    columns: list[ValueColumn] = []
    unknown: list[str] = []
    for selection in selections:
        attribute = registry.get(selection.attribute)
        if attribute is None:
            unknown.append(selection.attribute)
            continue
        if not attribute.per_market:
            # One value, so one column, whatever was ticked. The picker draws
            # this as a single box against four, and the sheet has to keep that
            # promise or the two stop describing the same thing.
            markets = (SHARED,)
        elif selection.markets:
            markets = tuple(m for m in MARKETS if m in selection.markets)
        else:
            markets = MARKETS
        for market in markets:
            columns.append(
                ValueColumn(
                    attribute=attribute.key,
                    label=attribute.label,
                    market=market,
                    per_market=bool(attribute.per_market),
                    data_type=attribute.data_type,
                    value_grain=attribute.value_grain,
                )
            )
    return columns, tuple(unknown)


def _clean(text: str) -> tuple[str, bool, bool]:
    """A value Excel will accept, and whether anything had to change.

    Control characters are removed and nothing else is. This is not HTML
    stripping — a vertical tab is not content, and a description's markup is
    exactly what the reader asked for.
    """
    scrubbed = ILLEGAL_CHARACTERS_RE.sub("", text)
    was_scrubbed = scrubbed != text
    if len(scrubbed) > EXCEL_CELL_LIMIT:
        return scrubbed[:EXCEL_CELL_LIMIT], was_scrubbed, True
    return scrubbed, was_scrubbed, False


def load_values(
    db: Session,
    selections: Sequence[ValueSelection],
    model_colors: Sequence[str],
    *,
    by_size: bool = False,
    max_columns: int = MAX_VALUE_COLUMNS,
    max_cells: int | None = None,
    max_chars: int = MAX_VALUE_CHARS,
) -> ValueBlock:
    """Read the chosen attributes for the chosen options.

    `model_colors` comes from the report, which came from the extract. This
    function narrows to it and never widens past it — at either grain. A size
    row can only be a size of an option the report already resolved, so the
    "cannot add or drop an option" bound holds exactly as it does at
    ModelColor grain; the sheet just says more about the same population.

    `by_size` puts one row per SKU instead of one per option, which is the only
    way the values stored per size are visible at all. Price and stock live at
    `sku` grain, so at ModelColor grain they are rolled up first-non-empty-wins
    and the sizes that disagree are counted rather than shown (15.1% of
    multi-market SKUs differ on price). This is that count, unrolled.

    The cost is rows: an option is five or six SKUs, so `max_cells` bites about
    six times sooner and a scope that downloads fine per option may have to be
    narrowed. That is a real limit and it is reported as one, with the numbers.
    """
    # Grain decides the cap, so a caller cannot pick the option-grain number
    # for a per-SKU sheet by forgetting to pass one.
    if max_cells is None:
        max_cells = MAX_SIZE_CELLS if by_size else MAX_VALUE_CELLS

    registry = {
        attribute.key: attribute
        for attribute in db.scalars(
            select(Attribute).where(
                Attribute.key.in_({s.attribute for s in selections})
            )
        )
    }
    columns, unknown = columns_for(selections, registry)
    # No columns and no size request is nothing to do. No columns *with* one is
    # a request for the key columns alone — a list of every SKU in scope, which
    # is a thing people ask for and a legitimate sheet.
    if not columns and not by_size:
        return ValueBlock(columns=[], unknown_keys=unknown)

    if len(columns) > max_columns:
        raise TooManyValues(
            options=len(model_colors),
            columns=len(columns),
            limit=max_columns,
            measured=len(columns),
            measuring="columns",
        )
    in_scope = set(model_colors)

    # The SKU table is read up front at size grain, because it decides the row
    # count and the row count decides whether this request is answerable at
    # all. Same `delisted_at` predicate as `load_catalogue`, so a delisted SKU
    # is invisible in both rather than appearing on one sheet and not another.
    size_rows: tuple[SizeRow, ...] = ()
    skus_of: dict[str, list[str]] = {}
    if by_size:
        size_rows = tuple(
            SizeRow(
                sku=sku,
                base_code_format=mcs or "",
                model_color=mc,
                size=size or "",
            )
            for sku, mcs, mc, size in sorted(
                db.execute(
                    select(
                        Product.sku,
                        Product.base_code_format,
                        Product.model_color,
                        Product.size,
                    ).where(Product.is_size())
                ).all(),
                # By the code the reader sees, not by the key we store against,
                # so the sheet reads in the order a person would sort it.
                key=lambda r: (r[2], r[1] or "", r[0]),
            )
            if mc in in_scope
        )
        for row in size_rows:
            skus_of.setdefault(row.model_color, []).append(row.sku)

    sheet_rows = len(size_rows) if by_size else len(model_colors)
    if sheet_rows * len(columns) > max_cells:
        raise TooManyValues(
            options=sheet_rows, columns=len(columns), limit=max_cells
        )

    # column index, keyed the way a scanned row arrives.
    index_of = {(c.attribute, c.market): i for i, c in enumerate(columns)}
    wanted_ids = {registry[c.attribute].id for c in columns}
    wanted_markets = {c.market for c in columns}

    # Built only when something asked for is stored per size. At size grain the
    # SKU *is* the row key, so the mapping is needed the other way round only
    # to decide whether a SKU is in scope.
    sku_to_mc: dict[str, str] = {}
    if by_size:
        sku_to_mc = {row.sku: row.model_color for row in size_rows}
    elif any(c.value_grain == "sku" for c in columns):
        sku_to_mc = dict(
            db.execute(
                select(Product.sku, Product.model_color).where(Product.is_size())
            ).all()
        )

    cells: dict[str, dict[int, str]] = {}
    # Nothing is rolled up at size grain — the row is the size, so a per-size
    # value lands on its own row instead of competing for one option's cell.
    rolled_up: set[int] = (
        set() if by_size else {i for i, c in enumerate(columns) if c.value_grain == "sku"}
    )
    disagreements = 0
    scrubbed = 0
    truncated = 0
    chars = 0

    rows = db.execute(
        select(
            AttributeValue.entity_kind,
            AttributeValue.entity_key,
            AttributeValue.attribute_id,
            AttributeValue.market,
            AttributeValue.value,
        )
        .where(
            AttributeValue.state_lane == "current",
            AttributeValue.attribute_id.in_(wanted_ids),
            AttributeValue.market.in_(wanted_markets),
        )
        # Not decoration. Where several SKUs of one option carry a value, the
        # first one read wins, and without an order that winner changes between
        # runs — which makes the same request produce two different files.
        .order_by(AttributeValue.entity_key)
    )

    # Deliberately no `entity_key IN (...)`. Eighteen thousand ModelColors and
    # ninety thousand SKUs are far past SQLite's parameter ceiling, and the
    # chunked version would be a second code path that eventually disagrees
    # with this one. Filtering on `attribute_id` alone reads roughly 45,000
    # rows for a description, against the 772,450-row unfiltered scan the
    # extract generator already runs nightly.
    #
    # Nor any filter on `state`. `load_catalogue` does not have one either, and
    # a different definition of "we hold a value" here is precisely the drift
    # this design exists to bound.
    key_of = {attribute.id: attribute.key for attribute in registry.values()}
    for entity_kind, entity_key, attribute_id, market, value in rows:
        if value is None or not str(value).strip():
            continue
        index = index_of.get((key_of[attribute_id], market))
        if index is None:
            continue
        # A base_code-grain row falls through to None here and is skipped, the
        # same way it is skipped in `load_catalogue`.
        mc = entity_key if entity_kind == "model_color" else sku_to_mc.get(entity_key)
        if mc is None or mc not in in_scope:
            continue

        # Which row(s) this value belongs to. At ModelColor grain there is
        # always exactly one. At size grain a per-size value goes to its own
        # SKU, and an option-grain value — a name, a description — is repeated
        # down every size of the option, because that is what it means: one
        # value that applies to all of them.
        if not by_size:
            keys: tuple[str, ...] = (mc,)
        elif entity_kind == "model_color":
            keys = tuple(skus_of.get(mc, ()))
        else:
            keys = (entity_key,)

        text = str(value)
        cleaned: str | None = None
        for key in keys:
            held = cells.setdefault(key, {})
            if index in held:
                if held[index] != text:
                    disagreements += 1
                continue

            if cleaned is None:
                cleaned, was_scrubbed, was_truncated = _clean(text)
                scrubbed += was_scrubbed
                truncated += was_truncated
            held[index] = cleaned
            chars += len(cleaned)
            if chars > max_chars:
                raise TooManyValues(
                    options=sheet_rows,
                    columns=len(columns),
                    limit=max_chars,
                    measured=chars,
                    measuring="characters",
                )

    # Read-only, but it ends the implicit transaction and hands the connection
    # back before the caller spends several seconds inside openpyxl holding it.
    db.rollback()

    return ValueBlock(
        columns=columns,
        cells=cells,
        read_at=datetime.now(UTC),
        unknown_keys=unknown,
        size_rows=size_rows,
        rolled_up=tuple(sorted(rolled_up)),
        disagreements=disagreements,
        scrubbed=scrubbed,
        truncated=truncated,
    )
