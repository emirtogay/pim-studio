/**
 * Coverage, computed in the browser from `coverage.json`.
 *
 * Every facet click and every attribute ticked re-answers "how many options
 * fall short", over thousands of products. Doing that on the server would put
 * a round trip between a checkbox and its own count, which is the difference
 * between a tool you explore with and a form you submit.
 *
 * ## What is duplicated here, and what deliberately is not
 *
 * The four-way verdict below is a second implementation of
 * `rules/coverage.evaluate_product`. That is a real cost and it is taken
 * knowingly, because everything *judgement-bearing* stays on the Python side
 * and arrives in the extract:
 *
 *   - which attributes exist, their labels, and who owns them
 *   - which sub-categories each attribute applies to
 *   - which attribute/market pairs have ever been read from a source
 *   - the issue vocabulary and the preset profiles
 *
 * So this file cannot invent a requirement, cannot decide an attribute is
 * inapplicable, and cannot claim a column was read. What it does is a
 * three-branch lookup, and `POST /api/coverage/summary` returns the server's
 * answer for the same scope whenever anyone wants to check it.
 *
 * The precedent is `Blocker` in `rules/liveness`, whose reason strings cross
 * the same boundary — and whose one mismatch, `NOT_OPENED`, made an entire
 * readiness bucket dead on real data. The lesson taken from it is to move
 * *data* across the boundary and keep the duplicated code down to something
 * with no room for a mismatch.
 */

import type { AttributeOwner, Market } from './api'
import { MARKETS } from './api'

/** Market sentinel for an attribute that has one value for all markets. */
export const SHARED = '*'

export type Verdict = 'met' | 'missing' | 'unknown' | 'na'
export type Standing = 'complete' | 'incomplete' | 'indeterminate'

export interface CoverageAttribute {
  key: string
  label: string
  owner: AttributeOwner
  group: string
  perMarket: boolean
  /** Comma list of sub-categories it applies to; empty means all of them. */
  appliesWhen: string
  /** Comma list of markets a value has ever been seen for. */
  sourced: string
}

export interface CoverageIssue {
  key: string
  label: string
  definition: string
  remedy: string
  /** True when the count rests on the unreconciled Omnitron stock read. */
  stockBacked: boolean
}

export interface RequirementSpec {
  attribute: string
  /** Empty means all four, and is ignored for a market-agnostic attribute. */
  markets: string[]
}

export interface ProfileSpec {
  key: string
  label: string
  blurb: string
  requirements: RequirementSpec[]
}

export interface CoverageProduct {
  mc: string
  name: string
  season: string
  sub: string
  gender: string
  skus: number
  /** Comma list of market catalogues the option belongs to. */
  markets: string
  flags: string[]
  /** attribute key → comma list of markets carrying a value. */
  filled: Record<string, string>
  comment: string
  image: string
  stock: number
}

export interface CoverageExtract {
  version: number
  generatedAt: string
  markets: Market[]
  attributes: CoverageAttribute[]
  issues: CoverageIssue[]
  profiles: ProfileSpec[]
  stockCaveat: string
  unread: [string, string][]
  products: CoverageProduct[]
}

const EMPTY_EXTRACT: CoverageExtract = {
  version: 0,
  generatedAt: '',
  markets: [...MARKETS],
  attributes: [],
  issues: [],
  profiles: [],
  stockCaveat: '',
  unread: [],
  products: [],
}

export const emptyExtract = (): CoverageExtract => EMPTY_EXTRACT

/** `"PL,CZ"` → `["PL","CZ"]`, and `""` → `[]` rather than `[""]`. */
export const marketsOf = (text: string | undefined): string[] =>
  (text ?? '').split(',').filter(Boolean)

/**
 * Which markets a requirement is asked in.
 *
 * A market-agnostic attribute is asked once whatever the picker says. Honouring
 * four markets on a single shared value would report one gap as four and
 * multiply the size of the work — the same normalisation
 * `Requirement.scopes` does on the Python side.
 */
