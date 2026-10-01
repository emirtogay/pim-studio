import type { Facet, FacetKey, Market, ProductSummary } from './api'
import { MARKETS } from './api'

/**
 * Scope — the set of active facet selections.
 *
 * Held in the URL query string, not in component state. That's what makes a
 * scope survive navigating into a product and back, makes it shareable, and
 * makes a saved scope just a stored query string. "The 26AW polos" is a place
 * you return to over days, not a filter you rebuild each morning.
 *
 * Within one facet, values are OR-ed (Polo or T-shirt). Across facets they're
 * AND-ed (26AW and a polo and missing a description). That's the standard
 * faceted-search contract and it's what people expect without being told.
 */

export type Scope = Partial<Record<FacetKey, string[]>>

export const FACET_LABELS: Record<FacetKey, string> = {
  season: 'Season',
  mainCategory: 'Category',
  subCategory: 'Sub-category',
  gender: 'Gender',
  readiness: 'Readiness',
  missing: 'Missing',
  defect: 'Defects',
}

/** Readiness is derived rather than stored, so its values are fixed here. */
export const READINESS_VALUES = [
  'live everywhere',
  'live somewhere',
  'blocked',
  'not opened',
] as const

export function readinessOf(p: ProductSummary): string {
  const flags = MARKETS.map((m) => p.live[m] ?? false)
  if (flags.every(Boolean)) return 'live everywhere'
  if (flags.some(Boolean)) return 'live somewhere'
  // Nothing live anywhere: distinguish "doesn't exist upstream yet" from
  // "exists but blocked", because they go to entirely different people.
  const notOpened = MARKETS.some((m) => p.blockedBy[m]?.includes('Not opened'))
  return notOpened ? 'not opened' : 'blocked'
}

/**
 * Free-text search, deliberately not a facet.
 *
 * Facets are closed sets you pick from; this is an open string you type. It
 * shares the URL with the scope for the same reasons — shareable, survives
 * navigating into a product and back — but it is AND-ed with the facets
 * rather than being one of them.
 *
 * Codes are written both ways upstream: the 26AW order file carries
 * `2001524.000` on one sheet and `2001524000` on another, and people paste
 * whichever they have. So a query is matched twice — literally, and with
 * separators stripped from both sides — which also makes `AH4463 132` and
 * `ah4463.132` find the same product.
 */
const squash = (s: string) => s.toLowerCase().replace(/[\s.\-_/]/g, '')

export function matchesQuery(p: ProductSummary, query: string): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  if (p.modelColor.toLowerCase().includes(needle)) return true
  if (p.name.toLowerCase().includes(needle)) return true
  const flat = squash(needle)
  return flat.length > 0 && squash(p.modelColor).includes(flat)
}

export function applySearch(
  products: ProductSummary[],
  query: string,
): ProductSummary[] {
  if (!query.trim()) return products
  return products.filter((p) => matchesQuery(p, query))
}

export function scopeFromQuery(params: URLSearchParams): Scope {
  const scope: Scope = {}
  for (const key of Object.keys(FACET_LABELS) as FacetKey[]) {
    const values = params.getAll(key)
    if (values.length) scope[key] = values
  }
  return scope
}

export function queryFromScope(scope: Scope): URLSearchParams {
  const params = new URLSearchParams()
  for (const [key, values] of Object.entries(scope)) {
    for (const v of values ?? []) params.append(key, v)
  }
  return params
}

export function toggleFacet(scope: Scope, key: FacetKey, value: string): Scope {
  const current = scope[key] ?? []
  const next = current.includes(value)
    ? current.filter((v) => v !== value)
    : [...current, value]
  const out = { ...scope }
  if (next.length) out[key] = next
  else delete out[key]
  return out
}

/** Every facet value a product contributes to. `missing` and `defect` are
 *  multi-valued — one product missing three attributes counts in all three. */
