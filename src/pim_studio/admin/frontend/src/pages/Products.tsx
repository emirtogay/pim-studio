import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ArrowUpDown, LayoutGrid, Rows3, Search, X } from 'lucide-react'
import { cn } from '../lib/cn'
import BulkSetValue from '../components/BulkSetValue'
import FacetRail from '../components/FacetRail'
import {
  GridSkeleton,
  ProductGrid,
  ProductTable,
  TABLE_COLUMNS,
  type TableColumnKey,
} from '../components/ProductViews'
import { api, MARKETS } from '../lib/api'
import type { FacetKey } from '../lib/api'
import {
  applyScope,
  applySearch,
  applySort,
  bannedButLive,
  bestFacetToRelax,
  buildFacets,
  changedAt,
  FACET_LABELS,
  marketTally,
  naturalSort,
  seasonRank,
  unexplainedTally,
  queryFromScope,
  scopeFromQuery,
  SORT_DIRECTIONS,
  SORT_KEYS,
  SORT_LABELS,
  sortFromQuery,
  sortToQuery,
  toggleFacet,
} from '../lib/scope'
import type { Scope, SortKey, SortOrder } from '../lib/scope'
import { loadMeta, loadProducts } from '../lib/demoData'
import { useApi } from '../lib/useApi'

/**
 * Products — the everyday page.
 *
 * A season or category isn't a filter here, it's a working scope: you return
 * to "the 26AW polos" across days, so the scope lives in the URL — along with
 * the search text and the sort order — survives navigating into a product and
 * back, and is shareable as a link.
 *
 * Two views over that one scope — a grid for visual work, a table with
 * user-chosen attribute columns for data work. Switching preserves both the
 * scope and the selection.
 *
 * Results are paged rather than rendered whole. At real scale an unpaged
 * grid put ~12,500 anchors and 6,242 full-resolution CDN images into the DOM
 * and the page stopped responding — the scope narrows the *data*, it doesn't
 * bound the *render*, and those are different problems.
 *
 * Data comes from the real-data demo extract in `public/demo/` — thousands of
 * real products with real per-market liveness and real gaps — so the page can
 * be judged at scale before `/api/products` exists. Swapping in the endpoint
 * changes only the loader; scope, facet counting and selection are already
 * real.
 */