export function scopesFor(
  requirement: RequirementSpec,
  attribute: CoverageAttribute | undefined,
): string[] {
  if (!attribute) return []
  if (!attribute.perMarket) return [SHARED]
  return requirement.markets.length ? requirement.markets : [...MARKETS]
}

/** Whether an attribute is a real requirement for a product's sub-category.
 *
 *  An unknown sub-category applies everything: we cannot show the attribute is
 *  inapplicable, and calling it `na` would delete work from a queue silently.
 *  Over-reporting is visible and dismissible; under-reporting is neither. */
export function appliesTo(attribute: CoverageAttribute, subCategory: string): boolean {
  const allowed = attribute.appliesWhen
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
  if (!allowed.length) return true
  const sub = (subCategory ?? '').trim().toLowerCase()
  if (!sub) return true
  return allowed.includes(sub)
}

export function verdictFor(
  product: CoverageProduct,
  attribute: CoverageAttribute,
  market: string,
): Verdict {
  if (!appliesTo(attribute, product.sub)) return 'na'
  if (marketsOf(product.filled[attribute.key]).includes(market)) return 'met'
  if (marketsOf(attribute.sourced).includes(market)) return 'missing'
  return 'unknown'
}

export interface Gap {
  attribute: string
  label: string
  /** `ALL` for a market-agnostic attribute, else the market code. */
  scope: string
  owner: AttributeOwner
}

export interface ProductVerdict {
  mc: string
  standing: Standing
  met: number
  missing: number
  unknown: number
  na: number
  gaps: Gap[]
}

export type Registry = Record<string, CoverageAttribute>

export function registryOf(extract: CoverageExtract): Registry {
  return Object.fromEntries(extract.attributes.map((a) => [a.key, a]))
}

export function evaluate(
  product: CoverageProduct,
  requirements: RequirementSpec[],
  registry: Registry,
): ProductVerdict {
  let met = 0
  let missing = 0
  let unknown = 0
  let na = 0
  const gaps: Gap[] = []

  for (const requirement of requirements) {
    const attribute = registry[requirement.attribute]
    // An unknown key is a bug in the requirement, not a gap in the product.
    // Counting it missing would send somebody looking for a field that does
    // not exist.
    if (!attribute) continue
    for (const market of scopesFor(requirement, attribute)) {
      const verdict = verdictFor(product, attribute, market)
      if (verdict === 'met') met++
      else if (verdict === 'na') na++
      else if (verdict === 'unknown') unknown++
      else {
        missing++
        gaps.push({
          attribute: attribute.key,
          label: attribute.label,
          scope: market === SHARED ? 'ALL' : market,
          owner: attribute.owner,
        })
      }
    }
  }

  const standing: Standing = missing
    ? 'incomplete'
    : unknown
      ? 'indeterminate'
      : 'complete'
  return { mc: product.mc, standing, met, missing, unknown, na, gaps }
}

// ── rollups ──────────────────────────────────────────────────────────────

export interface RequirementRow {
  attribute: string
  label: string
  market: string
  owner: AttributeOwner
  met: number
  missing: number
  unknown: number
  na: number
  /** met / (met + missing). Null when nothing was judgeable — which must not
   *  render as 0%: "never read" and "nobody filled it in" are different work
   *  for different people, and one of them has nobody to give it to. */
  fillRate: number | null
  /** Every option in scope came back not-applicable, so the requirement
   *  contributed nothing at all.
   *
   *  Usually that is honest — nobody asks a wallet for a sleeve type. But the
   *  registry matches `applies_when` against a sub-category that arrives as a
   *  category breadcrumb, and when the two spellings diverge every option
   *  reads `na` and the requirement vanishes from the queue instead of showing
   *  as a gap. That failure is silent and it under-reports, so the row says so
   *  rather than sitting there as a column of zeroes. */
  blind: boolean
}

export interface CoverageResult {
  population: number
  complete: number
  incomplete: number
  indeterminate: number
  /** complete + incomplete — the denominator the rate is honest about. */
  judged: number
  completionRate: number | null
  requirements: RequirementRow[]
  issues: { issue: CoverageIssue; count: number }[]
  buckets: { key: string; label: string; count: number }[]
  verdicts: Map<string, ProductVerdict>
  /** True when any figure shown rests on the Omnitron stock read. */
  stockInvolved: boolean
}