function valuesFor(p: ProductSummary, key: FacetKey): string[] {
  switch (key) {
    case 'season': return [p.season]
    case 'mainCategory': return [p.mainCategory]
    case 'subCategory': return [p.subCategory]
    case 'gender': return [p.gender]
    case 'readiness': return [readinessOf(p)]
    case 'missing': return p.missing
    case 'defect': return p.defects
  }
}

function matchesFacet(p: ProductSummary, key: FacetKey, selected: string[]): boolean {
  const values = valuesFor(p, key)
  return selected.some((s) => values.includes(s))
}

export function applyScope(products: ProductSummary[], scope: Scope): ProductSummary[] {
  const keys = Object.keys(scope) as FacetKey[]
  return products.filter((p) =>
    keys.every((k) => matchesFacet(p, k, scope[k] ?? [])),
  )
}

/**
 * Facet counts, each computed against the scope with *that* facet's own
 * selection removed.
 *
 * The subtlety: if counts were computed against the full scope, every
 * unselected value in an active facet would read zero — you'd select "Polo"
 * and "T-shirt" would claim there were none, which is false and makes the
 * facet unusable for widening. Excluding the facet's own selection is what
 * makes the numbers mean "how many would I get if I added this".
 */
export function buildFacets(products: ProductSummary[], scope: Scope): Facet[] {
  return (Object.keys(FACET_LABELS) as FacetKey[]).map((key) => {
    const others = { ...scope }
    delete others[key]
    const pool = applyScope(products, others)

    const counts = new Map<string, number>()
    for (const p of pool) {
      for (const v of valuesFor(p, key)) {
        counts.set(v, (counts.get(v) ?? 0) + 1)
      }
    }
    // Keep selected values visible even at zero, so a selection can always be
    // undone from the rail it was made in.
    for (const v of scope[key] ?? []) {
      if (!counts.has(v)) counts.set(v, 0)
    }

    const values = [...counts.entries()]
      .map(([value, count]) => ({ value, count }))
      .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))

    return { key, label: FACET_LABELS[key], values }
  })
}

/** For the empty state: which single facet, if relaxed, would return the most
 *  results? Better than "no results, try again". */
export function bestFacetToRelax(
  products: ProductSummary[],
  scope: Scope,
): { key: FacetKey; count: number } | null {
  const keys = Object.keys(scope) as FacetKey[]
  if (keys.length < 2) return null
  let best: { key: FacetKey; count: number } | null = null
  for (const key of keys) {
    const relaxed = { ...scope }
    delete relaxed[key]
    const count = applyScope(products, relaxed).length
    if (count > 0 && (!best || count > best.count)) best = { key, count }
  }
  return best
}

/** Per-market live counts for the scoped set — the market strip is a
 *  comparison, not a filter, so it reports rather than narrows. */
export function marketTally(products: ProductSummary[]): Record<Market, number> {
  const out = {} as Record<Market, number>
  for (const m of MARKETS) out[m] = products.filter((p) => p.live[m]).length
  return out
}

/**
 * Options the shop is selling that our own six rules call blocked.
 *
 * Not a defect count and not noise: it is the distance between what is true
 * and what we can explain. On 2026-09-07 it was ~600 in Poland and ~500 in
 * Czechia, because both markets had been moved to catalogue 601 and the rules
 * were still reading the catalogues of switched-off channels — a migration
 * nothing on this page could previously have surfaced.
 *
 * Zero is the goal and is not the same as the number being uninteresting:
 * it means the rules currently explain the shop, which is exactly the claim
 * worth watching for the day it stops being true.
 */
export function unexplainedTally(
  products: ProductSummary[],
): Record<Market, number> {
  const out = {} as Record<Market, number>
  for (const m of MARKETS) {
    out[m] = products.filter((p) => p.live[m] && p.blockedBy[m]).length
  }
  return out
}

