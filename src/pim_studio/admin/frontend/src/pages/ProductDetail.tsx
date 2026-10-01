import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { cn } from '../lib/cn'
import AttributeMatrix, { type CellRef } from '../components/AttributeMatrix'
import CellInspector from '../components/CellInspector'
import TakeOffline from '../components/TakeOffline'
import { StateLegend } from '../components/StateGlyph'
import FilterChipGroup, { type FilterOption } from '../components/FilterChipGroup'
import { MARKETS, api } from '../lib/api'
import type {
  AttributeCell,
  AttributeRow,
  ProductDetail as ProductDetailType,
  ValueChange,
} from '../lib/api'
import { useAuth } from '../lib/auth'
import { loadProductDetail } from '../lib/demoData'
import { useApi } from '../lib/useApi'

/**
 * Product Detail — implements `docs/design/Product Detail - Attribute
 * Matrix.html` from the design handoff: header with per-market liveness,
 * the attribute matrix with its pinned cell inspector, the blocker rail,
 * images, and the size-level stock/price table.
 *
 * Data comes from the extract, keyed on the route param, so this renders
 * genuine products with their genuine gaps.
 *
 * It used to fall back to the design's hand-built fixture when a ModelColor
 * was not in the extract. That is worse than an error page: the URL says one
 * product and the page shows another's attributes, blockers, images and
 * sizes, with no indication anything is wrong. It fired routinely, because the
 * extract fetches used relative paths that the SPA catch-all answered with
 * index.html — so a detail page loaded directly always got the fixture.
 * A page that cannot show the product asked for should say so.
 */
export default function ProductDetail() {
  const { modelColor } = useParams()
  // The extract and the live overlay, together. The overlay is optional in
  // the sense that the page still renders without it — the design preview has
  // no backend — but when it is there, its cells win.
  const { data, loading, refetch } = useApi(
    () =>
      modelColor
        ? api
            .productValues(modelColor)
            .catch(() => null)
            .then((overlay) => loadProductDetail(modelColor, overlay))
        : Promise.resolve(null),
    [modelColor],
  )

  if (loading) {
    return (
      <div className="mx-auto max-w-table px-10 py-12">
        <div className="h-40 animate-pulse rounded-md border hairline bg-paper-card" />
      </div>
    )
  }

  if (!data) {
    return (
      <div className="mx-auto max-w-table px-10 py-12">
        <div className="eyebrow mb-[7px]">Products</div>
        <h1 className="font-serif text-[28px] font-medium tracking-tight">
          Not in the extract
        </h1>
        <p className="mt-3 max-w-prose text-[13px] leading-relaxed text-ink-500">
          Nothing is held for{' '}
          <span className="font-mono text-ink-900">{modelColor}</span>. Either
          the ModelColor does not exist in our four catalogues, or the extract
          has not been regenerated since it arrived — it rebuilds after each
          sync.
        </p>
      </div>
    )
  }

  return <ProductDetailView product={data} onChanged={refetch} />
}