export default function Products() {
  const [params, setParams] = useSearchParams()
  const [view, setView] = useState<'grid' | 'table'>('grid')
  const [columns, setColumns] = useState<TableColumnKey[]>(['season', 'subCategory'])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkNote, setBulkNote] = useState('')
  const [exporting, setExporting] = useState(false)

  // The Values sheet for the selection, with every writable attribute in
  // every market — the file the Imports page reads back after it is edited.
  const exportSelection = async () => {
    setExporting(true)
    setBulkNote('')
    try {
      const { attributes } = await api.importsTargets()
      const { blob, filename } = await api.coverageReport({
        label: 'Selected products',
        modelColors: [...selected],
        values: attributes.map((a) => ({ attribute: a.key, markets: [] })),
        bySize: false,
      })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setBulkNote(err instanceof Error ? err.message : 'Could not build the sheet.')
    } finally {
      setExporting(false)
    }
  }
  const [limit, setLimit] = useState(PAGE)

  const scope = useMemo(() => scopeFromQuery(params), [params])
  const query = params.get('q') ?? ''
  const sort = useMemo(() => sortFromQuery(params), [params])
  // Local draft so typing stays responsive, pushed to the URL on a short
  // debounce. Writing every keystroke would put one history entry per
  // character and make the back button useless.
  const [draft, setDraft] = useState(query)
  const { data, loading } = useApi(loadProducts, [])
  const { data: meta } = useApi(loadMeta, [])
  const all = useMemo(() => data ?? [], [data])

  // Search narrows the set the facets are counted against, so the counts
  // describe what you are actually looking at. A facet reading "Polo 5" while
  // the search has left two visible would be worse than no count at all.
  const searched = useMemo(() => applySearch(all, query), [all, query])
  // Sort last, over the narrowed set: ordering eighteen thousand options to
  // then show sixty of them is work thrown away, and nothing upstream of here
  // — the counts, the facets, the tallies — reads the order at all.
  const products = useMemo(
    () => applySort(applyScope(searched, scope), sort),
    [searched, scope, sort],
  )
  const facets = useMemo(() => buildFacets(searched, scope), [searched, scope])
  const tally = useMemo(() => marketTally(products), [products])
  const unexplained = useMemo(() => unexplainedTally(products), [products])
  const banned = useMemo(() => bannedButLive(products), [products])

  const activeCount = Object.values(scope).reduce((a, v) => a + (v?.length ?? 0), 0)

  // How much of the scoped set the current sort cannot place. Zero for code
  // and name, which every product has; often most of the catalogue for season,
  // where eleven thousand options are OUTLET rather than a collection.
  const unranked = useMemo(() => {
    if (sort.key === 'season') {
      return products.filter((p) => seasonRank(p.season) === null).length
    }
    if (sort.key === 'changed') {
      return products.filter((p) => changedAt(p.lastChangedAt) === null).length
    }
    return 0
  }, [products, sort.key])

  function update(next: Scope) {
    setParams(urlFor(next, query, sort), { replace: false })
  }

  // Picking a field sorts it the way that field is normally wanted; picking
  // the one already active reverses it. Pushed rather than replaced, like a
  // facet click and unlike a keystroke — it is one deliberate act, and the
  // back button should undo it.
  function chooseSort(key: SortKey) {
    const next: SortOrder =
      key === sort.key ? { key, desc: !sort.desc } : naturalSort(key)
    setParams(urlFor(scope, query, next), { replace: false })
  }

  // Debounced: replace rather than push, so a search is one history entry
  // however many characters it took to type.
  useEffect(() => {
    if (draft === query) return
    const id = setTimeout(() => {
      setParams(urlFor(scope, draft, sort), { replace: true })
    }, 250)
    return () => clearTimeout(id)
  }, [draft])

  // The URL can change without the input — back button, a saved scope, a
  // cleared search — and the box has to follow it.
  useEffect(() => setDraft(query), [query])

  function onToggle(key: FacetKey, value: string) {
    update(toggleFacet(scope, key, value))
  }

  function toggleSelect(mc: string) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(mc)) next.delete(mc)
      else next.add(mc)
      return next
    })
  }

  function toggleAll() {
    setSelected((s) =>
      products.every((p) => s.has(p.modelColor))
        ? new Set()
        : new Set(products.map((p) => p.modelColor)),
    )
  }

  // Suggest relaxing against the *searched* set: with a search active, the
  // count from relaxing a facet has to be the count you would actually see.
  const relax = products.length === 0 ? bestFacetToRelax(searched, scope) : null
  // A search that matches nothing is not a scope that is too narrow, and
  // offering to drop a facet would be advice that cannot help.
  const searchIsTheProblem = products.length === 0 && query !== '' && searched.length === 0
  const shown = useMemo(() => products.slice(0, limit), [products, limit])

  // Any change of scope or view starts the page count over — otherwise a
  // narrow scope inherits a huge limit from a previous wide one.
  useEffect(() => setLimit(PAGE), [params, view])

  return (
    <div className="mx-auto max-w-table px-10 py-12">
      {/* title */}
      <div className="mb-4 flex items-end justify-between">
        <div>
          <div className="eyebrow mb-[7px]">Products</div>
          <h1 className="font-serif text-[28px] font-medium tracking-tight">
            {activeCount === 0 ? 'All products' : 'Scoped view'}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded-2xs border hairline">
            <ViewButton active={view === 'grid'} onClick={() => setView('grid')}>
              <LayoutGrid size={13} /> Grid
            </ViewButton>
            <ViewButton active={view === 'table'} onClick={() => setView('table')}>
              <Rows3 size={13} /> Table
            </ViewButton>
          </div>
        </div>
      </div>

      {/* search — AND-ed with the scope, not one of its facets */}
      <div className="mb-3">
        <div className="relative">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
          />
          <input
            type="search"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setDraft('')
            }}
            placeholder="Search by code or name — L3558.132, AH4463, polo"
            aria-label="Search products"
            className="w-full rounded-md border hairline bg-paper-card py-2 pl-9 pr-9 text-[13px] text-ink-900 shadow-card placeholder:text-ink-400 focus:border-brand-600 focus:outline-none"
          />
          {draft && (
            <button
              onClick={() => setDraft('')}
              aria-label="Clear search"
              className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-ink-400 hover:text-danger-600"
            >
              <X size={14} />
            </button>
          )}
        </div>
      </div>

      {/* sort — reorders the scoped set, never narrows it. The active field
          carries its own direction in words and reverses on a second click,
          so the order that is running is on the screen rather than in an
          arrow somebody has to interpret. */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="eyebrow">Sort</span>
        {SORT_KEYS.map((key) => {
          const active = key === sort.key
          const direction = SORT_DIRECTIONS[key][sort.desc ? 'desc' : 'asc']
          return (
            <button
              key={key}
              onClick={() => chooseSort(key)}
              aria-pressed={active}
              title={
                active
                  ? `Sorted by ${SORT_LABELS[key].toLowerCase()}, ${direction}. Click to reverse.`
                  : `Sort by ${SORT_LABELS[key].toLowerCase()}, ${SORT_DIRECTIONS[key][naturalSort(key).desc ? 'desc' : 'asc']}`
              }
              className={cn(
                'flex items-center gap-1.5 rounded-2xs border px-2 py-1 text-[12px]',
                active
                  ? 'border-brand-600 bg-brand-50 text-brand-900'
                  : 'hairline bg-paper-card text-ink-700 hover:border-ink-300',
              )}
            >
              {active && <ArrowUpDown size={11} className="text-brand-700" />}
              {SORT_LABELS[key]}
              {active && (
                <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-brand-600">
                  {direction}
                </span>
              )}
            </button>
          )
        })}
        {/* Season and Changed leave products behind — an OUTLET row has no
            collection, and a row Omnitron has never stamped has no date. Both
            sort to the end, and saying how many keeps that from reading as a
            bug when somebody scrolls to the bottom. */}
        {unranked > 0 && (
          <span className="ml-1 font-mono text-[10.5px] tabular-nums text-ink-400">
            {unranked.toLocaleString()} with no {sort.key === 'season' ? 'season' : 'date'}, last
          </span>
        )}
      </div>

      {/* active scope, readable and individually removable */}
      <div className="mb-4 flex flex-wrap items-center gap-2 rounded-md border hairline bg-paper-card px-3 py-2.5 shadow-card">
        <span className="font-mono text-[11px] tabular-nums text-ink-900">
          {products.length.toLocaleString()}
        </span>
        <span className="text-[12.5px] text-ink-500">
          {products.length === 1 ? 'option' : 'options'}
        </span>
        {(activeCount > 0 || query) && <span className="text-ink-300">·</span>}
        {query && (
          <button
            onClick={() => setDraft('')}
            className="group flex items-center gap-1.5 rounded-2xs border hairline bg-paper-sunk py-[3px] pl-2 pr-1.5 text-[12px] text-ink-700 hover:border-ink-300"
            title={`Clear search: ${query}`}
          >
            <span className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-400">
              Search
            </span>
            {query}
            <X size={11} className="text-ink-400 group-hover:text-danger-600" />
          </button>
        )}
        {(Object.keys(scope) as FacetKey[]).flatMap((key) =>
          (scope[key] ?? []).map((value) => (
            <button
              key={`${key}:${value}`}
              onClick={() => onToggle(key, value)}
              className="group flex items-center gap-1.5 rounded-2xs border hairline bg-paper-sunk py-[3px] pl-2 pr-1.5 text-[12px] text-ink-700 hover:border-ink-300"
              title={`Remove ${FACET_LABELS[key]}: ${value}`}
            >
              <span className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-400">
                {FACET_LABELS[key]}
              </span>
              {value}
              <X size={11} className="text-ink-400 group-hover:text-danger-600" />
            </button>
          )),
        )}
        {(activeCount > 0 || query) && (
          <button
            onClick={() => {
              setDraft('')
              // The chips in this bar, and nothing else. The sort is not one
              // of them — it narrows nothing, it survives.
              setParams(urlFor({}, '', sort))
            }}
            className="ml-auto font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 hover:text-danger-600"
          >
            clear all
          </button>
        )}
      </div>

      <div className="flex items-start gap-6">
        <FacetRail facets={facets} scope={scope} onToggle={onToggle} />

        <div className="min-w-0 flex-1">
          {/* market comparison strip — reports, never filters */}
          <div className="mb-3 flex items-center gap-4 rounded-md border hairline bg-paper-card px-3 py-2 shadow-card">
            <span className="eyebrow">Live in scope</span>
            {MARKETS.map((m) => {
              const n = tally[m]
              const pct = products.length ? Math.round((n / products.length) * 100) : 0
              return (
                <div key={m} className="flex items-baseline gap-1.5">
                  <span className="font-mono text-[11px] font-semibold tracking-[0.06em] text-ink-700">
                    {m}
                  </span>
                  <span className="font-mono text-[12.5px] tabular-nums text-ink-900">{n}</span>
                  <span className="font-mono text-[10px] tabular-nums text-ink-400">{pct}%</span>
                </div>
              )
            })}
            {meta?.observedAt && (
              <span
                className="font-mono text-[10px] tabular-nums text-ink-400"
                title={`The shops were last read at ${meta.observedAt}. Markets measured rather than computed: ${(meta.observedMarkets ?? []).join(', ') || 'none'}`}
              >
                as of {asOf(meta.observedAt)}
              </span>
            )}
            {view === 'table' && (
              <div className="ml-auto flex items-center gap-2">
                <span className="eyebrow">Columns</span>
                {TABLE_COLUMNS.map((c) => (
                  <button
                    key={c.key}
                    onClick={() =>
                      setColumns((cols) =>
                        cols.includes(c.key)
                          ? cols.filter((k) => k !== c.key)
                          : [...cols, c.key],
                      )
                    }
                    className={cn(
                      'rounded-2xs border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.08em]',
                      columns.includes(c.key)
                        ? 'border-brand-600 bg-brand-50 text-brand-900'
                        : 'hairline text-ink-500 hover:border-ink-300',
                    )}
                  >
                    {c.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* What the shop is doing that our rules cannot account for. Shown
              beside the counts rather than folded into them: the first is a
              measurement gap, the second is a merchandising one, and they go
              to different people. Both hidden at zero — a row of zeroes reads
              as a broken panel rather than as good news. */}
          {(sum(unexplained) > 0 || sum(banned) > 0) && (
            <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-1 rounded-md border hairline bg-paper-sunk px-3 py-2">
              {sum(unexplained) > 0 && (
                <div className="flex items-baseline gap-2">
                  <span className="eyebrow" title="On sale, and our six liveness rules say it should not be. The gap between what is true and what we can explain.">
                    Selling, unexplained
                  </span>
                  {MARKETS.map((m) => (
                    <span key={m} className="font-mono text-[11px] tabular-nums text-ink-500">
                      {m} {unexplained[m]}
                    </span>
                  ))}
                </div>
              )}
              {sum(banned) > 0 && (
                <div className="flex items-baseline gap-2">
                  <span className="eyebrow" title="On sale despite a confirmed ban in PL_comment. The rule is right; nothing downstream of it acted.">
                    Banned but live
                  </span>
                  {MARKETS.map((m) => (
                    <span key={m} className="font-mono text-[11px] tabular-nums text-danger-600">
                      {m} {banned[m]}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {bulkOpen && (
            <BulkSetValue
              modelColors={[...selected]}
              onClose={() => setBulkOpen(false)}
              onApplied={(out) => {
                setBulkOpen(false)
                setBulkNote(
                  `${out.applied} set as approved` +
                    (out.skipped ? `, ${out.skipped} left as they were` : '') +
                    ' — on the product pages now, in this grid after the next rebuild.',
                )
              }}
            />
          )}

          {/* bulk actions appear only with a selection */}
          {selected.size > 0 && (
            <div className="mb-3 flex items-center gap-2 rounded-md border border-brand-600 bg-brand-50 px-3 py-2">
              <span className="font-mono text-[12px] tabular-nums text-brand-900">
                {selected.size} selected
              </span>
              {bulkNote && <span className="text-[12px] text-ink-700">{bulkNote}</span>}
              <div className="ml-auto flex gap-2">
                <BulkButton onClick={() => setBulkOpen(true)}>Set value…</BulkButton>
                <BulkButton onClick={() => void exportSelection()} disabled={exporting}>
                  {exporting ? 'Building…' : 'Export to spreadsheet'}
                </BulkButton>
                <button
                  onClick={() => setSelected(new Set())}
                  className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-500 hover:text-ink-900"
                >
                  clear
                </button>
              </div>
            </div>
          )}

          {loading ? (
            <GridSkeleton />
          ) : products.length === 0 ? (
            <div className="rounded-md border hairline bg-paper-card px-6 py-12 text-center shadow-card">
              <div className="font-serif text-[18px] text-ink-900">
                {searchIsTheProblem ? 'No product matches that' : 'Nothing in this scope'}
              </div>
              <p className="mx-auto mt-2 max-w-prose text-[12.5px] text-ink-500">
                {searchIsTheProblem ? (
                  <>
                    Nothing matches{' '}
                    <b className="font-medium text-ink-700">{query}</b> — codes
                    match with or without dots, and names match on any part.
                  </>
                ) : relax ? (
                  <>
                    Removing <b className="font-medium text-ink-700">{FACET_LABELS[relax.key]}</b>{' '}
                    would return {relax.count} options.
                  </>
                ) : (
                  'No products match. Try clearing a filter.'
                )}
              </p>
              {searchIsTheProblem && (
                <button
                  onClick={() => setDraft('')}
                  className="mt-4 rounded-2xs bg-brand-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-brand-600"
                >
                  Clear search
                </button>
              )}
              {!searchIsTheProblem && relax && (
                <button
                  onClick={() => {
                    const next = { ...scope }
                    delete next[relax.key]
                    update(next)
                  }}
                  className="mt-4 rounded-2xs bg-brand-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-brand-600"
                >
                  Remove {FACET_LABELS[relax.key]}
                </button>
              )}
            </div>
          ) : view === 'grid' ? (
            <ProductGrid
              products={shown}
              selected={selected}
              onToggleSelect={toggleSelect}
            />
          ) : (
            <ProductTable
              products={shown}
              columns={columns}
              selected={selected}
              onToggleSelect={toggleSelect}
              onToggleAll={toggleAll}
            />
          )}

          {shown.length < products.length && (
            <div className="mt-4 flex items-center justify-center gap-3">
              <span className="font-mono text-[11px] tabular-nums text-ink-400">
                showing {shown.length.toLocaleString()} of{' '}
                {products.length.toLocaleString()}
              </span>
              <button
                onClick={() => setLimit((l) => l + PAGE)}
                className="rounded-2xs border hairline bg-paper-card px-3 py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300"
              >
                Load {Math.min(PAGE, products.length - shown.length)} more
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** How many results render at once. The scope bounds the data; this bounds
 *  the DOM. */
const PAGE = 60

/**
 * The whole query string, from the three things that make it up.
 *
 * `queryFromScope` only knows about facets, so anything rebuilding the URL
 * from a scope has to put the search text and the sort order back. That used
 * to be a line inside each caller — and each new caller was one more chance
 * to forget one and have a facet click silently reset it. One function
 * instead, and there is nowhere left to forget.
 */
function urlFor(scope: Scope, query: string, sort: SortOrder): URLSearchParams {
  const out = queryFromScope(scope)
  if (query.trim()) out.set('q', query.trim())
  const s = sortToQuery(sort)
  if (s) out.set('sort', s)
  return out
}

/** Sum a per-market tally. The chips are hidden at zero, and "is any market
 *  non-zero" is the only question asked of the whole row. */
function sum(tally: Record<string, number>): number {
  return Object.values(tally).reduce((a, b) => a + b, 0)
}

/** `2026-09-07T14:20:11+00:00` -> `7 Sep 14:20`.
 *
 *  Date as well as time, deliberately. A bare `14:20` beside a live count is
 *  read as today by anyone who does not stop to check, and the whole reason
 *  this stamp exists is that a figure eighteen days old was read as today's. */
function asOf(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return iso
  return at.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function ViewButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium',
        active ? 'bg-brand-50 text-brand-900' : 'bg-paper-card text-ink-500 hover:bg-paper-sunk',
      )}
    >
      {children}
    </button>
  )
}

function BulkButton({
  children,
  onClick,
  disabled,
}: {
  children: React.ReactNode
  onClick?: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-2xs border hairline bg-paper-card px-2.5 py-1 text-[12px] font-medium text-ink-700 hover:border-ink-300 disabled:opacity-50"
    >
      {children}
    </button>
  )
}