export function computeCoverage(
  products: CoverageProduct[],
  requirements: RequirementSpec[],
  extract: CoverageExtract,
): CoverageResult {
  const registry = registryOf(extract)
  const rows = new Map<string, RequirementRow>()

  // Seed every requirement row up front so a requirement nothing matches still
  // appears, at zero, rather than dropping out of the table. A row that
  // vanishes reads as "handled".
  for (const requirement of requirements) {
    const attribute = registry[requirement.attribute]
    if (!attribute) continue
    for (const market of scopesFor(requirement, attribute)) {
      // NUL as the separator, because no attribute key or market can contain
      // one. Written as the escape rather than as a literal NUL byte: the
      // string is identical either way, but a raw NUL in the file makes git
      // classify this module as binary, which silently turns off line-ending
      // normalisation and renders every diff of it as one 600-line block.
      rows.set(`${attribute.key}\u0000${market}`, {
        attribute: attribute.key,
        label: attribute.label,
        market,
        owner: attribute.owner,
        met: 0,
        missing: 0,
        unknown: 0,
        na: 0,
        fillRate: null,
        blind: false,
      })
    }
  }

  const verdicts = new Map<string, ProductVerdict>()
  const issueCounts = new Map<string, number>(extract.issues.map((i) => [i.key, 0]))
  const bucketCounts = new Map<string, number>()

  let complete = 0
  let incomplete = 0
  let indeterminate = 0

  for (const product of products) {
    const verdict = evaluate(product, requirements, registry)
    verdicts.set(product.mc, verdict)
    if (verdict.standing === 'complete') complete++
    else if (verdict.standing === 'incomplete') incomplete++
    else indeterminate++

    // Per-requirement tallies need the verdict per market, which the
    // per-product rollup has already thrown away — so they are counted here
    // rather than derived from `verdict`.
    for (const requirement of requirements) {
      const attribute = registry[requirement.attribute]
      if (!attribute) continue
      for (const market of scopesFor(requirement, attribute)) {
        const row = rows.get(`${attribute.key}\u0000${market}`)
        if (!row) continue
        const cell = verdictFor(product, attribute, market)
        if (cell === 'met') row.met++
        else if (cell === 'missing') row.missing++
        else if (cell === 'unknown') row.unknown++
        else row.na++
      }
    }

    for (const flag of product.flags) {
      issueCounts.set(flag, (issueCounts.get(flag) ?? 0) + 1)
    }
    const key = bucketKey(product.flags, extract)
    bucketCounts.set(key, (bucketCounts.get(key) ?? 0) + 1)
  }

  const requirementRows = [...rows.values()]
    .map((row) => ({
      ...row,
      fillRate: row.met + row.missing ? row.met / (row.met + row.missing) : null,
      blind: row.na > 0 && row.met + row.missing + row.unknown === 0,
    }))
    .sort(
      (a, b) =>
        b.missing - a.missing ||
        b.unknown - a.unknown ||
        a.label.localeCompare(b.label) ||
        a.market.localeCompare(b.market),
    )

  const issues = extract.issues
    .map((issue) => ({ issue, count: issueCounts.get(issue.key) ?? 0 }))
    .sort((a, b) => b.count - a.count)

  const stockInvolved =
    issues.some((i) => i.issue.stockBacked && i.count > 0) ||
    requirements.some((r) => r.attribute === 'stock')

  return {
    population: products.length,
    complete,
    incomplete,
    indeterminate,
    judged: complete + incomplete,
    completionRate: complete + incomplete ? complete / (complete + incomplete) : null,
    requirements: requirementRows,
    issues,
    buckets: [...bucketCounts.entries()]
      .map(([key, count]) => ({ key, label: bucketLabel(key, extract), count }))
      .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key)),
    verdicts,
    stockInvolved,
  }
}

/** The one mutually-exclusive bucket an option falls into.
 *
 *  Ordered by the extract's own issue order so two spellings of the same
 *  combination cannot split one row into two. */
