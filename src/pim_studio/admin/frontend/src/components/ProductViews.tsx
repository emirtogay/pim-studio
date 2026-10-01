import { Link } from 'react-router-dom'
import { cn } from '../lib/cn'
import { MARKETS } from '../lib/api'
import type { Market, ProductSummary } from '../lib/api'

/**
 * Two views over one scope: an image-led grid for visual work (checking
 * imagery, naming) and a dense table for data work. Both share the market
 * strip and the selection model, so switching view never changes what you're
 * looking at — only how.
 */

/** Per-market liveness on a card, at ~11px.
 *
 * Colour alone can't carry four markets at this size, so the mark is filled
 * for live and hollow for not, and the market code is always shown. It stays
 * legible in greyscale and to anyone who can't separate the two hues. */
export function MarketStrip({
  product,
  size = 'sm',
}: {
  product: ProductSummary
  size?: 'sm' | 'xs'
}) {
  return (
    <div className="flex items-center gap-1">
      {MARKETS.map((m) => {
        const live = product.live[m] ?? false
        const why = product.blockedBy[m]
        return (
          <span
            key={m}
            title={live ? `${m}: live` : `${m}: ${why ?? 'not live'}`}
            className={cn(
              'inline-flex items-center gap-[3px] rounded-[2px] border px-[3px] font-mono tabular-nums',
              size === 'sm' ? 'text-[9.5px] leading-[15px]' : 'text-[9px] leading-[13px]',
              live
                ? 'border-[#b9d3c9] bg-ok-100 text-ok-600'
                : 'border-ink-200 bg-transparent text-ink-400',
            )}
          >
            <span
              className={cn(
                'h-[5px] w-[5px] rounded-full',
                live ? 'bg-ok-600' : 'border border-ink-300 bg-transparent',
              )}
            />
            {m}
          </span>
        )
      })}
    </div>
  )
}

/** Compact statement of what's wrong. Missing and defects are visually
 *  distinct because they're different work for different people. */
function Problems({ product, max = 2 }: { product: ProductSummary; max?: number }) {
  const items = [
    ...product.defects.map((d) => ({ text: d, kind: 'defect' as const })),
    ...product.missing.map((m) => ({ text: m, kind: 'missing' as const })),
  ]
  if (!items.length) {
    return <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-300">complete</span>
  }
  const shown = items.slice(0, max)
  const rest = items.length - shown.length
  return (
    <div className="flex flex-wrap items-center gap-1">
      {shown.map((it) => (
        <span
          key={it.text}
          className={cn(
            'truncate rounded-[2px] border px-[3px] font-mono text-[9.5px] leading-[15px]',
            it.kind === 'defect'
              ? 'border-[#ddc48a] bg-warn-100 text-warn-600'
              : 'border-ink-200 bg-paper-sunk text-ink-500',
          )}
          title={`${it.kind}: ${it.text}`}
        >
          {it.text}
        </span>
      ))}
      {rest > 0 && (
        <span className="font-mono text-[9.5px] text-ink-400">+{rest}</span>
      )}
    </div>
  )
}