function ProductDetailView({
  product,
  onChanged,
}: {
  product: ProductDetailType
  onChanged: () => void
}) {
  // Taking a product off sale is developer-only: it is the one control on this
  // page that can stop a product selling, and the dialog it opens is where the
  // blast radius gets explained.
  const { user } = useAuth()
  const [takingOffline, setTakingOffline] = useState(false)

  const [selected, setSelected] = useState<CellRef | null>(null)

  const liveImages = product.images.filter((i) => i.state === 'live').length
  const proposedImages = product.images.filter((i) => i.state === 'proposed').length

  useEffect(() => {
    if (!selected) return
    const drop = () => setSelected(null)
    window.addEventListener('scroll', drop, true)
    window.addEventListener('resize', drop)
    return () => {
      window.removeEventListener('scroll', drop, true)
      window.removeEventListener('resize', drop)
    }
  }, [selected])

  const selectedData = useMemo((): { row: AttributeRow; cell: AttributeCell } | null => {
    if (!selected) return null
    for (const g of product.groups) {
      const row = g.rows.find((r) => r.key === selected.rowKey)
      if (!row) continue
      const cell = selected.market === 'shared' ? row.shared : row.perMarket?.[selected.market]
      if (cell) return { row, cell }
    }
    return null
  }, [product, selected])

  return (
    <div className="mx-auto max-w-table px-10 py-12">
      {/* title row */}
      <div className="mb-[18px] flex items-end justify-between">
        <div>
          <div className="eyebrow mb-[7px]">
            Products · {product.season} · {product.category} / {product.subCategory}
          </div>
          <h1 className="font-serif text-[28px] font-medium tracking-tight">Product detail</h1>
        </div>
        <div className="flex gap-2">
          {user?.role === 'developer' && (
            <button
              onClick={() => setTakingOffline(true)}
              className="rounded-2xs border border-[#e0bdb7] bg-paper-card px-[11px] py-1.5 text-[12px] font-medium text-danger-600 hover:bg-danger-100"
            >
              Take offline
            </button>
          )}
        </div>
      </div>

      {/* header card */}
      <div className="mb-4 flex items-stretch gap-[22px] rounded-md border hairline bg-paper-card p-5 shadow-card">
        {/* The product's own photo. The striped background stays underneath so
            a product with no image reads as a deliberate empty slot rather
            than a broken one. */}
        <div className="ph-img h-[138px] w-[104px] flex-none overflow-hidden rounded-2xs border hairline">
          {product.images[0]?.url && (
            <img
              src={product.images[0].url}
              alt={product.name}
              loading="lazy"
              decoding="async"
              className="h-full w-full object-cover"
            />
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-2.5">
          <div>
            <div className="font-mono text-[19px] font-medium tracking-tight">{product.modelColor}</div>
            <h2 className="m-0 font-serif text-[24px] font-medium leading-tight">{product.name}</h2>
          </div>
          <div className="mt-auto flex flex-wrap gap-5">
            <Meta k="Base code" v={product.baseCode} mono />
            <Meta k="Colour" v={`${product.colorCode} · ${product.colorName}`} mono />
            <Meta k="Season" v={product.season} mono />
            <Meta k="Gender" v={product.gender} mono />
            <Meta k="Sizes" v={String(product.sizeCount)} mono />
            <Meta k="Last change" v={`${product.lastChangedAt} · ${product.lastChangedVia}`} />
          </div>
        </div>
        <div className="grid flex-none grid-cols-4 gap-2.5">
          {product.markets.map((m) => (
            <div
              key={m.market}
              className={cn(
                'flex w-[164px] flex-col gap-[5px] rounded-2xs border hairline p-[9px_10px]',
                m.live ? 'bg-[#fcfdfc]' : 'bg-paper-card',
              )}
            >
              <div className="flex items-center justify-between">
                <span className="font-mono text-[12.5px] font-semibold tracking-[0.06em]">{m.market}</span>
                <span
                  className={cn(
                    'rounded-2xs border px-[5px] py-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em]',
                    m.live
                      ? 'border-[#b9d3c9] bg-ok-100 text-ok-600'
                      : 'border-[#e0bdb7] bg-danger-100 text-danger-600',
                  )}
                >
                  {m.live ? 'live' : 'not live'}
                </span>
              </div>
              <div className="min-h-[31px] text-[11.5px] leading-[1.35] text-ink-500">
                {m.reason || (m.live ? 'Live.' : 'Not live.')}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-start gap-6">
        {/* left column: matrix + sizes */}
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <div
            data-matrix-card
            className="relative rounded-md border hairline bg-paper-card px-4 pb-1.5 pt-3.5 shadow-card"
          >
            <div className="mb-2.5 flex items-center justify-between">
              <div className="eyebrow">
                Attribute matrix ·{' '}
                {product.groups.reduce((a, g) => a + g.rows.length, 0)} attributes × {MARKETS.length}{' '}
                markets
              </div>
              <StateLegend />
            </div>
            <AttributeMatrix groups={product.groups} selected={selected} onSelect={setSelected} />

            {/* Inspector opens beside the cell that was clicked. Anchoring it
                to the card corner meant clicking a row near the bottom put the
                panel somewhere unrelated near the top. */}
            {selectedData && selected && (
              <div className="absolute z-10" style={inspectorStyle(selected.anchor)}>
                <CellInspector
                  row={selectedData.row}
                  market={selected.market}
                  cell={selectedData.cell}
                  onClose={() => setSelected(null)}
                  onSave={async (value) => {
                    await api.setValue({
                      modelColor: product.modelColor,
                      attribute: selectedData.row.key,
                      market: selected.market === 'shared' ? '*' : selected.market,
                      value,
                    })
                    onChanged()
                  }}
                  onAcceptProposal={async (value) => {
                    await api.importsAcceptProposal({
                      modelColor: product.modelColor,
                      attribute: selectedData.row.key,
                      market: selected.market === 'shared' ? '*' : selected.market,
                      ...(value === undefined ? {} : { value }),
                    })
                    setSelected(null)
                    onChanged()
                  }}
                  onRejectProposal={async () => {
                    await api.importsRejectProposal({
                      modelColor: product.modelColor,
                      attribute: selectedData.row.key,
                      market: selected.market === 'shared' ? '*' : selected.market,
                    })
                    setSelected(null)
                    onChanged()
                  }}
                />
              </div>
            )}
          </div>

          {/* sizes */}
          <div
            className={cn(
              'rounded-md border hairline bg-paper-card px-4 pb-3 pt-3.5 shadow-card',
              product.sizes.length === 0 && 'hidden',
            )}
          >
            <div className="eyebrow mb-2.5">Sizes · stock and price are size-level</div>
            <table className="w-full border-separate border-spacing-0">
              <thead>
                <tr>
                  <SzTh left>Size</SzTh>
                  {/* Omnitron's own key. Not the article code above it — that
                      is the MCS, a different identifier — and it is what a
                      push writes against, so it has to be readable somewhere. */}
                  {product.sizes.some((s) => s.sku) && <SzTh left>SKU</SzTh>}
                  {product.sizes.some((s) => s.ean) && <SzTh left>EAN</SzTh>}
                  {MARKETS.map((m) => (
                    <SzTh key={m}>Stock {m}</SzTh>
                  ))}
                  {MARKETS.map((m) => (
                    <SzTh key={m}>Price {m}</SzTh>
                  ))}
                </tr>
              </thead>
              <tbody>
                {product.sizes.map((s) => (
                  <tr key={s.label} className="hover:bg-[#fbfbf8]">
                    <SzTd left>{s.label}</SzTd>
                    {product.sizes.some((x) => x.sku) && (
                      <SzTd left>{s.sku ?? '—'}</SzTd>
                    )}
                    {product.sizes.some((x) => x.ean) && (
                      <SzTd left>{s.ean ?? '—'}</SzTd>
                    )}
                    {MARKETS.map((m) => {
                      const v = s.stock[m]
                      return (
                        <SzTd key={m} tone={v === 0 ? 'zero' : undefined}>
                          {v === undefined ? '—' : v}
                        </SzTd>
                      )
                    })}
                    {MARKETS.map((m) => {
                      const v = s.price[m]
                      return (
                        <SzTd key={m} tone={v === undefined ? 'none' : undefined}>
                          {v ?? '—'}
                        </SzTd>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <SzTd left foot>
                    {product.sizes.length} sizes
                  </SzTd>
                  {product.sizes.some((s) => s.sku) && <SzTd left foot> </SzTd>}
                  {product.sizes.some((s) => s.ean) && <SzTd left foot> </SzTd>}
                  {MARKETS.map((m) => {
                    const t = product.sizes.reduce((a, s) => a + (s.stock[m] ?? 0), 0)
                    return (
                      <SzTd key={m} foot tone={t === 0 ? 'zero' : undefined}>
                        {t}
                      </SzTd>
                    )
                  })}
                  {/* Derived, not asserted. This read `HU has no price — the
                      market cannot go live regardless of stock.` as a literal,
                      so it printed on every product whatever the table above
                      it showed — including products where HU carried a price
                      and the HU tile said LIVE, which is a page contradicting
                      itself in two places a reader can see at once.

                      A market with no price on any size genuinely cannot sell,
                      so the caveat is worth keeping; it just has to be read
                      off the same `sizes` the table renders. Silent when every
                      market is priced, which is the common case. */}
                  {(() => {
                    const unpriced = MARKETS.filter((m) =>
                      product.sizes.every((s) => s.price[m] === undefined),
                    )
                    if (!unpriced.length) return null
                    return (
                      <td
                        colSpan={MARKETS.length}
                        className="border-t border-ink-200 py-1.5 pl-2.5 text-left font-mono text-[11.5px] text-ink-500"
                      >
                        {unpriced.length === 1
                          ? `${unpriced[0]} has no price on any size, so it cannot sell whatever the stock says.`
                          : `${unpriced.join(', ')} have no price on any size, so they cannot sell whatever the stock says.`}
                      </td>
                    )
                  })()}
                </tr>
              </tfoot>
            </table>
          </div>
        </div>

        {/* right rail */}
        <div className="flex w-[372px] flex-none flex-col gap-4">
          <div className="rounded-md border hairline bg-paper-card px-4 pb-4 pt-3.5 shadow-card">
            <div className="eyebrow mb-[9px]">What's blocking this product</div>
            <div className="flex flex-col gap-3">
              {product.blockers.map((g) => (
                <div
                  key={g.label}
                  className={cn(
                    'border-l-2 pl-[11px]',
                    g.owner === 'us'
                      ? 'border-brand-600'
                      : g.owner === 'partner' || g.owner === 'warning'
                        ? 'border-warn-600'
                        : 'border-ink-200',
                  )}
                >
                  <div
                    className={cn(
                      'mb-[5px] font-mono text-[9.5px] uppercase tracking-[0.12em]',
                      g.owner === 'warning' ? 'text-warn-600' : 'text-ink-500',
                    )}
                  >
                    {g.label}
                  </div>
                  <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                    {g.items.map((it, i) => (
                      <li key={i} className="flex gap-[7px] text-[12.5px] leading-snug text-ink-700">
                        <span className="pt-px font-mono text-[11px] text-ink-400">{it.scope}</span>
                        <span>{it.text}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </div>

          {/* images */}
          <div className="rounded-md border hairline bg-paper-card px-4 pb-4 pt-3.5 shadow-card">
            <div className="mb-2.5 flex items-baseline justify-between">
              <div className="eyebrow">
                Images · {liveImages} live, {proposedImages} proposed
              </div>
              <span className="font-mono text-[10px] text-ink-400">brand library</span>
            </div>
            <div className="grid grid-cols-4 gap-2">
              {product.images.map((img) => (
                <div
                  key={img.position}
                  className={cn(
                    'ph-img relative aspect-[3/4] overflow-hidden rounded-2xs border',
                    img.state === 'proposed' ? 'border-dashed border-info-600' : 'hairline',
                  )}
                >
                  {img.url && (
                    <img src={img.url} alt="" className="h-full w-full object-cover" />
                  )}
                  <div
                    className={cn(
                      'absolute inset-x-0 bottom-0 border-t hairline bg-white/90 px-1 py-[3px] font-mono text-[8.5px] uppercase tracking-[0.08em]',
                      img.state === 'proposed' ? 'text-info-600' : 'text-ink-500',
                    )}
                  >
                    {String(img.position).padStart(2, '0')} · {img.state}
                  </div>
                </div>
              ))}
            </div>
            {proposedImages > 0 ? (
              <div className="mt-2.5 text-[11.5px] leading-normal text-ink-500">
                {proposedImages} image{proposedImages === 1 ? '' : 's'} found in the brand library.
              </div>
            ) : product.images.length === 0 ? (
              <div className="mt-2.5 text-[11.5px] leading-normal text-ink-500">
                No images. The product cannot go live without one.
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <ValueHistory modelColor={product.modelColor} />

      {takingOffline && (
        <TakeOffline
          modelColor={product.modelColor}
          onClose={() => setTakingOffline(false)}
          // The header's liveness comes from the extract, which is rebuilt
          // nightly, so it will not move until then. Refetching still picks up
          // anything the overlay holds and keeps the page honest about the
          // values it can see.
          onDone={onChanged}
        />
      )}
    </div>
  )
}

/**
 * What this product's values used to say.
 *
 * Live from the database, unlike everything above it, which comes from the
 * nightly extract. The extract is rebuilt from scratch each night and so has
 * no memory by construction — and this adds no numbers for it to disagree
 * with, only events that already happened and carry their own timestamps.
 *
 * Fetched separately rather than folded into the detail payload: it is the
 * only part of this page that touches the database, most visits never scroll
 * to it, and a slow history query should not hold up the product.
 */
function ValueHistory({ modelColor }: { modelColor: string }) {
  const { data, loading } = useApi(
    () => api.productHistory(modelColor),
    [modelColor],
  )
  const [only, setOnly] = useState<string | null>(null)

  // Built from what this product actually changed, so a chip never matches
  // nothing. Ordered by weight because the heaviest is the one somebody wants
  // to put away: stock is 65% of every change recorded across the catalogue,
  // and arrives four times a day on a moving line — one warehouse movement
  // written once per market — which buries the row somebody came for.
  const counts = useMemo<FilterOption[]>(() => {
    const seen = new Map<string, { label: string; count: number }>()
    for (const c of data?.changes ?? []) {
      const at = seen.get(c.attribute)
      if (at) at.count += 1
      else seen.set(c.attribute, { label: c.label, count: 1 })
    }
    return [...seen.entries()]
      .sort((a, b) => b[1].count - a[1].count)
      .map(([value, v]) => ({ value, label: v.label, count: v.count }))
  }, [data])

  const shown = only
    ? (data?.changes ?? []).filter((c) => c.attribute === only)
    : (data?.changes ?? [])

  if (loading) {
    return (
      <div className="mt-4 h-24 animate-pulse rounded-md border hairline bg-paper-card" />
    )
  }
  if (!data) return null

  return (
    <div className="mt-4 rounded-md border hairline bg-paper-card p-5 shadow-card">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="eyebrow">Value history</h2>
        {data.recordingSince && (
          <span className="text-[11.5px] text-ink-500">
            recording since {formatWhen(data.recordingSince)}
          </span>
        )}
      </div>

      {counts.length > 1 && (
        <FilterChipGroup
          className="mt-3"
          options={counts}
          value={only}
          onChange={setOnly}
        />
      )}

      {data.changes.length === 0 ? (
        // Deliberately not a blank panel. Nothing here almost always means the
        // table is younger than the product, not that the product never
        // changed, and those read identically if the page stays quiet.
        <p className="mt-2 max-w-prose text-[12.5px] leading-relaxed text-ink-500">
          {data.recordingSince
            ? 'Nothing has changed on this product since we started keeping this.'
            : 'No changes recorded yet. History starts from the first value that changes after this was switched on — an older product will look empty here for a while.'}
        </p>
      ) : (
        <>
          <ol className="mt-3 flex flex-col">
            {shown.map((change, i) => (
              <HistoryRow key={i} change={change} />
            ))}
          </ol>
          {shown.length === 0 && (
            <p className="mt-3 text-[12.5px] text-ink-500">
              Nothing under that filter in the changes loaded.
            </p>
          )}
          {data.more && (
            <p className="mt-2 text-[11.5px] text-ink-500">
              Older changes are not shown.
            </p>
          )}
        </>
      )}
    </div>
  )
}

function HistoryRow({ change }: { change: ValueChange }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-1 border-b hairline py-2 last:border-b-0">
      <span className="w-[132px] flex-none font-mono text-[11px] tabular-nums text-ink-500">
        {formatWhen(change.changedAt)}
      </span>
      <span className="text-[12.5px] font-medium text-ink-900">{change.label}</span>
      {change.market !== '*' && (
        <span className="rounded-2xs border hairline px-1 font-mono text-[10px] tracking-[0.06em] text-ink-500">
          {change.market}
        </span>
      )}
      {/* Only when it was a size. At option grain the ModelColor is the page
          you are already on, and repeating it would be noise on every row. */}
      {change.sku && (
        <span className="font-mono text-[10.5px] text-ink-400">{change.sku}</span>
      )}
      <span className="flex flex-wrap items-baseline gap-1.5 text-[12.5px]">
        <Value text={change.oldValue} was />
        <span className="text-ink-400">→</span>
        <Value text={change.newValue} />
      </span>
      {change.truncated && (
        <span className="text-[10.5px] text-ink-400">(cut)</span>
      )}
      <span className="ml-auto font-mono text-[10.5px] text-ink-400">
        {change.changedBy ?? (change.syncRunId ? `sync #${change.syncRunId}` : 'sync')}
      </span>
    </li>
  )
}

/** An empty value is a fact, not a missing one — a row can exist holding
 *  nothing, and "was blank, now says X" is the change. So it is drawn rather
 *  than left as a gap the reader has to interpret. */
function Value({ text, was }: { text: string | null; was?: boolean }) {
  if (text === null || text === '') {
    return <span className="italic text-ink-400">empty</span>
  }
  return (
    <span
      className={cn(
        'rounded-2xs px-1',
        was ? 'bg-paper-sunk text-ink-500 line-through' : 'bg-brand-50 text-brand-900',
      )}
    >
      {text}
    </span>
  )
}

function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const when = new Date(iso)
  return Number.isNaN(when.getTime())
    ? '—'
    : when.toLocaleString('pl-PL', {
        year: 'numeric',
        month: 'short',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
}

/**
 * Place the inspector next to its cell: to the right if it fits, otherwise to
 * the left, otherwise pinned inside the card's right edge. Vertically it sits
 * a little above the cell so the cell stays visible beside it.
 */
const PANEL_W = 440
const GAP = 10

function inspectorStyle(
  anchor: CellRef['anchor'],
): React.CSSProperties {
  if (!anchor) return { right: 16, top: 64 }
  const { top, left, width, height, cardWidth } = anchor

  let x = left + width + GAP
  if (x + PANEL_W > cardWidth - 8) x = left - PANEL_W - GAP
  if (x < 8) x = Math.max(8, cardWidth - PANEL_W - 8)

  return { left: x, top: Math.max(8, top + height / 2 - 60) }
}

function Meta({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="whitespace-nowrap">
      <div className="eyebrow mb-[3px]">{k}</div>
      <div className={cn('text-[12.5px] text-ink-700', mono && 'font-mono')}>{v}</div>
    </div>
  )
}

function SzTh({ children, left }: { children: React.ReactNode; left?: boolean }) {
  return (
    <th
      className={cn(
        'border-b border-ink-200 px-2.5 pb-[7px] font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-ink-500',
        left ? 'text-left' : 'text-right',
      )}
    >
      {children}
    </th>
  )
}

function SzTd({
  children,
  left,
  foot,
  tone,
}: {
  children: React.ReactNode
  left?: boolean
  foot?: boolean
  tone?: 'zero' | 'none'
}) {
  return (
    <td
      className={cn(
        'px-2.5 py-1.5 font-mono text-[12px] tabular-nums',
        left ? 'text-left' : 'text-right',
        foot ? 'border-t border-ink-200 text-[11.5px] text-ink-500' : 'border-b border-ink-100',
        tone === 'zero' && 'text-danger-600',
        tone === 'none' && 'text-ink-300',
        !tone && !foot && 'text-ink-900',
      )}
    >
      {children}
    </td>
  )
}
