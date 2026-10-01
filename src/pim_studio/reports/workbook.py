"""The coverage report as a downloadable workbook.

Still a read — it mutates no row and calls no API — but it writes a file, which
is the point: the number on the dashboard is for the person looking at it, and
this is for the four people who were not.

The layout follows the report this team already produces by hand, so the file
that arrives from the panel is recognisable as the same document rather than a
new thing to learn. What it adds is everything the hand-made version could not
carry:

**A third column beside met and missing.** `unknown` has its own column on
every table. A fill rate computed over judgeable values only, with the count it
could not judge printed next to it, is the difference between "Slovakia is at
0%" and "we have never read a Slovak file".

**A remedy on every row.** Each requirement names who fixes it and through
which channel, so the reader can sort by owner and see the size of their own
queue rather than the size of the problem.

**Its own caveats.** `Methodology` restates the rules and carries whatever the
report flagged — the stock provenance note among them. A caveat that lives only
in the UI does not survive being emailed, and this file will be emailed.

Formulas rather than baked numbers for the percentage columns, with
`fullCalcOnLoad` set so they show values the moment the file opens. Anyone who
filters the detail sheet gets percentages that still describe what they are
looking at.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..rules.coverage import ISSUES_BY_KEY, SHARED, Owner, Standing
from .coverage import CoverageReport
from .values import ValueBlock, ValueColumn

FONT = "Arial"

TITLE_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FILL = PatternFill("solid", fgColor="2E75B6")
SUBHEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
BAD_FILL = PatternFill("solid", fgColor="FCE4D6")
GOOD_FILL = PatternFill("solid", fgColor="E2EFDA")
#: Neither good nor bad. `unknown` gets its own fill for the same reason it
#: gets its own column — colouring it red would assert a gap we cannot show,
#: and colouring it green would assert a pass we cannot show either.
UNKNOWN_FILL = PatternFill("solid", fgColor="EDEDED")
STRIPE_FILL = PatternFill("solid", fgColor="F7F9FC")

TITLE_FONT = Font(name=FONT, size=14, bold=True, color="FFFFFF")
HEADER_FONT = Font(name=FONT, size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name=FONT, size=10)
NOTE_FONT = Font(name=FONT, size=9, color="595959")
BOLD = Font(name=FONT, size=10, bold=True)

THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

OWNER_LABEL = {
    Owner.US: "Us",
    Owner.PARTNER: "Turkey (partner)",
    Owner.SYSTEM: "Feed / nobody",
}

STANDING_LABEL = {
    Standing.COMPLETE: "Complete",
    Standing.INCOMPLETE: "Incomplete",
    Standing.INDETERMINATE: "Indeterminate",
}


def _title(sheet: Worksheet, text: str, subtitle: str, width: int) -> int:
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    cell = sheet.cell(row=1, column=1, value=text)
    cell.font = TITLE_FONT
    cell.fill = TITLE_FILL
    cell.alignment = Alignment(vertical="center", indent=1)
    sheet.row_dimensions[1].height = 26

    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
    note = sheet.cell(row=2, column=1, value=subtitle)
    note.font = NOTE_FONT
    note.alignment = Alignment(vertical="center", indent=1)
    sheet.row_dimensions[2].height = 18
    return 4


def _header(sheet: Worksheet, row: int, labels: list[str]) -> int:
    for column, label in enumerate(labels, start=1):
        cell = sheet.cell(row=row, column=column, value=label)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = BOX
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[row].height = 26
    return row + 1


def _section(sheet: Worksheet, row: int, text: str, width: int) -> int:
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    cell = sheet.cell(row=row, column=1, value=text)
    cell.font = BOLD
    cell.fill = SUBHEAD_FILL
    cell.alignment = Alignment(vertical="center", indent=1)
    return row + 1


def _write(
    sheet: Worksheet,
    row: int,
    values: list[object],
    *,
    fills: dict[int, PatternFill] | None = None,
    bold: bool = False,
    text_from: int | None = None,
) -> int:
    """One row. `text_from` forces every column at or past it to stay text.

    That last part is load-bearing, not tidiness. openpyxl reads a string
    beginning with `=` as a formula — `data_type` comes back `'f'` and it
    survives the save, so Excel opens the cell as `#NAME?` and the value is
    gone. Product copy really does start with `=` occasionally, and the guard
    has to be per cell rather than per attribute because no attribute can
    promise it never will.
    """
    for column, value in enumerate(values, start=1):
        # A control character anywhere in a cell makes openpyxl raise on save
        # and takes the whole download with it. That is not new and not about
        # values — a product name carrying one has always been able to 500 the
        # detail sheet — so the guard belongs on every cell this file writes,
        # not only on the ones read for the Values sheet. `load_values` scrubs
        # as well, because it also has to *count* what it changed.
        if isinstance(value, str):
            value = ILLEGAL_CHARACTERS_RE.sub("", value)
        cell = sheet.cell(row=row, column=column, value=value)
        cell.font = BOLD if bold else BODY_FONT
        cell.border = BOX
        if fills and column in fills:
            cell.fill = fills[column]
        if isinstance(value, (int, float)):
            cell.alignment = Alignment(horizontal="right")
        if text_from is not None and column >= text_from:
            cell.data_type = "s"
            cell.alignment = Alignment(vertical="top")
    return row + 1


def _widths(sheet: Worksheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def _pct(numerator_cell: str, denominator_cell: str) -> str:
    """A percentage that survives an empty denominator.

    `IFERROR` rather than a guard on the count: a division by zero renders as
    `#DIV/0!` across the whole column and makes the sheet look broken when the
    honest answer is that there was nothing to divide.
    """
    return f"=IFERROR({numerator_cell}/{denominator_cell},\"\")"


def _summary_sheet(
    sheet: Worksheet, report: CoverageReport, values: ValueBlock | None = None
) -> None:
    scope = report.scope
    generated = report.generated_at.strftime("%d %b %Y %H:%M UTC")
    subtitle = (
        f"{report.population:,} ModelColors · {scope.describe()} · "
        f"definition of complete: {report.profile.label} · data as at {generated}"
    )
    row = _title(sheet, "Northwind CEE — product coverage", subtitle, width=6)
    _widths(sheet, [44, 14, 12, 14, 20, 46])

    # ── standing against the chosen profile ────────────────────────────────
    row = _section(sheet, row, f"Completeness — {report.profile.label}", 6)
    blurb = sheet.cell(row=row, column=1, value=report.profile.blurb)
    blurb.font = NOTE_FONT
    blurb.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    sheet.row_dimensions[row].height = 28
    row += 1

    row = _header(sheet, row, ["Standing", "Options", "% of judged", "", "", "Meaning"])
    # The percentage column divides by "judged", which is written four rows
    # below the first data row. Named here rather than inlined so the two stay
    # together if a row is ever inserted between them.
    judged_cell = f"$B${row + 4}"
    row = _write(
        sheet,
        row,
        [
            "Complete",
            report.summary.complete,
            _pct(f"B{row}", judged_cell),
            "",
            "",
            "Every required value present.",
        ],
        fills={1: GOOD_FILL},
    )
    row = _write(
        sheet,
        row,
        [
            "Incomplete",
            report.summary.incomplete,
            _pct(f"B{row}", judged_cell),
            "",
            "",
            "At least one required value we hold a source for is empty.",
        ],
        fills={1: BAD_FILL},
    )
    row = _write(
        sheet,
        row,
        [
            "Indeterminate",
            report.summary.indeterminate,
            "",
            "",
            "",
            "Nothing missing, but at least one requirement has no source. "
            "Not counted as complete, and not counted against it.",
        ],
        fills={1: UNKNOWN_FILL},
    )
    row = _write(sheet, row, ["Total options in scope", report.population, ""], bold=True)
    row = _write(
        sheet,
        row,
        [
            "Judged (complete + incomplete)",
            report.summary.judged,
            "",
            "",
            "",
            "The denominator for the percentages above.",
        ],
        bold=True,
    )
    row += 1

    # ── the standing issue flags ───────────────────────────────────────────
    row = _section(sheet, row, "Issues — asked of every option, whatever the profile", 6)
    row = _header(
        sheet, row, ["Issue", "Options", "% of scope", "", "What it means", "What to do"]
    )
    population_cell = f"$B${row + len(report.issues) + 1}"
    for issue in report.issues:
        label = issue.label + (" ⚠" if issue.stock_backed else "")
        row = _write(
            sheet,
            row,
            [
                label,
                issue.count,
                _pct(f"B{row}", population_cell),
                "",
                issue.definition,
                issue.remedy,
            ],
            fills={1: BAD_FILL if issue.count else GOOD_FILL},
        )
    row = _write(sheet, row, ["Options in scope", report.population], bold=True)
    row += 1

    # ── mutually exclusive buckets ─────────────────────────────────────────
    row = _section(
        sheet, row, "Issue overlap — every option in exactly one bucket", 6
    )
    row = _header(sheet, row, ["Bucket", "Options", "% of scope", "", "", ""])
    bucket_start = row
    for key, count in sorted(
        report.buckets.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        row = _write(
            sheet,
            row,
            [_bucket_label(key), count, _pct(f"B{row}", population_cell)],
            fills={1: GOOD_FILL if key == "clean" else BAD_FILL},
        )
    total = (
        f"=SUM(B{bucket_start}:B{row - 1})" if row > bucket_start else 0
    )
    row = _write(
        sheet,
        row,
        ["Total (sanity check — must equal options in scope)", total, ""],
        bold=True,
    )
    row += 1

    # ── who fixes it ───────────────────────────────────────────────────────
    row = _section(sheet, row, "Where the work lands", 6)
    row = _header(
        sheet,
        row,
        ["Owner", "Gaps", "", "Options affected", "Channel", "What to do"],
    )
    for owner in Owner:
        row = _write(
            sheet,
            row,
            [
                OWNER_LABEL[owner],
                report.summary.gaps_by_owner.get(owner, 0),
                "",
                report.summary.products_by_owner.get(owner, 0),
                _CHANNEL[owner],
                _ACTION[owner],
            ],
        )
    row += 1

    # ── methodology ────────────────────────────────────────────────────────
    row = _section(sheet, row, "Methodology and caveats", 6)
    for line in _methodology(report, values):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        cell = sheet.cell(row=row, column=1, value=line)
        cell.font = NOTE_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top", indent=1)
        sheet.row_dimensions[row].height = 14 * (1 + len(line) // 150)
        row += 1


_CHANNEL = {
    Owner.US: "Omnitron import file",
    Owner.PARTNER: "Product-card request",
    Owner.SYSTEM: "Upstream feed",
}

_ACTION = {
    Owner.US: "Generate the import file from this scope and upload it.",
    Owner.PARTNER: "Add these options to the next product-card file for Turkey.",
    Owner.SYSTEM: "Nothing to upload. Chase the source, or accept the absence.",
}


def _bucket_label(key: str) -> str:
    if key == "clean":
        return "Clean — no issues"
    if key.startswith("only:"):
        issue = ISSUES_BY_KEY.get(key[5:])
        return f"Only — {issue.label if issue else key[5:]}"
    parts = [ISSUES_BY_KEY[k].label for k in key.split("+") if k in ISSUES_BY_KEY]
    return " + ".join(parts) or key


def _values_methodology(report: CoverageReport, values: ValueBlock) -> list[str]:
    """What the Values sheet is, said where the Summary reader will see it.

    The same sentences are on the Values sheet itself. They are repeated here
    because the person who is emailed this file reads the Summary and may never
    open the fifth tab, and a caveat only one reader sees is not a caveat.
    """
    read = values.read_at.strftime("%d %b %Y %H:%M UTC")
    built = report.generated_at.strftime("%d %b %Y %H:%M UTC")
    lines = [
        "",
        f"The 'Values' sheet carries {len(values.columns)} attribute column(s) "
        f"read live from the mirror at {read}. Every count in this workbook "
        f"was computed from the coverage extract built at {built}. A value "
        "filled between those two moments appears on that sheet and is still "
        "counted as missing here — the sheet lists exactly the options counted "
        "in this file and can neither add one nor drop one, so the two can "
        "differ about what an option says but never about how many there are.",
    ]
    if values.unknown_keys:
        lines.append(
            "Requested but unknown to the registry, and therefore absent from "
            "that sheet: " + ", ".join(values.unknown_keys) + "."
        )
    if values.rolled_up:
        note = (
            f"{len(values.rolled_up)} of those column(s) hold an attribute "
            "stored per size; the sheet shows one SKU's value for the option."
        )
        if values.disagreements:
            note += (
                f" {values.disagreements:,} of those cells had sizes that "
                "disagree, which is a defect worth chasing rather than a "
                "rounding choice."
            )
        lines.append(note)
    if values.scrubbed:
        lines.append(
            f"{values.scrubbed:,} value(s) contained control characters Excel "
            "cannot store. Those characters were removed; nothing else was "
            "changed."
        )
    if values.truncated:
        lines.append(
            f"{values.truncated:,} value(s) were longer than a cell can hold "
            "and were cut at 32,767 characters."
        )
    return lines


def _methodology(
    report: CoverageReport, values: ValueBlock | None = None
) -> list[str]:
    lines = [
        "Grain — one row per ModelColor (an option: one style in one colour). "
        "Sizes sit underneath and are rolled up: an option counts as having a "
        "price or an image if any of its SKUs does.",
        "",
        f"Complete means — {report.profile.label}: {report.profile.blurb}",
        "  " + "; ".join(_requirement_sentences(report)),
        "",
        "Four verdicts, not two. A required value is MET when we hold one, "
        "MISSING when we hold a source that would carry it and it is empty, "
        "UNKNOWN when no source we have read could carry it, and NOT APPLICABLE "
        "when the attribute does not apply to the option's sub-category (a "
        "wallet has no sleeve type).",
        "",
        "UNKNOWN is never counted as a gap and never counted as a pass. Fill "
        "rates divide met by (met + missing), so a column we have never read "
        "does not read as 0%. The count it could not judge is printed beside "
        "every rate.",
        "",
        "Not applicable is excluded from every denominator, so an attribute "
        "that applies to garments is not scored against bags.",
        "",
        "Ownership comes from the attribute registry's write channel, which is "
        "who may write the value — and therefore who can fix it being absent. "
        "It is not a guess about who is responsible.",
    ]
    if report.scope.not_found:
        lines += [
            "",
            f"{len(report.scope.not_found)} uploaded codes matched no product we "
            "hold and are listed on the 'Not found' sheet. They are excluded "
            "from every number here.",
        ]
    if report.caveats:
        lines.append("")
        lines += [f"⚠ {caveat}" for caveat in report.caveats]
    if values is not None and (values.columns or values.by_size):
        lines += _values_methodology(report, values)
    return lines


def _requirement_sentences(report: CoverageReport) -> list[str]:
    out = []
    for requirement in report.profile.requirements:
        meta = None
        for tally in report.requirements:
            if tally.attribute == requirement.attribute:
                meta = tally
                break
        label = meta.label if meta else requirement.attribute
        markets = [t.market for t in report.requirements if t.attribute == requirement.attribute]
        scope = "one shared value" if markets == [SHARED] else ", ".join(sorted(markets))
        out.append(f"{label} ({scope})")
    return out


def _requirements_sheet(sheet: Worksheet, report: CoverageReport) -> None:
    row = _title(
        sheet,
        "Requirements — what is missing, and with how many options",
        "Sorted worst first. 'Fill rate' divides met by (met + missing); unknown "
        "is excluded from both halves and shown in its own column.",
        width=9,
    )
    _widths(sheet, [26, 8, 10, 11, 11, 11, 11, 20, 50])
    row = _header(
        sheet,
        row,
        [
            "Attribute",
            "Market",
            "Met",
            "Missing",
            "Unknown",
            "Not applicable",
            "Fill rate",
            "Who fixes it",
            "What to do",
        ],
    )
    first = row
    for tally in report.requirements:
        fills = {}
        if tally.missing:
            fills[4] = BAD_FILL
        if tally.unknown:
            fills[5] = UNKNOWN_FILL
        if tally.met and not tally.missing:
            fills[3] = GOOD_FILL
        row = _write(
            sheet,
            row,
            [
                tally.label,
                "ALL" if tally.market == SHARED else tally.market,
                tally.met,
                tally.missing,
                tally.unknown,
                tally.na,
                # The fallback names *why* there is no rate. A requirement
                # that matched no option and a column nobody has loaded both
                # divide by zero, and they are entirely different problems.
                f"=IFERROR(C{row}/(C{row}+D{row}),"
                f"\"{'no match' if tally.blind else 'never read'}\")",
                OWNER_LABEL[tally.owner],
                tally.remedy.action,
            ],
            fills=fills,
        )
    for cell in sheet[f"G{first}:G{max(first, row - 1)}"]:
        cell[0].number_format = "0.0%"
    sheet.freeze_panes = sheet.cell(row=first, column=1)
    if row > first:
        sheet.auto_filter.ref = f"A{first - 1}:I{row - 1}"


def _detail_sheet(sheet: Worksheet, report: CoverageReport) -> None:
    row = _title(
        sheet,
        "ModelColor detail",
        "One row per option. Sorted by standing, then by how many required "
        "values are missing.",
        width=12,
    )
    _widths(sheet, [16, 38, 10, 18, 10, 8, 12, 14, 10, 12, 30, 34])
    row = _header(
        sheet,
        row,
        [
            "ModelColor",
            "Name",
            "Season",
            "Sub-category",
            "Gender",
            "SKUs",
            "Catalogues",
            "Standing",
            "Gaps",
            "Best stock",
            "Issues",
            "PL comment",
        ],
    )
    first = row

    def sort_key(product) -> tuple:
        standing = report.standings.get(product.model_color, Standing.COMPLETE)
        rank = {Standing.INCOMPLETE: 0, Standing.INDETERMINATE: 1, Standing.COMPLETE: 2}
        return (
            rank[standing],
            -report.gaps_by_product.get(product.model_color, 0),
            -len(product.flags),
            product.model_color,
        )

    striped = False
    for product in sorted(report.rows, key=sort_key):
        standing = report.standings.get(product.model_color, Standing.COMPLETE)
        gaps = report.gaps_by_product.get(product.model_color, 0)
        issues = ", ".join(
            ISSUES_BY_KEY[flag].label for flag in product.flags if flag in ISSUES_BY_KEY
        )
        fills = {
            8: {
                Standing.COMPLETE: GOOD_FILL,
                Standing.INCOMPLETE: BAD_FILL,
                Standing.INDETERMINATE: UNKNOWN_FILL,
            }[standing]
        }
        if issues:
            fills[11] = BAD_FILL
        if striped:
            for column in range(1, 13):
                fills.setdefault(column, STRIPE_FILL)
        striped = not striped
        row = _write(
            sheet,
            row,
            [
                product.model_color,
                product.name,
                product.season,
                product.sub_category,
                product.gender,
                product.sku_count,
                ", ".join(product.markets) or "none",
                STANDING_LABEL[standing],
                gaps,
                product.best_stock,
                issues,
                product.comment,
            ],
            fills=fills,
        )
    sheet.freeze_panes = sheet.cell(row=first, column=1)
    if row > first:
        sheet.auto_filter.ref = f"A{first - 1}:L{row - 1}"


def _column_label(column: ValueColumn) -> str:
    # The market is named only where there is a choice of market. That is the
    # same asymmetry the picker draws as four boxes against a single dot, and
    # repeating it here keeps the sheet and the control describing one thing.
    return f"{column.label} · {column.market}" if column.per_market else column.label


def _column_width(column: ValueColumn) -> int:
    return 60 if column.data_type == "html" else 24


def _values_title(values: ValueBlock) -> str:
    """What to call the sheet, and the tab.

    "Values" describes an option-grain lookup table. At size grain the same
    sheet is an import file — Omnitron writes by SKU — and someone opening the
    workbook to find their SKUs should not have to guess that "Values" is where
    they went. Named to match "ModelColor Detail", because the pair is the
    point: one row per option there, one row per SKU here.
    """
    return "SKU Detail" if values.by_size else "Values"


def _values_subtitle(report: CoverageReport, values: ValueBlock) -> str:
    read = values.read_at.strftime("%d %b %Y %H:%M UTC")
    built = report.generated_at.strftime("%d %b %Y %H:%M UTC")
    grain = (
        (
            f"One row per size — {len(values.size_rows):,} SKUs across the "
            "options counted elsewhere in this file. Values stored per size "
            "(price, stock) are shown as they are stored; values stored per "
            "option (names, descriptions) repeat down that option's sizes, "
            "because one value is what applies to all of them. "
        )
        if values.by_size
        else ""
    )
    return (
        f"{grain}Values read live from the mirror at {read}. Every other sheet "
        f"in this file was computed from the coverage extract built at {built}, "
        "so the two can disagree: a value that landed in between shows here and "
        "is still counted as missing on the Summary. The options are the same "
        "options either way — this sheet can neither add one nor drop one. A "
        "blank cell means we hold no value; see 'Requirements' for whether that "
        "is missing or never read."
    )


def _values_sheet(
    sheet: Worksheet, report: CoverageReport, values: ValueBlock
) -> None:
    """The chosen attributes, as text, one row per option — or per size.

    Everything about this sheet's shape is derived from `headers`. `_detail_sheet`
    states its column count in four separate places and they have to be kept in
    step by hand; here the width is computed once and the widths, the striping,
    the freeze and the autofilter all read it. That is what lets the size-grain
    variant add four identifying columns without touching anything below.

    At size grain the key columns come first and the MCS leads, because that is
    the code the sheet was asked for. The SKU sits beside it rather than
    instead of it: the MCS is what a person matches on, the SKU is what
    Omnitron is keyed by, and a file carrying only one of them sends the reader
    back to the panel to find the other.
    """
    if values.by_size:
        keys = ["base_code_format", "SKU", "ModelColor", "Size", "Name"]
        key_widths = [22, 22, 16, 10, 38]
    else:
        keys = ["ModelColor", "Name"]
        key_widths = [16, 38]

    headers = [*keys, *(_column_label(c) for c in values.columns)]
    widths = [*key_widths, *(_column_width(c) for c in values.columns)]
    width = len(headers)
    #: Where the values start, and every one of them is text.
    first_value_column = len(keys) + 1

    row = _title(
        sheet, _values_title(values), _values_subtitle(report, values), width=width
    )
    _widths(sheet, widths)
    row = _header(sheet, row, headers)
    first = row

    # An entirely empty column reads exactly like one that failed to load, and
    # only one of those is news. Greying the header says which — the same fill
    # and the same reasoning as `unknown` elsewhere in this file.
    for index in range(len(values.columns)):
        if values.filled(index):
            continue
        cell = sheet.cell(row=first - 1, column=first_value_column + index)
        cell.fill = UNKNOWN_FILL
        cell.font = Font(name=FONT, size=10, bold=True, color="595959")

    # By key, not by standing like the detail sheet. This one is a lookup
    # table — someone pasting a VLOOKUP against it needs a key order that does
    # not move when the definition of complete changes. `size_rows` arrives
    # already in that order.
    names = {product.model_color: product.name for product in report.rows}
    if values.by_size:
        lines = [
            (
                size.sku,
                [
                    size.base_code_format,
                    size.sku,
                    size.model_color,
                    size.size,
                    names.get(size.model_color, ""),
                ],
            )
            for size in values.size_rows
        ]
    else:
        lines = [
            (product.model_color, [product.model_color, product.name])
            for product in sorted(report.rows, key=lambda p: p.model_color)
        ]

    striped = False
    for key, identifying in lines:
        fills = (
            {column: STRIPE_FILL for column in range(1, width + 1)}
            if striped
            else None
        )
        striped = not striped
        row = _write(
            sheet,
            row,
            [*identifying, *values.row(key)],
            fills=fills,
            text_from=first_value_column,
        )

    # Freezes the identifying columns as well as the header. The only sheet
    # here wide enough to lose track of which row you are reading when you
    # scroll right, which is why it is the only one that does this.
    sheet.freeze_panes = sheet.cell(row=first, column=first_value_column)
    if row > first:
        sheet.auto_filter.ref = (
            f"A{first - 1}:{get_column_letter(width)}{row - 1}"
        )


def _not_found_sheet(sheet: Worksheet, report: CoverageReport) -> None:
    row = _title(
        sheet,
        "Not found",
        "Codes in the uploaded list that match no product we hold. They are "
        "excluded from every number in this workbook — which is itself a "
        "finding, not a rounding error.",
        width=2,
    )
    _widths(sheet, [28, 60])
    row = _header(sheet, row, ["Code as written", "Why it may not have matched"])
    for code in report.scope.not_found:
        row = _write(
            sheet,
            row,
            [
                code,
                "No product with this ModelColor is in our Omnitron mirror. "
                "Matching ignores dots, underscores and a size suffix, so this "
                "is usually a product that has not been opened yet, or one from "
                "a catalogue we do not sync.",
            ],
        )


def build_workbook(
    report: CoverageReport, values: ValueBlock | None = None
) -> bytes:
    """The whole report as .xlsx bytes, ready to stream to a browser.

    `values` is optional and defaults to nothing, so a caller that never asked
    for attribute text gets exactly the file it got before this existed.
    """
    workbook = Workbook()
    workbook.remove(workbook.active)

    _summary_sheet(workbook.create_sheet("Summary"), report, values)
    _requirements_sheet(workbook.create_sheet("Requirements"), report)
    _detail_sheet(workbook.create_sheet("ModelColor Detail"), report)
    if values is not None and values.columns:
        _values_sheet(
            workbook.create_sheet(_values_title(values)), report, values
        )
    # `Not found` stays last: it is a footnote about the input rather than a
    # finding about the catalogue.
    if report.scope.not_found:
        _not_found_sheet(workbook.create_sheet("Not found"), report)

    for sheet in workbook.worksheets:
        sheet.sheet_view.showGridLines = False
    # Percentages are formulas so they follow a filtered sheet. Without this
    # they open showing nothing until the reader presses F9, which reads as a
    # broken file.
    workbook.calculation.fullCalcOnLoad = True

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def filename_for(report: CoverageReport, now: datetime | None = None) -> str:
    stamp = (now or report.generated_at).strftime("%Y-%m-%d")
    return f"Northwind_coverage_{report.profile.key}_{stamp}.xlsx"