export function ProductGrid({
  products,
  selected,
  onToggleSelect,
}: {
  products: ProductSummary[]
  selected: Set<string>
  onToggleSelect: (mc: string) => void
}) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(212px,1fr))] gap-3">
      {products.map((p) => (
        <div
          key={p.modelColor}
          className={cn(
            'group relative flex flex-col overflow-hidden rounded-md border bg-paper-card shadow-card',
            selected.has(p.modelColor) ? 'border-brand-600' : 'hairline',
          )}
        >
          <label className="absolute left-2 top-2 z-10 cursor-pointer">
            <input
              type="checkbox"
              checked={selected.has(p.modelColor)}
              onChange={() => onToggleSelect(p.modelColor)}
              className="h-3.5 w-3.5 accent-brand-700"
            />
          </label>
          <Link to={`/products/${encodeURIComponent(p.modelColor)}`} className="block">
            <div className="ph-img relative aspect-[3/4] w-full">
              {(p.imageCount ?? 0) > 1 && (
                <span className="absolute right-1.5 top-1.5 z-10 rounded-[2px] bg-ink-900/70 px-1 font-mono text-[9px] leading-[14px] text-white">
                  {p.imageCount}
                </span>
              )}
              {p.imageUrl && (
                <img
                  src={p.imageUrl}
                  alt=""
                  /* The Akinon CDN serves full resolution only and ignores
                     every resize parameter, so images run 238-703 KB each.
                     Lazy + async keeps a scroll from pulling tens of MB. */
                  loading="lazy"
                  decoding="async"
                  className="h-full w-full object-cover"
                />
              )}
            </div>
          </Link>
          <div className="flex flex-1 flex-col gap-1.5 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <Link
                to={`/products/${encodeURIComponent(p.modelColor)}`}
                className="truncate font-mono text-[11.5px] font-medium text-ink-900 hover:text-brand-700"
              >
                {p.modelColor}
              </Link>
              <span className="flex-none font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-400">
                {p.season}
              </span>
            </div>
            <div className="clamp-2 text-[12px] leading-snug text-ink-700">{p.name}</div>
            <div className="mt-auto flex flex-col gap-1.5 pt-1">
              <MarketStrip product={p} />
              <Problems product={p} />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

/** Columns the user can add to the table. This is how "view by attribute"
 *  works in practice — pick the attribute, sort by it, work the gaps. */
export const TABLE_COLUMNS = [
  { key: 'season', label: 'Season' },
  { key: 'mainCategory', label: 'Category' },
  { key: 'subCategory', label: 'Sub-category' },
  { key: 'gender', label: 'Gender' },
  { key: 'skuCount', label: 'Sizes' },
] as const

export type TableColumnKey = (typeof TABLE_COLUMNS)[number]['key']

export function ProductTable({
  products,
  columns,
  selected,
  onToggleSelect,
  onToggleAll,
}: {
  products: ProductSummary[]
  columns: TableColumnKey[]
  selected: Set<string>
  onToggleSelect: (mc: string) => void
  onToggleAll: () => void
}) {
  const allSelected = products.length > 0 && products.every((p) => selected.has(p.modelColor))
  return (
    <div className="overflow-x-auto rounded-md border hairline bg-paper-card shadow-card">
      <table className="w-full border-separate border-spacing-0">
        <thead className="sticky top-0 z-10 bg-paper-card">
          <tr>
            <Th narrow>
              <input
                type="checkbox"
                checked={allSelected}
                onChange={onToggleAll}
                className="h-3.5 w-3.5 accent-brand-700"
                aria-label="Select all"
              />
            </Th>
            <Th>ModelColor</Th>
            <Th>Product</Th>
            {TABLE_COLUMNS.filter((c) => columns.includes(c.key)).map((c) => (
              <Th key={c.key}>{c.label}</Th>
            ))}
            <Th>Markets</Th>
            <Th>Missing / defects</Th>
          </tr>
        </thead>
        <tbody>
          {products.map((p) => (
            <tr key={p.modelColor} className="hover:bg-[#fbfbf8]">
              <Td narrow>
                <input
                  type="checkbox"
                  checked={selected.has(p.modelColor)}
                  onChange={() => onToggleSelect(p.modelColor)}
                  className="h-3.5 w-3.5 accent-brand-700"
                  aria-label={`Select ${p.modelColor}`}
                />
              </Td>
              <Td>
                <Link
                  to={`/products/${encodeURIComponent(p.modelColor)}`}
                  className="font-mono text-[12px] text-ink-900 hover:text-brand-700"
                >
                  {p.modelColor}
                </Link>
              </Td>
              <Td>
                <span className="block max-w-[280px] truncate text-[12.5px] text-ink-700">
                  {p.name}
                </span>
              </Td>
              {TABLE_COLUMNS.filter((c) => columns.includes(c.key)).map((c) => (
                <Td key={c.key}>
                  <span className="font-mono text-[11.5px] text-ink-700">
                    {String(p[c.key as keyof ProductSummary] ?? '')}
                  </span>
                </Td>
              ))}
              <Td>
                <MarketStrip product={p} size="xs" />
              </Td>
              <Td>
                <Problems product={p} max={3} />
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Th({ children, narrow }: { children: React.ReactNode; narrow?: boolean }) {
  return (
    <th
      className={cn(
        'border-b border-ink-200 px-2.5 pb-[7px] pt-2.5 text-left font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-ink-500',
        narrow && 'w-8',
      )}
    >
      {children}
    </th>
  )
}

function Td({ children, narrow }: { children: React.ReactNode; narrow?: boolean }) {
  return (
    <td className={cn('border-b border-ink-100 px-2.5 py-1.5 align-middle', narrow && 'w-8')}>
      {children}
    </td>
  )
}

/** Skeletons that match the final layout rather than a spinner — the page
 *  shouldn't jump when data arrives. */
export function GridSkeleton() {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(212px,1fr))] gap-3">
      {Array.from({ length: 12 }, (_, i) => (
        <div key={i} className="overflow-hidden rounded-md border hairline bg-paper-card">
          <div className="skeleton aspect-[3/4] w-full" />
          <div className="flex flex-col gap-2 p-2.5">
            <div className="skeleton h-3 w-2/3" />
            <div className="skeleton h-3 w-full" />
            <div className="skeleton h-4 w-3/4" />
          </div>
        </div>
      ))}
    </div>
  )
}

export type { Market }
