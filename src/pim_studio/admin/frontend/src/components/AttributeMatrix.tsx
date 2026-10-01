import { cn } from '../lib/cn'
import StateGlyph from './StateGlyph'
import { MARKETS, MARKET_NAMES } from '../lib/api'
import type { AttributeCell, AttributeGroup, AttributeRow, Market } from '../lib/api'
// Who fills an attribute, in one vocabulary. Coverage and the requirement
// picker already read this; the matrix rendered the raw registry values
// instead, which put **US** in the column immediately left of PL, CZ, SK and
// HU — read as the United States by anyone not already in on it, in the one
// column whose whole job is to say who acts.
import { OWNER_LABEL } from '../lib/coverage'

export interface CellRef {
  rowKey: string
  market: Market | 'shared'
  /** Where the clicked cell sits inside the matrix card, so the inspector can
   *  open beside it. Measured at click time rather than tracked, because the
   *  panel is dismissed on any scroll or resize that would invalidate it. */
  anchor?: { top: number; left: number; width: number; height: number; cardWidth: number }
}

/**
 * Attributes down, markets across. Around 38 rows × 4 markets, so roughly 160
 * cells — the user scans it for problems rather than reading it.
 *
 * Deliberately not an editable grid. Selecting a cell opens an inspector
 * beside it; 160 inline inputs is how someone overwrites a live value by
 * accident.
 */