export function bucketKey(flags: string[], extract: CoverageExtract): string {
  const set = new Set(flags)
  const ordered = extract.issues.map((i) => i.key).filter((k) => set.has(k))
  if (!ordered.length) return 'clean'
  return ordered.join('+')
}

export function bucketLabel(key: string, extract: CoverageExtract): string {
  if (key === 'clean') return 'Clean — no issues'
  const byKey = new Map(extract.issues.map((i) => [i.key, i.label]))
  const parts = key.split('+').map((k) => byKey.get(k) ?? k)
  return parts.length === 1 ? `Only — ${parts[0]}` : parts.join(' + ')
}

// ── scoping the population ───────────────────────────────────────────────

/** "present anywhere", as opposed to `''` which does not filter at all. */
export const PRESENCE_ANY = 'any'

export interface CoverageScope {
  season: string[]
  sub: string[]
  gender: string[]
  flags: string[]
  standing: Standing[]
  inCatalogue: string
  /**
   * Price presence: `''` no filter, `'any'` priced somewhere, or a market
   * code for priced in that market. Per-market because a price is — the
   * extract has carried `filled.price` as a market list all along.
   *
   * Positive direction only. "Priced nowhere" is the `no_price` flag, one
   * row up the same rail, and saying it twice is how two controls that must
   * agree stop agreeing.
   */
  priced: string
  /**
   * Stock presence: `''` or `'any'`. No market code — one warehouse feeds all
   * four markets, so "in stock in PL" is not a question that has an answer.
   */
  stocked: string
  /** An uploaded or pasted list. Empty means the whole catalogue. */
  modelColors: string[]
  search: string
}

export const emptyScope = (): CoverageScope => ({
  season: [],
  sub: [],
  gender: [],
  flags: [],
  standing: [],
  inCatalogue: '',
  priced: '',
  stocked: '',
  modelColors: [],
  search: '',
})

export function scopeIsEmpty(scope: CoverageScope): boolean {
  return (
    !scope.season.length &&
    !scope.sub.length &&
    !scope.gender.length &&
    !scope.flags.length &&
    !scope.standing.length &&
    !scope.inCatalogue &&
    !scope.priced &&
    !scope.stocked &&
    !scope.modelColors.length &&
    !scope.search.trim()
  )
}

const squash = (s: string) => s.toLowerCase().replace(/[\s.\-_/]/g, '')

/**
 * The population, before any standing is known.
 *
 * Standing is deliberately *not* applied here: it depends on the requirement
 * set, so filtering on it has to happen after evaluation, and mixing the two
 * would make the requirement table describe a population selected by its own
 * output.
 */
export function applyScope(
  products: CoverageProduct[],
  scope: CoverageScope,
): CoverageProduct[] {
  const wanted = scope.modelColors.length ? new Set(scope.modelColors) : null
  const needle = scope.search.trim().toLowerCase()
  const flat = squash(needle)

  return products.filter((p) => {
    if (wanted && !wanted.has(p.mc)) return false
    if (scope.season.length && !scope.season.includes(p.season)) return false
    if (scope.sub.length && !scope.sub.includes(p.sub)) return false
    if (scope.gender.length && !scope.gender.includes(p.gender)) return false
    if (scope.inCatalogue && !marketsOf(p.markets).includes(scope.inCatalogue))
      return false
    if (scope.priced) {
      // The same `filled` the attribute matrix reads, so "priced in PL" here
      // and "price missing in PL" there are one fact rather than two.
      const priced = marketsOf(p.filled.price)
      const ok =
        scope.priced === PRESENCE_ANY
          ? priced.length > 0
          : priced.includes(scope.priced)
      if (!ok) return false
    }
    // `stock` is the best count any one SKU carries in any market, and the
    // threshold is zero — mirroring rules.liveness.STOCK_THRESHOLD, which is
    // what the no_stock flag is built from.
    if (scope.stocked && p.stock <= 0) return false
    if (scope.flags.length && !scope.flags.some((f) => p.flags.includes(f)))
      return false
    if (needle) {
      const hit =
        p.mc.toLowerCase().includes(needle) ||
        p.name.toLowerCase().includes(needle) ||
        (flat.length > 0 && squash(p.mc).includes(flat))
      if (!hit) return false
    }
    return true
  })
}

