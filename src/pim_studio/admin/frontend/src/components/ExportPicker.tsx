import { useMemo, useState } from 'react'
import { ChevronDown, RotateCcw } from 'lucide-react'
import { cn } from '../lib/cn'
import AttributeMarketGrid, { columnsIn } from './AttributeMarketGrid'
import type { AttributeMarkets, CoverageAttribute } from '../lib/coverage'

/** Above this the server refuses, so the button is disabled rather than the
 *  request sent. Kept in step with `MAX_VALUE_CELLS` in `reports/values.py`;
 *  the server stays the authority and this only saves a round trip. */
export const MAX_EXPORT_CELLS = 50_000

/** The cap at size grain, where a sheet is an import file rather than
 *  something to read and the whole catalogue is the normal request. Kept in
 *  step with `MAX_SIZE_CELLS` in `reports/values.py`. */
export const MAX_SIZE_EXPORT_CELLS = 200_000


/**
 * Which attributes' values to carry out of the report.
 *
 * The coverage sheets answer "how many options are incomplete". They cannot
 * answer "let me read the descriptions", because until now nothing in the file
 * carried the text of an attribute. This picks the columns for a fifth sheet
 * that does.
 *
 * Separate from `RequirementPicker` on purpose even though they share a grid.
 * Requiring an attribute and exporting it are different asks, and conflating
 * them would mean you could not read a field without also being told off for
 * its being empty.
 *
 * The counter shows cells rather than attributes because that is the number the
 * limit is about: two attributes across four markets and six thousand options
 * is a large file, and "2 attributes" does not say so.
 */
export default function ExportPicker({
  attributes,
  selection,
  population,
  sizePopulation,
  bySize,
  onBySizeChange,
  onChange,
}: {
  attributes: CoverageAttribute[]
  selection: AttributeMarkets[]
  /** Options the *server* will report on. Not what the screen shows — see the
   *  note in `Coverage.tsx`, which is why this is passed in rather than
   *  derived here. */
  population: number
  /** SKUs across those same options. The cap is about rows, and at size grain
   *  a row is a SKU — roughly six times as many — so predicting against the
   *  option count would let the button look fine and the request come back
   *  413. */
  sizePopulation: number
  /** One row per SKU. Held by the caller, in the URL, alongside the selection
   *  rather than inside it: `base_code_format` is a real attribute — the grid
   *  lists it as "Article (MCS)" — so spelling this as a selection entry made
   *  ticking that row silently change the grain. */
  bySize: boolean
  onBySizeChange: (next: boolean) => void
  onChange: (next: AttributeMarkets[]) => void
}) {
  const [open, setOpen] = useState(false)

  const columns = useMemo(
    () => columnsIn(selection, attributes),
    [selection, attributes],
  )
  const rows = bySize ? sizePopulation : population
  const cells = columns * rows
  const cap = bySize ? MAX_SIZE_EXPORT_CELLS : MAX_EXPORT_CELLS
  const over = cells > cap



  return (
    <div className="rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
        <span className="eyebrow">Also export the values of</span>
        <span className="text-[12px] text-ink-500">
          {columns === 0 && !bySize
            ? 'nothing — the file carries the coverage sheets only'
            : [
                columns > 0
                  ? `${selection.length} attribute${selection.length === 1 ? '' : 's'}`
                  : '',
                bySize ? 'one row per size' : '',
              ]
                .filter(Boolean)
                .join(' · ')}
        </span>

        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={cn(
            'ml-auto flex items-center gap-1.5 rounded-2xs border px-2 py-1 text-[12px]',
            columns > 0
              ? 'border-brand-600 bg-brand-50 text-brand-900'
              : 'hairline text-ink-700 hover:border-ink-300',
          )}
        >
          Choose attributes
          <span
            className={cn(
              'font-mono text-[10.5px] tabular-nums',
              over ? 'text-danger-600' : 'text-ink-400',
            )}
          >
            {columns} col · {rows.toLocaleString('pl-PL')} rows ·{' '}
            {cells.toLocaleString('pl-PL')} values
          </span>
          <ChevronDown
            size={12}
            className={cn('transition-transform', open && 'rotate-180')}
          />
        </button>
      </div>

      {open && (
        <div className="border-t hairline px-3 py-3">
          <div className="mb-2 flex items-center justify-between">
            <p className="max-w-prose text-[12px] text-ink-500">
              Tick what you want to <b className="font-medium">read</b>, not what
              a product must have. These arrive as their own sheet, one row per
              option in scope, with the text exactly as the mirror holds it —
              descriptions come out as HTML. A struck-through market has never
              been read, so its column will be empty.
            </p>
            {(selection.length > 0 || bySize) && (
              <button
                type="button"
                onClick={() => {
                  onChange([])
                  onBySizeChange(false)
                }}
                className="flex flex-none items-center gap-1 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 hover:text-danger-600"
              >
                <RotateCcw size={11} />
                clear
              </button>
            )}
          </div>

          {over && (
            <p className="mb-2 rounded-2xs border border-danger-600/40 bg-danger-100 px-2 py-1.5 text-[12px] text-danger-600">
              {cells.toLocaleString('pl-PL')} values is more than one download
              can carry. Narrow the scope, or untick some markets — the limit is{' '}
              {cap.toLocaleString('pl-PL')}.
            </p>
          )}

          {/* Above the grid, not in it. Every row in the grid is an attribute
              with markets to tick; this is neither, and it changes the shape
              of the sheet rather than adding a column to it. */}
          <label className="mb-3 flex cursor-pointer items-start gap-2 rounded-2xs border hairline bg-paper-sunk px-2.5 py-2">
            <input
              type="checkbox"
              checked={bySize}
              onChange={(e) => onBySizeChange(e.target.checked)}
              className="mt-0.5 h-3 w-3 flex-none accent-brand-700"
            />
            <span className="text-[12px] leading-relaxed text-ink-700">
              <b className="font-medium">One row per size</b> — splits every
              option into its sizes and adds{' '}
              <code className="font-mono text-[11px]">base_code_format</code>{' '}
              (listed below as <b className="font-medium">Article (MCS)</b>),
              the SKU and the size as columns. Price and stock are stored per
              size, so this is the only way to see them as they really are; at
              option level they are rolled up and the sizes that disagree are
              only counted. Names and descriptions repeat down an option's
              sizes.
            </span>
          </label>

          <AttributeMarketGrid
            attributes={attributes}
            selection={selection}
            onChange={onChange}
            unreadHint={(label) =>
              `${label} — never read from any source, so this column will be empty`
            }
          />
        </div>
      )}
    </div>
  )
}