export default function AttributeMatrix({
  groups,
  selected,
  onSelect,
}: {
  groups: AttributeGroup[]
  selected: CellRef | null
  onSelect: (ref: CellRef | null) => void
}) {
  return (
    <table className="w-full table-fixed border-separate border-spacing-0">
      <colgroup>
        <col className="w-[196px]" />
        <col className="w-[66px]" />
        {MARKETS.map((m) => (
          <col key={m} />
        ))}
      </colgroup>
      <thead>
        <tr>
          <th className="border-b border-ink-200 pb-[7px] text-left align-bottom">
            <span className="eyebrow">Attribute</span>
          </th>
          <th className="border-b border-ink-200 pb-[7px] text-left align-bottom">
            <span className="eyebrow">Owner</span>
          </th>
          {MARKETS.map((m) => (
            <th
              key={m}
              className="border-b border-ink-200 pb-[7px] text-left align-bottom"
            >
              <span className="pl-[9px] font-mono text-[11px] font-semibold tracking-[0.08em]">
                {m}
              </span>
              <span className="block pl-[9px] font-mono text-[9.5px] tracking-[0.06em] text-ink-400">
                {MARKET_NAMES[m]}
              </span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {groups.map((group) => (
          <GroupBlock
            key={group.title}
            group={group}
            selected={selected}
            onSelect={onSelect}
          />
        ))}
      </tbody>
    </table>
  )
}

function GroupBlock({
  group,
  selected,
  onSelect,
}: {
  group: AttributeGroup
  selected: CellRef | null
  onSelect: (ref: CellRef | null) => void
}) {
  return (
    <>
      <tr>
        <td colSpan={2 + MARKETS.length} className="border-b hairline pb-[5px] pt-4">
          <span className="eyebrow !text-ink-400">{group.title}</span>
        </td>
      </tr>
      {group.rows.map((row) => (
        <Row key={row.key} row={row} selected={selected} onSelect={onSelect} />
      ))}
    </>
  )
}

function Row({
  row,
  selected,
  onSelect,
}: {
  row: AttributeRow
  selected: CellRef | null
  onSelect: (ref: CellRef | null) => void
}) {
  const partner = row.owner === 'partner'

  return (
    <tr className="group">
      <td
        title={row.label}
        className="h-[33px] truncate border-b border-ink-100 pr-2.5 text-[12.5px] text-ink-700 group-hover:bg-[#fbfbf8]"
      >
        {row.label}
      </td>
      <td
        className={cn(
          'h-[33px] border-b border-ink-100 font-mono text-[9.5px] uppercase tracking-[0.08em] group-hover:bg-[#fbfbf8]',
          partner ? 'text-warn-600' : 'text-ink-400',
        )}
      >
        {OWNER_LABEL[row.owner] ?? row.owner}
      </td>

      {row.shared ? (
        <Cell
          cell={row.shared}
          rowKey={row.key}
          market="shared"
          partner={partner}
          colSpan={MARKETS.length}
          selected={selected}
          onSelect={onSelect}
        />
      ) : (
        MARKETS.map((m) => (
          <Cell
            key={m}
            cell={row.perMarket?.[m]}
            rowKey={row.key}
            market={m}
            partner={partner}
            selected={selected}
            onSelect={onSelect}
          />
        ))
      )}
    </tr>
  )
}

function Cell({
  cell,
  rowKey,
  market,
  partner,
  colSpan,
  selected,
  onSelect,
}: {
  cell: AttributeCell | undefined
  rowKey: string
  market: Market | 'shared'
  partner: boolean
  colSpan?: number
  selected: CellRef | null
  onSelect: (ref: CellRef | null) => void
}) {
  const isSelected = selected?.rowKey === rowKey && selected.market === market

  if (!cell) {
    return <td className="h-[33px] border-b border-l border-ink-100" colSpan={colSpan} />
  }

  const na = cell.state === 'na'
  const shared = market === 'shared'

  return (
    <td
      colSpan={colSpan}
      onClick={(e) => {
        if (isSelected) return onSelect(null)
        // Position relative to the card rather than the viewport: the card is
        // the positioned ancestor the inspector renders into, and the page
        // scrolls underneath both.
        const card = (e.currentTarget as HTMLElement).closest<HTMLElement>('[data-matrix-card]')
        const c = (e.currentTarget as HTMLElement).getBoundingClientRect()
        const k = card?.getBoundingClientRect()
        onSelect({
          rowKey,
          market,
          anchor: k
            ? {
                top: c.top - k.top,
                left: c.left - k.left,
                width: c.width,
                height: c.height,
                cardWidth: k.width,
              }
            : undefined,
        })
      }}
      className={cn(
        'relative h-[33px] cursor-default border-b border-l border-ink-100 px-[9px]',
        'group-hover:bg-[#fbfbf8]',
        shared && 'cell-shared',
        na && 'bg-paper-sunk',
        partner && 'cell-partner',
        cell.suspect && 'cell-suspect',
        isSelected && 'z-[2] bg-brand-50 shadow-[inset_0_0_0_1px_#1e6a52]',
      )}
    >
      <div className="flex min-w-0 items-center gap-[7px]">
        <StateGlyph state={cell.state} />
        <CellValue cell={cell} />
        {cell.suspect && (
          <span className="flex-none rounded-[2px] border border-[#ddc48a] bg-warn-100 px-[3px] font-mono text-[9.5px] font-semibold leading-[13px] text-warn-600">
            {cell.suspect}
          </span>
        )}
      </div>
      {shared && (
        <span className="absolute right-[9px] top-1/2 -translate-y-1/2 font-mono text-[9px] uppercase tracking-[0.12em] text-ink-300">
          all markets
        </span>
      )}
    </td>
  )
}

/** Typography carries a second signal alongside the glyph: mono-dim for
 *  absence, italic for unpublished, struck for declined. */
function CellValue({ cell }: { cell: AttributeCell }) {
  if (cell.state === 'na') {
    return (
      <span className="cell-value font-mono text-[11px] tracking-[0.06em] text-ink-300">
        n/a
      </span>
    )
  }
  if (cell.state === 'missing') {
    return (
      <span className="cell-value font-mono text-[11px] tracking-[0.06em] text-ink-300">
        missing
      </span>
    )
  }
  if (cell.state === 'rejected') {
    return (
      <span
        title={cell.value ?? undefined}
        className="cell-value truncate text-[12.5px] text-ink-400 line-through"
      >
        {cell.value}
      </span>
    )
  }
  if (cell.state === 'proposed') {
    return (
      <span className="cell-value truncate border-b border-dotted border-info-600 text-[12.5px] italic text-ink-700">
        {cell.value}
      </span>
    )
  }
  if (cell.perSize) {
    return (
      <span
        title="Each size holds its own value — see the sizes table below."
        className="cell-value truncate font-mono text-[11px] tracking-[0.06em] text-ink-400"
      >
        {cell.sizeCount ? `per size · ${cell.sizeCount}` : 'per size'}
      </span>
    )
  }
  return (
    <span
      title={cell.value ?? undefined}
      className={cn(
        'cell-value truncate text-ink-900',
        cell.code ? 'font-mono text-[12px]' : 'text-[12.5px]',
      )}
    >
      {cell.value}
    </span>
  )
}