/** Counts for one facet, computed with that facet's own selection removed, so
 *  a number answers "what would I get if I added this" rather than "what is
 *  already selected". Same contract as the Products page's rail. */
export function facetCounts(
  products: CoverageProduct[],
  scope: CoverageScope,
  key: 'season' | 'sub' | 'gender' | 'flags',
): { value: string; count: number }[] {
  const relaxed = { ...scope, [key]: [] as string[] }
  const pool = applyScope(products, relaxed)
  const counts = new Map<string, number>()
  for (const product of pool) {
    const values = key === 'flags' ? product.flags : [product[key]]
    for (const value of values) {
      if (!value) continue
      counts.set(value, (counts.get(value) ?? 0) + 1)
    }
  }
  // Keep a selected value visible at zero so a selection can always be undone
  // from the rail it was made in.
  for (const value of scope[key]) if (!counts.has(value)) counts.set(value, 0)
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
}

// ── requirement sets ─────────────────────────────────────────────────────

/** A requirement set the user built by ticking boxes, as a URL fragment.
 *
 *  `name:PL+CZ,image_0` — attribute keys separated by commas, markets by plus.
 *  Short enough to live in a query string, which is what makes an analysis
 *  shareable: "the thing I am looking at" is a link, not a screenshot. */
/** An attribute plus the markets it was ticked in.
 *
 *  `RequirementSpec` under another name. The export picker asks a different
 *  question with exactly the same shape, and one shape means one
 *  `toggleRequirement` — two copies of its empty-means-all rule would come to
 *  disagree without either looking wrong. */
export type AttributeMarkets = RequirementSpec

export function encodeRequirements(requirements: RequirementSpec[]): string {
  return requirements
    .map((r) => (r.markets.length ? `${r.attribute}:${r.markets.join('+')}` : r.attribute))
    .join(',')
}

export function decodeRequirements(text: string): RequirementSpec[] {
  return text
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [attribute, markets] = part.split(':')
      return {
        attribute,
        markets: (markets ?? '').split('+').filter(Boolean),
      }
    })
}

/** Toggle one attribute/market cell in a requirement set.
 *
 *  Ticking a market-agnostic attribute stores no markets, because it has none;
 *  ticking every market of a per-market attribute stores all four rather than
 *  collapsing to "empty means all", so the picker shows what was actually
 *  chosen when it is read back from a URL. */
export function toggleRequirement(
  requirements: RequirementSpec[],
  attribute: CoverageAttribute,
  market: string | null,
): RequirementSpec[] {
  const index = requirements.findIndex((r) => r.attribute === attribute.key)
  const current = index >= 0 ? requirements[index] : null

  if (!attribute.perMarket || market === null) {
    return current
      ? requirements.filter((r) => r.attribute !== attribute.key)
      : [...requirements, { attribute: attribute.key, markets: [] }]
  }

  const markets = current ? [...current.markets] : []
  const at = markets.indexOf(market)
  if (at >= 0) markets.splice(at, 1)
  else markets.push(market)

  if (!markets.length) return requirements.filter((r) => r.attribute !== attribute.key)
  const next = { attribute: attribute.key, markets: MARKETS.filter((m) => markets.includes(m)) }
  if (index < 0) return [...requirements, next]
  return requirements.map((r) => (r.attribute === attribute.key ? next : r))
}

export function isRequired(
  requirements: RequirementSpec[],
  attribute: CoverageAttribute,
  market: string | null,
): boolean {
  const found = requirements.find((r) => r.attribute === attribute.key)
  if (!found) return false
  if (!attribute.perMarket || market === null) return true
  return found.markets.length === 0 || found.markets.includes(market)
}

export const OWNER_LABEL: Record<AttributeOwner, string> = {
  us: 'Ours',
  partner: 'Turkey',
  system: 'Feed',
}