/** Options on sale that our ban list says should not be.
 *
 *  Kept apart from `unexplainedTally` because it is not a measurement problem
 *  at all — the rule is right and nothing downstream of it acted. 163 options
 *  carrying a confirmed ban were selling in Poland on 2026-09-07. */
export function bannedButLive(products: ProductSummary[]): Record<Market, number> {
  const out = {} as Record<Market, number>
  for (const m of MARKETS) {
    out[m] = products.filter((p) => p.live[m] && p.blockedBy[m] === 'Banned').length
  }
  return out
}

// ── Sort ─────────────────────────────────────────────────────────────────

/**
 * Sort order — the third thing the URL carries, alongside the scope and the
 * search.
 *
 * In the URL for the same reason the scope is: "26AW, least recently touched
 * first" is a place you send someone, not a control you ask them to set for
 * themselves. It is a query parameter rather than a facet because it reorders
 * the set instead of narrowing it — the option count, the market strip and
 * the facet rail all read exactly the same whichever order is chosen.
 */
export type SortKey = 'code' | 'season' | 'changed' | 'name'

export interface SortOrder {
  key: SortKey
  desc: boolean
}

export const SORT_KEYS: SortKey[] = ['code', 'season', 'changed', 'name']

export const SORT_LABELS: Record<SortKey, string> = {
  code: 'Code',
  season: 'Season',
  changed: 'Changed',
  name: 'Name',
}

/** What each direction reads as, per field. "Descending" means nothing to
 *  anyone looking at a season; "newest first" is the same claim and says it.
 *  The words are shown rather than an arrow for the same reason the `as of`
 *  stamp carries a date — a glyph you have to decode gets decoded wrong. */
export const SORT_DIRECTIONS: Record<SortKey, { asc: string; desc: string }> = {
  code: { asc: 'A–Z', desc: 'Z–A' },
  season: { asc: 'oldest first', desc: 'newest first' },
  changed: { asc: 'oldest first', desc: 'newest first' },
  name: { asc: 'A–Z', desc: 'Z–A' },
}

/** The direction a field is wanted in when it is first picked. A season or a
 *  change date is nearly always wanted newest-first; a code or a name A–Z.
 *  Getting this wrong costs a second click on every single use. */
const NATURAL_DESC: Record<SortKey, boolean> = {
  code: false,
  season: true,
  changed: true,
  name: false,
}

/** Code ascending — the order the extract is built in, so an unsorted page
 *  and an explicitly code-sorted one agree. */
export const DEFAULT_SORT: SortOrder = { key: 'code', desc: false }

export function naturalSort(key: SortKey): SortOrder {
  return { key, desc: NATURAL_DESC[key] }
}

export function sortFromQuery(params: URLSearchParams): SortOrder {
  const [key, dir] = (params.get('sort') ?? '').split('-')
  if (!SORT_KEYS.includes(key as SortKey)) return DEFAULT_SORT
  const k = key as SortKey
  if (dir === 'asc' || dir === 'desc') return { key: k, desc: dir === 'desc' }
  return naturalSort(k)
}

/** Omitted at the default, so a plain scope keeps a clean URL and a shared
 *  link only ever carries an order somebody actually chose. */
export function sortToQuery(sort: SortOrder): string | null {
  if (sort.key === DEFAULT_SORT.key && sort.desc === DEFAULT_SORT.desc) return null
  return `${sort.key}-${sort.desc ? 'desc' : 'asc'}`
}

/**
 * A season as a number, so "newest collection first" can mean something.
 *
 * Written both ways upstream — `26AW` in the PL season field on nearly every
 * row, `AW26` on a handful — and SS precedes AW inside a year, so the rank is
 * `year * 2 + half` rather than anything sorting the string would give you.
 *
 * `null` for everything that is not a dated collection: OUTLET, OUTLETAW,
 * REMOVED, `removed since 20.03.2025`, and the em dash standing in for a
 * product with no PL season at all. Those are not old collections, they are
 * not collections, and `applySort` holds them at the end in both directions.
 * Eleven thousand OUTLET options sitting above 26AW under "oldest first"
 * would break the sort in precisely the case it is wanted for.
 */
