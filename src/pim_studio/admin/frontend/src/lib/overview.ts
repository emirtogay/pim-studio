/**
 * The go-live funnel, as it arrives from `overview.json`.
 *
 * Unlike `coverage.ts`, nothing is computed here. Coverage re-implements a
 * verdict in the browser because a facet click has to re-answer "how many fall
 * short" over thousands of products, and a round trip between a checkbox and
 * its own count is the difference between a tool and a form. The funnel has no
 * such interaction: the season picker chooses between slices the generator
 * already built. So this file is types, two selectors and a URL builder — and
 * every judgement, every count and every deep link stays on the Python side
 * where it is tested.
 *
 * That includes the links. `LinkDescriptor` carries the facet key and value
 * that `rules/funnel` decided; this file only turns them into a query string.
 * The precedent is `Blocker` in `rules/liveness`, whose one string mismatch
 * across this boundary left an entire readiness bucket dead on real data — and
 * an empty work queue looks exactly like a job already done.
 */

import type { Market } from './api'
import { MARKETS } from './api'

/** The slice key meaning "no season filter". */
export const ALL_SLICE = 'all'

export type StageKey =
  | 'assortment'
  | 'opened'
  | 'base'
  | 'imaged'
  | 'localised'
  | 'priced'
  | 'stocked'
  | 'live'

/** Who fixes it. `null` on a stage that is a consequence, not a queue. */
export type StageOwner = 'partner' | 'us' | null

export interface StageMeta {
  key: StageKey
  label: string
  /** The stage's number in the design brief, kept through the reordering. */
  briefIndex: number
  owner: StageOwner
  perMarket: boolean
  /** False when no source we hold could answer it. Shown greyed, not hidden. */
  computable: boolean
  blurb: string
  attributes: string[]
}

/**
 * One stage's standing. `indeterminate` is not a gap and not a pass — it is
 * "no source could say", and it sits beside the bar rather than inside it.
 */
export interface Bucket {
  passed: number
  indeterminate: number
}

export interface LinkDescriptor {
  page: 'products' | 'coverage' | 'conflicts'
  params: [string, string][]
}

export interface Reason {
  key: string
  label: string
  count: number
  owner: 'us' | 'partner' | 'system'
  /** What to actually do, from the Python side's own remedy text. */
  remedy: string
  /** Null when no facet would find these — better than a link to nothing. */
  link: LinkDescriptor | null
}

export interface DropSide {
  /** Options whose *first* failed stage is this one. */
  count: number
  reasons: Reason[]
}

export interface Drop {
  from: StageKey
  to: StageKey
  shared?: DropSide
  markets?: Record<Market, DropSide>
}

export interface Defect {
  key: string
  label: string
  count: number
  link: LinkDescriptor | null
}

export interface SliceDeltas {
  baselineDate: string
  ageDays: number
  live: number
  blocked: number
  waitingOnPartner: number
  liveByMarket: Record<Market, number>
}

export interface Slice {
  key: string
  population: number
  counts: Record<StageKey, { shared?: Bucket; markets?: Record<Market, Bucket> }>
  drops: Drop[]
  headline: { live: number; blocked: number; waitingOnPartner: number }
  liveNow: Record<Market, number>
  liveNowAnywhere: number
  /** Live on the site with an earlier stage still incomplete. */
  liveDespiteGaps: number
  defects: Defect[]
}

export interface Caveat {
  key: string
  text: string
  stage?: StageKey
}

export interface SeasonOption {
  key: string
  label: string
  count: number
}

export interface OverviewExtract {
  version: number
  generatedAt: string
  markets: Market[]
  population: number
  stages: StageMeta[]
  seasons: SeasonOption[]
  slices: Record<string, Slice>
  conflicts: { productName: number; total: number }
  caveats: Caveat[]
  /** Null until a week of nights has run. Not the same as a delta of zero. */
  deltas: Record<string, SliceDeltas> | null
}

/**
 * The fallback when no extract has been generated.
 *
 * Deliberately empty rather than a fixture: an invented funnel that renders
 * convincingly is how this panel showed 28 products that did not exist.
 */
export const emptyOverview = (): OverviewExtract => ({
  version: 0,
  generatedAt: '',
  markets: [...MARKETS],
  population: 0,
  stages: [],
  seasons: [],
  slices: {},
  conflicts: { productName: 0, total: 0 },
  caveats: [],
  deltas: null,
})

/** A deep link's href. Repeated keys are how the Products facets combine. */
export function hrefFor(link: LinkDescriptor): string {
  const params = new URLSearchParams()
  for (const [key, value] of link.params) params.append(key, value)
  const query = params.toString()
  return `/${link.page}${query ? `?${query}` : ''}`
}

/**
 * The same link, carrying the season the reader is currently looking at.
 *
 * Without this a drop-off inside "26SS" would open the whole catalogue's
 * queue, and the number on the page would not match the number of rows that
 * came back — which is the fastest way to make someone stop trusting both.
 * Coverage links are left alone: that page keeps its scope in component state,
 * so a season in the URL would be silently ignored.
 */
export function hrefForInSeason(link: LinkDescriptor, season: string): string {
  if (!season || season === ALL_SLICE || link.page !== 'products') {
    return hrefFor(link)
  }
  return hrefFor({ ...link, params: [...link.params, ['season', season]] })
}

export interface BiggestDrop {
  from: StageKey
  to: StageKey
  market?: Market
  count: number
}

/**
 * The stage that stopped the most options, so the page can point at it.
 *
 * The brief asks for the biggest bottleneck to be obvious in under two
 * seconds. Per market rather than summed: four markets each losing 300 at
 * different stages is not one bottleneck of 1,200, and summing would invent a
 * stage nobody is actually stuck at.
 */
export function biggestDrop(slice: Slice | undefined): BiggestDrop | null {
  if (!slice) return null
  let best: BiggestDrop | null = null
  for (const drop of slice.drops) {
    if (drop.shared) {
      if (drop.shared.count > (best?.count ?? 0)) {
        best = { from: drop.from, to: drop.to, count: drop.shared.count }
      }
    }
    for (const market of MARKETS) {
      const side = drop.markets?.[market]
      if (side && side.count > (best?.count ?? 0)) {
        best = { from: drop.from, to: drop.to, market, count: side.count }
      }
    }
  }
  return best && best.count > 0 ? best : null
}

/** One stage's bucket in one market, whatever its scope. */
export function bucketFor(
  slice: Slice,
  stage: StageMeta,
  market: Market,
): Bucket | null {
  const counts = slice.counts?.[stage.key]
  if (!counts) return null
  return counts.shared ?? counts.markets?.[market] ?? null
}

/** One drop's side in one market, whatever its scope. */
export function dropSideFor(drop: Drop, market: Market): DropSide | null {
  return drop.shared ?? drop.markets?.[market] ?? null
}