export function seasonRank(season: string): number | null {
  const s = season.trim().toUpperCase()
  let year: string | undefined
  let half: string | undefined
  const forward = /^(\d{2})(SS|AW)$/.exec(s)
  if (forward) [, year, half] = forward
  else {
    const reversed = /^(SS|AW)(\d{2})$/.exec(s)
    if (reversed) [, half, year] = reversed
  }
  if (year === undefined || half === undefined) return null
  return (2000 + Number(year)) * 2 + (half === 'AW' ? 1 : 0)
}

const MONTHS = [
  'jan', 'feb', 'mar', 'apr', 'may', 'jun',
  'jul', 'aug', 'sep', 'oct', 'nov', 'dec',
]

/**
 * `lastChangedAt` as a timestamp.
 *
 * The extract emits it already formatted — `12 Aug 2026`, from Python's
 * `%d %b %Y` — because until now it was only ever printed on the product
 * page. Sorting needs a number, and the string sorts by day-of-month, which
 * would interleave every August with every other. ISO is accepted as well,
 * so this keeps working the day the generator emits the raw timestamp.
 */
export function changedAt(value: string | null | undefined): number | null {
  if (!value) return null
  const s = value.trim()
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) {
    const t = Date.parse(s)
    return Number.isNaN(t) ? null : t
  }
  const m = /^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})$/.exec(s)
  if (!m) return null
  const month = MONTHS.indexOf(m[2].toLowerCase())
  if (month < 0) return null
  return Date.UTC(Number(m[3]), month, Number(m[1]))
}

function sortKeyOf(p: ProductSummary, key: SortKey): string | number | null {
  switch (key) {
    case 'code':
      return p.modelColor
    case 'name':
      return p.name.trim().toLowerCase() || null
    case 'season':
      return seasonRank(p.season)
    case 'changed':
      return changedAt(p.lastChangedAt)
  }
}

/** Codes are compared by code point, not by locale. That is the order the
 *  extract is built in and the order Omnitron lists them in, and a locale
 *  collation quietly reorders everything beginning with punctuation — of
 *  which this catalogue has plenty. Names are human text, in four languages,
 *  and get the locale comparison they need. */
function compareKeys(key: SortKey, a: string | number, b: string | number): number {
  if (typeof a !== 'string' && typeof b !== 'string') return a - b
  const [x, y] = [String(a), String(b)]
  if (key === 'name') return x.localeCompare(y)
  return x < y ? -1 : x > y ? 1 : 0
}

/**
 * Order the scoped set.
 *
 * Keys are computed once per product rather than inside the comparator. At
 * eighteen thousand options a parse per comparison is a quarter of a million
 * regex runs for one sort; one per product is eighteen thousand.
 *
 * Two rules the comparator holds to. Products with no key — no dated season,
 * no Omnitron timestamp — go last whichever way the sort runs, because
 * "unknown" is not a position on the scale. And every comparison falls back
 * to the ModelColor, so equal keys keep one fixed order: the grid renders
 * `slice(0, limit)`, and rows that reshuffle between "Load more" clicks would
 * show some products twice and others never.
 */
export function applySort(
  products: ProductSummary[],
  sort: SortOrder,
): ProductSummary[] {
  const dir = sort.desc ? -1 : 1
  const keyed = products.map((p) => ({ p, k: sortKeyOf(p, sort.key) }))
  keyed.sort((a, b) => {
    if (a.k === null || b.k === null) {
      if (a.k !== b.k) return a.k === null ? 1 : -1
    } else {
      const d = compareKeys(sort.key, a.k, b.k)
      if (d !== 0) return d * dir
    }
    return a.p.modelColor < b.p.modelColor ? -1 : a.p.modelColor > b.p.modelColor ? 1 : 0
  })
  return keyed.map((e) => e.p)
}
