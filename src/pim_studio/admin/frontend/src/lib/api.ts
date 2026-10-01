/**
 * Thin fetch wrapper around the FastAPI backend.
 *
 * All endpoints live under `/api/*`. In dev, Vite proxies `/api` → :8000
 * (see `vite.config.ts`); in production, FastAPI serves both the API and
 * the built SPA from the same origin, so no origin rewriting is needed.
 *
 * Responses that aren't 2xx throw an `ApiError` with the status code and,
 * when available, the FastAPI-formatted `{ detail }` body. Callers show
 * `err.message` to the user and log `err.status` for triage.
 */

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    // Always send the session cookie. Vite's dev proxy forwards cookies
    // transparently; prod serves the SPA from the same origin so it just works.
    credentials: 'same-origin',
    ...init,
    headers: {
      'content-type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    // FastAPI returns `{ detail: "..." }` for HTTPException; fall back to
    // the raw response text for unexpected shapes (e.g. network error HTML).
    let message = `${res.status} ${res.statusText}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body?.detail) message = body.detail
    } catch {
      /* response body wasn't JSON — keep the default message */
    }
    // Session expired mid-use: bounce back to /login so the user can
    // re-authenticate. `/api/auth/me` is allowed to return 401 on boot —
    // the AuthProvider handles that path itself.
    if (
      res.status === 401 &&
      !path.startsWith('/api/auth/') &&
      typeof window !== 'undefined' &&
      window.location.pathname !== '/login'
    ) {
      const next = encodeURIComponent(
        window.location.pathname + window.location.search,
      )
      window.location.href = `/login?next=${next}`
    }
    throw new ApiError(message, res.status)
  }
  // 201 with a body is fine; 204 never will have one. Return `undefined as T`
  // for the 204 case rather than exploding on empty parse.
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

// ── Auth types ───────────────────────────────────────────────────────────

export type Role = 'developer' | 'planning' | 'manager'

/** Lifecycle of any background job — an Omnitron sync, a masterdata import,
 *  an export-file build. Shared so StatusPill renders all of them alike. */
export type RunStatus = 'success' | 'partial' | 'failed' | 'running' | 'skipped'

export interface ChangelogEntry {
  version: string
  date: string
  body: string
}

export interface ChangelogPayload {
  current_version: string
  entries: ChangelogEntry[]
}

export interface ConflictRow {
  modelColor: string
  attribute: string
  market: string
  skuCount: number
  sources: string
  /** Each candidate value, and how many of the product's SKUs reported it. */
  candidates: Record<string, number>
  /** What the importer wrote despite the disagreement, and which rule
   *  picked it. Null on rows written before 0.9.3. */
  resolvedValue: string | null
  resolvedRule: string | null
}

export interface ConflictList {
  total: number
  byAttribute: { attribute: string; count: number }[]
  rows: ConflictRow[]
  /** Conflicts recorded against a whole style rather than one colour, counted
   *  and not listed. Omnitron supplies no colour in `integration_base_code_format`
   *  for these, so every colour parses to the same key and the "disagreement"
   *  is ten products being asked to agree. Absent on an older server. */
  notAnOption?: { rows: number; styles: number }
}

/** One ingest. `mode` decides what its success actually proves. */
export interface SyncRunRow {
  id: number
  kind: string
  /** `full` read everything upstream holds and may therefore remove things.
   *  `incremental` read a window and can only add — it is never evidence that
   *  something is gone. */
  mode: 'full' | 'incremental'
  status: 'running' | 'success' | 'partial' | 'failed'
  startedAt: string | null
  finishedAt: string | null
  durationSeconds: number | null
  rowsSeen: number
  rowsImported: number
  rowsDropped: number
  /** Rows deleted because a full pass proved upstream no longer has them.
   *  Separate from dropped on purpose — a big number here is worth a look. */
  rowsRetired: number
  sourceFile: string | null
  errorSummary: string | null
  triggeredBy: string | null
}

/** Per-kind health. Two different ages, because they answer different
 *  questions: `ageHours` is "how old is this data", `lastFullAt` is "when did
 *  we last check whether anything was removed". */
export interface SyncKindHealth {
  kind: string
  lastStatus: string | null
  lastMode: string | null
  lastSuccessAt: string | null
  lastFullAt: string | null
  ageHours: number | null
  stale: boolean
  /** This kind's own threshold. Cadences differ — the Omnitron stages run
   *  nightly, image publishing weekly — so one number cannot judge both, and
   *  judging a weekly job by the nightly figure marks it stale six days out
   *  of seven. `null` for a kind nothing schedules (the push): it runs when a
   *  person starts it, so no age makes it late and it is never `stale`. */
  staleAfterHours: number | null
  needsReconcile: boolean
}

export interface SyncRunsPayload {
  runs: SyncRunRow[]
  kinds: SyncKindHealth[]
  /** Kinds whose last full pass is too old. `days: null` means never. */
  reconciliationOverdue: { kind: string; days: number | null }[]
  /** Rows still marked `running` — a process that died without finalising. */
  unfinished: { id: number; kind: string; startedAt: string }[]
  /** The default threshold, for kinds that declare no cadence of their own. */
  staleAfterHours: number
}

/** One product that stopped being sellable, and what it lost.
 *
 * Grouped by ModelColor rather than by row: a product losing stock in four
 * markets is one thing to open, not four. `modelColor` is null when the
 * retirement outlived the product record — it still happened, so it is still
 * listed, keyed on the SKU. */
export interface RetiredProduct {
  modelColor: string | null
  sku: string
  markets: string[]
  /** Which fields went — `price`, `retail_price`, `stock`. */
  fields: string[]
  /** "PL price" → "349.00". What was lost, not just that something was. */
  lastValues: Record<string, string | null>
  retiredAt: string | null
  syncRunId: number | null
  skuCount: number
}

export interface RetirementsPayload {
  total: number
  shown: number
  products: RetiredProduct[]
}

export interface ModuleCard {
  key: string
  name: string
  blurb: string
  url: string
}

export interface ModuleList {
  /** The key this panel calls itself, so the chooser can mark "you are here". */
  current: string
  modules: ModuleCard[]
}

export interface AuthUser {
  id: number
  email: string
  name: string | null
  role: Role
  // True when the running build's version is newer than what this user has
  // last opened on the What's-new page. Drives the bell pulse in the topbar.
  changelog_unread?: boolean
}

/** A row on the Users page.
 *
 *  snake_case, unlike most of this file, because it extends `AuthUser` — and
 *  `/api/auth/me` has emitted `changelog_unread` since the first release. One
 *  of the two had to bend and it is not the one already shipped.
 *
 *  There is no `password_hash` and there is no delete: `users` is shared with
 *  the price-comparison app, so removing a row takes that account with it and
 *  orphans every audit row pointing at the id. `is_active` is the off switch. */
export interface AdminUser extends AuthUser {
  is_active: boolean
  created_at: string | null
  /** Null means never signed in — a real state, not a missing value. */
  last_login_at: string | null
}

/** What a role can reach, served rather than typed into the frontend: the
 *  descriptions come from the same tuple `auth.py` gates on, so they cannot
 *  drift from what the API actually allows. */
export interface RoleInfo {
  key: Role
  blurb: string
}

export interface AuditEntry {
  id: number
  created_at: string
  user_email: string | null
  action: string
  method: string | null
  path: string | null
  status_code: number | null
  ip: string | null
  detail: string | null
}

export interface AuditList {
  /** Rows matching the current filters — what the pager needs, not the page. */
  total: number
  items: AuditEntry[]
  /** Facet counts over the time window **only**, ignoring the action and user
   *  filters they offer. Counted over the filtered rows instead, picking one
   *  chip would leave it standing alone with no way back — the filter bar
   *  would delete its own alternatives. */
  actions: { action: string; count: number }[]
  users: { email: string; count: number }[]
  windowDays: number
  /** The oldest row in the table, not in the window. Empty because a filter
   *  matched nothing and empty because the log started on Tuesday look
   *  identical, and the second is not evidence about anything. */
  firstAt: string | null
  /** Which actions mean somebody got in, or tried to. Named by the server so
   *  the page and the log cannot disagree about what counts as one. */
  accessActions: string[]
}


// ── Coverage ─────────────────────────────────────────────────────────────

/** What an uploaded or pasted list of codes turned out to contain.
 *
 *  `notFound` is the field that matters. A list of 500 codes that matches 380
 *  has told you something about the other 120, and an upload that reported on
 *  the 380 without saying so would be indistinguishable from one that worked. */
export interface CodeResolution {
  modelColors: string[]
  notFound: string[]
  matched: number
  rowsRead: number
  /** Which column the codes were read from — the step most likely to have
   *  picked the wrong one, so the page shows it and lets the user check. */
  column: string
  sheet: string
  /** Set when nothing resolved, explaining what was looked at. */
  problem: string
  catalogueSize: number
}

/** What a publish started from an uploaded list turned out to cover.
 *
 *  `overrides` is the half that needs saying out loud. The run publishes every
 *  code it was given — an explicit list is an instruction — so these are the
 *  ones the weekly job would have set aside, reported rather than acted on. A
 *  code can appear under more than one heading. */
export interface ImageListPublish {
  started: boolean
  dryRun: boolean
  /** The filename, or `pasted`. */
  source: string
  options: number
  rowsRead: number
  column: string
  sheet: string
  duplicates: number
  /** Searched, but Omnitron has no product with this code. Not an exclusion —
   *  the brand library holds photos for options this catalogue never carried,
   *  and that is often the reason for uploading a list. */
  notOnRecord: string[]
  /** The only values dropped: not shaped like a product code at all. Listed
   *  rather than counted, so a heading is visibly a heading. */
  unreadable: string[]
  overrides: {
    banned: string[]
    published: string[]
    outOfCatalogue: string[]
  }
}

/** A scope and a definition of complete, as the report endpoints take them. */
export interface CoverageRequest {
  profile?: string
  requirements?: { attribute: string; markets: string[] }[]
  label?: string
  modelColors?: string[]
  season?: string[]
  subCategory?: string[]
  gender?: string[]
  inCatalogue?: string
  /** `''`, `'any'`, or a market code. The server rejects anything else rather
   *  than widening the filter silently. */
  priced?: string
  /** `''` or `'any'`. A market code is refused: one stock pool, four markets. */
  stocked?: string
  /** Issue keys, OR-ed together. */
  flags?: string[]
  /** Attributes whose *values* go on the Values sheet. Same shape as
   *  `requirements`, different question: that one asks whether a value must be
   *  present, this one asks to be shown what it says. */
  values?: { attribute: string; markets: string[] }[]
  /** One row per SKU rather than per option. Its own field rather than a key
   *  inside `values`, because it changes the shape of the sheet rather than
   *  adding a column — and because `base_code_format` is a real attribute
   *  ("Article (MCS)"), so using it as a sentinel collided with ticking it. */
  bySize?: boolean
}


// ── Product detail ───────────────────────────────────────────────────────

/** The four markets liveness is tracked for. Content also exists for UA, but
 *  it isn't part of the liveness table — see Rules/Liveness in the vault. */
export const MARKETS = ['PL', 'CZ', 'SK', 'HU'] as const
export type Market = (typeof MARKETS)[number]

export const MARKET_NAMES: Record<Market, string> = {
  PL: 'Poland',
  CZ: 'Czechia',
  SK: 'Slovakia',
  HU: 'Hungary',
}

/**
 * Where an attribute value sits in its life.
 *
 * `missing → proposed → approved → exported → live`, with `rejected` and
 * `stale` as side exits and `na` meaning the attribute doesn't apply to this
 * product at all. `na` is not a gap and must never be counted as one — a
 * wallet has no sleeve type.
 */
export type AttributeState =
  | 'missing'
  | 'proposed'
  | 'approved'
  | 'exported'
  | 'live'
  | 'rejected'
  | 'stale'
  | 'na'

/** Who may write the attribute. `partner` is the Turkey team, who receive it
 *  on a product-card file; we can request a change but not make one. */
export type AttributeOwner = 'us' | 'partner' | 'system'

/** A value can be present and still be wrong. These are the defect classes we
 *  can detect mechanically; they annotate a cell rather than replacing its
 *  lifecycle state, because "live but in the wrong language" is both. */
/** `T?` — Omnitron holds no translation for this term in one or more of
 *  our markets, so it falls back to the Turkish label. The storefront
 *  falls back the same way, which makes it a customer-facing defect
 *  rather than a panel one. */
export type SuspectFlag = 'EN?' | 'T?' | 'LIST' | 'RANGE' | 'ZERO' | 'DIVERGED'

export interface AttributeCell {
  state: AttributeState
  value?: string
  /** Render in mono — codes, prices, enum values. */
  code?: boolean
  suspect?: SuspectFlag
  source?: string
  sourceDetail?: string
  changedAt?: string
  changedBy?: string
  proposedValue?: string
  proposedAt?: string
  /** Why a replacement was proposed — which language the live value reads as,
   *  and which source found the candidate. Shown under the suggestion. */
  proposedWhy?: string
  /** Why the value is suspect, in a sentence, for the inspector. */
  suspectReason?: string
  /** The option's sizes hold different values here, so there is no single
   *  value to show. The article code is the clearest case — it contains the
   *  size — but any SKU-grain attribute can land here. The sizes table has
   *  the per-size values. */
  perSize?: boolean
  /** How many sizes this option has, for the cell to say so. */
  sizeCount?: number
  /** Whether this cell may be set from the panel — the registry's answer,
   *  carried on the live overlay. Absent when no overlay was loaded, in which
   *  case the inspector falls back to the owner. */
  writable?: boolean
}

/**
 * One allowed value for a vocabulary-backed attribute.
 *
 * Most classification attributes draw from short closed lists — sleeve type
 * has three values, fit ten, colour filter nine. The registry owns those
 * lists, and they grow: when a new value turns up in an export or a
 * masterdata file it lands as a `candidate` and someone promotes it. That's
 * why this is data rather than a TypeScript union.
 *
 * `usageCount` is how many products currently carry the value, which is what
 * separates "the real value everyone uses" from "a typo someone made once".
 */
export interface VocabularyOption {
  value: string
  status: 'approved' | 'candidate'
  usageCount?: number
}

export interface AttributeRow {
  key: string
  label: string
  owner: AttributeOwner
  /** Set when one value covers every market — classification, mostly. */
  shared?: AttributeCell
  /** Set when the attribute exists once per market. */
  perMarket?: Partial<Record<Market, AttributeCell>>
  /** Present when the attribute is vocabulary-backed. Absent means free text,
   *  which is the case for names and descriptions. */
  vocabulary?: VocabularyOption[]
  /** Free-text attributes that hold a paragraph rather than a phrase, so the
   *  inspector gives a textarea instead of a single line. */
  longText?: boolean
}

export interface AttributeGroup {
  title: string
  rows: AttributeRow[]
}

// ── Products browsing ────────────────────────────────────────────────────

/** One card/row in the Products grid. Deliberately flat and small — the grid
 *  renders thousands of these, and anything it doesn't display doesn't belong
 *  here. Full detail comes from the product detail endpoint. */
export interface ProductSummary {
  modelColor: string
  name: string
  imageUrl: string | null
  /** How many images the product has. Images are a gating attribute, so the
   *  count matters, not just presence. */
  imageCount?: number
  season: string
  /** Season as the ERP has it. Reference only — **never** filtered, grouped or
   *  reported on. The PL team's season field is the one the business checks,
   *  so a product with no PL season has no season as far as this module is
   *  concerned, however well populated the ERP field happens to be. Kept
   *  because it helps whoever fills the PL field in. */
  erpSeason?: string
  mainCategory: string
  subCategory: string
  gender: string
  skuCount: number
  /** Live per market — measured from that market's storefront where the
   *  storefront sync has run, and derived from the six rules where it hasn't.
   *  `observed` says which. Liveness is evaluated per SKU and a product page
   *  shows if any size is live, so that's what's summarised here; a listing
   *  page only ever answers at option grain. */
  live: Partial<Record<Market, boolean>>
  /** Why our six rules say a market isn't live.
   *
   *  Deliberately **not** conditional on `live` any more. An option the shop
   *  is selling while the rules call it blocked is the reconciliation queue,
   *  and hiding it is what let Poland and Czechia sit ~600 and ~500 options
   *  adrift without anything on the page saying so. */
  blockedBy: Partial<Record<Market, string>>
  /** Which markets' `live` was measured rather than computed. Absent on
   *  extracts built before the storefront stage existed. */
  observed?: Partial<Record<Market, boolean>>
  /** Attribute labels with no value at all. Drives the "missing" facet. */
  missing: string[]
  /** Newest omnitron_modified_date across the ModelColor's SKUs. */
  lastChangedAt?: string | null
  lastChangedVia?: string | null
  /** Registry attributes that gate go-live and are absent, each tagged
   *  with who can fill it. Absent on extracts built before 0.9.1. */
  gaps?: { scope: string; label: string; owner: AttributeOwner }[]
  /** Present-but-wrong. Kept apart from `missing` on purpose: one is work not
   *  started, the other is work done badly, and they go to different people. */
  defects: string[]
}

export type FacetKey =
  | 'season'
  | 'mainCategory'
  | 'subCategory'
  | 'gender'
  | 'readiness'
  | 'missing'
  | 'defect'

export interface FacetValue {
  value: string
  count: number
}

export interface Facet {
  key: FacetKey
  label: string
  values: FacetValue[]
}

export interface MarketLiveness {
  market: Market
  live: boolean
  /** Which of the six conditions failed. Empty when live. */
  reason: string
}

export interface SizeRow {
  label: string
  /** Omnitron's own key for this size — what a push writes against. */
  sku?: string
  /** The size's barcode. Size-level identity that no other view carries. */
  ean?: string
  stock: Partial<Record<Market, number>>
  price: Partial<Record<Market, string>>
}

export interface BlockerGroup {
  /** `warning`: a rule says blocked while the shop is selling it anyway —
   *  worth a look, but nothing is stopping the product going live. */
  owner: AttributeOwner | 'nobody' | 'warning'
  label: string
  items: Array<{ scope: string; text: string }>
}

export interface ProductImage {
  position: number
  url: string | null
  state: 'live' | 'proposed'
}

/** One recorded change to one value. `sku` is null when the value belongs to
 *  the option rather than to a size — "the price changed" and "the price
 *  changed on one size" are different events. */
export interface ValueChange {
  changedAt: string | null
  attribute: string
  label: string
  market: string
  entityKind: string
  sku: string | null
  oldValue: string | null
  newValue: string | null
  truncated: boolean
  syncRunId: number | null
  changedBy: string | null
}

/** One cell of one option as the database holds it right now — the live
 *  overlay the product page lays over the nightly extract, so a value set this
 *  afternoon does not vanish until tonight's rebuild. */
export interface OverlayRow {
  attribute: string
  /** PL|CZ|SK|HU, or `*` for a market-agnostic value. */
  market: string
  value: string | null
  state: AttributeState
  sourceKind: string
  sourceRef: string | null
  updatedAt: string | null
  updatedBy: string | null
  suspect: string | null
  suspectReason: string | null
}

/** One waiting suggestion in the Imports list, with the text it replaces. */
export interface WaitingProposal {
  modelColor: string
  attribute: string
  label: string
  market: string
  /** What the shop holds now. Null when the field was empty rather than wrong. */
  current: string | null
  proposed: string
  why: string | null
  flag: string | null
  source: string | null
  proposedAt: string | null
}

export interface ProposalsWaiting {
  rows: WaitingProposal[]
  /** Matching the current filters — what you are looking at. */
  total: number
  shown: number
  /** These describe *everything* waiting, so choosing a market does not
   *  collapse the breakdown you would use to choose the next one. */
  byMarket: Record<string, number>
  byAttribute: Record<string, number>
  byFlag: Record<string, number>
}

/** A suggested replacement for a value that reads as the wrong language. */
export interface ProposalRow {
  attribute: string
  market: string
  value: string
  /** Why it was proposed, in a sentence — shown under the suggestion. */
  why: string | null
  /** `EN?` or `TR?` — what the live value reads as. */
  flag: string | null
  /** Which source found it, e.g. `sibling`. */
  source: string | null
  proposedAt: string | null
}

export interface ValueOverlay {
  modelColor: string
  rows: OverlayRow[]
  /** Waiting suggestions, in their own lane beside the live values. */
  proposals: ProposalRow[]
  /** The (attribute, market) pairs the registry lets a person set. */
  writable: { attribute: string; market: string }[]
  /** Whether a push to Omnitron is switched on at all. */
  writesEnabled: boolean
}

export interface SetValueResult {
  ok: boolean
  written: number
  unchanged: number
  row: OverlayRow
}

// ── Pushes to Omnitron ───────────────────────────────────────────────────

/** One option value waiting to go out, or gone out and not yet confirmed. */
export interface PendingValueRow {
  modelColor: string
  attribute: string
  label: string
  market: string
  value: string | null
  state: AttributeState
  updatedBy: string | null
  updatedAt: string | null
  /** How many sellable sizes the write fans out to. */
  sizes: number
}

export type PushBatchStatus = 'pending' | 'pushing' | 'done' | 'partial' | 'failed' | 'halted'

export interface PushBatchRow {
  id: number
  createdAt: string | null
  createdBy: string | null
  source: string
  status: PushBatchStatus
  note: string | null
  itemCount: number
  productCount: number
  skuCount: number
  writtenCount: number
  confirmedCount: number
  /** Items the market's own shop has been seen serving.
   *
   *  `confirmedCount` is Omnitron agreeing seconds after the write. This is a
   *  customer being able to read it, which on the first real push was two
   *  hours later. It keeps rising after the batch says `done`. */
  publishedCount: number
  /** Of the rest, how many the shop check can never reach.
   *
   *  It confirms by crawling each market's listing feed. An option that has
   *  left that feed will never match, however many passes run, so counting it
   *  as "waiting" makes a landed push look like a stuck one. */
  unlistedCount: number
  failedCount: number
  skippedCount: number
  syncRunId: number | null
  startedAt: string | null
  finishedAt: string | null
  errorSummary: string | null
}

export interface PushItemRow {
  sku: string
  modelColor: string
  attribute: string
  market: string
  locale: string | null
  key: string
  oldUpstream: string | null
  newValue: string | null
  status: 'pending' | 'written' | 'confirmed' | 'failed' | 'skipped'
  reason: string | null
  writtenAt: string | null
  confirmedAt: string | null
  /** When the shop was first seen serving it. Null means not yet, never failed. */
  publishedAt: string | null
  /** The option is out of its market's listing feed, so `publishedAt` will
   *  stay null however long it is left. Not a failure — the value is in
   *  Omnitron and the product page may be showing it; the one source that
   *  can confirm it simply does not carry the option any more. */
  unlisted: boolean
}

export interface PushBatchDetail extends PushBatchRow {
  items: PushItemRow[]
}

/** One proposed cell — from a bulk edit or an uploaded sheet. */
export interface ValueEdit {
  modelColor: string
  attribute: string
  market: string
  value: string
}

export type EditFlag =
  | 'not_writable'
  | 'unknown_attribute'
  | 'unknown_product'
  | 'unchanged'
  | 'empty'
  | 'language_suspect'
  | 'vocabulary_miss'
  | 'sizes_disagree'

/** What one edit would do, and what is wrong with it. `applicable` is the
 *  server's verdict; a row it marks false is not written whatever is ticked. */
export interface EditPreviewRow {
  modelColor: string
  attribute: string
  label: string
  market: string
  current: string | null
  currentState: AttributeState | null
  value: string
  flags: EditFlag[]
  applicable: boolean
}

export interface EditPreview {
  rows: EditPreviewRow[]
  counts: Record<string, number>
  total: number
  /** Present when the preview came from an uploaded file. */
  file?: {
    name: string
    sheet: string
    headerRow: number
    rowsRead: number
    columns: string[]
    unknownColumns: string[]
  }
}

export interface ImportTarget {
  key: string
  label: string
  perMarket: boolean
  longText: boolean
  markets: string[]
}

export interface ImportsPending {
  approved: PendingValueRow[]
  approvedCount: number
  itemCount: number
  productCount: number
  skuCount: number
  requestEstimate: number
  maxItems: number
  exported: PendingValueRow[]
  openBatch: PushBatchRow | null
  canPush: boolean
  /** Why a push cannot start, in a sentence, when `canPush` is false. */
  reason: string
  writesEnabled: boolean
  writeMode: string
}

export interface ProductHistory {
  modelColor: string
  changes: ValueChange[]
  /** Oldest change anywhere, so the page can distinguish "nothing changed"
   *  from "we only started recording on Monday". Null before the first one. */
  recordingSince: string | null
  more: boolean
  skusCovered: number
}

export interface ProductDetail {
  modelColor: string
  name: string
  baseCode: string
  colorCode: string
  colorName: string
  season: string
  gender: string
  category: string
  subCategory: string
  sizeCount: number
  lastChangedAt: string
  lastChangedVia: string
  markets: MarketLiveness[]
  groups: AttributeGroup[]
  sizes: SizeRow[]
  blockers: BlockerGroup[]
  images: ProductImage[]
}

// ── Endpoints ────────────────────────────────────────────────────────────

// ── Taking a product offline ──────────────────────────────────────────────

/** A set of markets that can only be taken offline together.
 *
 *  Since 2026-09-16 there is one, `PL+CZ+HU+SK`: all four markets are served
 *  by catalogue 601, so removing a product there removes it from every
 *  storefront, while deleting it from one market's own catalogue usually
 *  leaves it on sale.
 *  The groups come from the server so
 *  the buttons follow `scope.py` rather than a copy of it kept here. */
export interface TakedownGroup {
  key: string
  markets: Market[]
  label: string
  /** More than one market. The confirmation says so in words. */
  shared: boolean
  catalogs: number[]
  /** Only the catalogues more than one of these markets reads — the reason
   *  they are stuck together. Not the same as `catalogs`: every market has
   *  its own as well, and saying they share all of them would be wrong. */
  sharedCatalogs: number[]
  names: string[]
}

/** What a takedown would delete, computed against Omnitron before anything
 *  is shown to the person about to press the button. */
export interface TakedownPlan {
  modelColor: string
  group: string
  markets: Market[]
  shared: boolean
  catalogs: number[]
  catalogsHit: number[]
  /** Catalogue rows — one per size per catalogue holding it. */
  rowCount: number
  sizeCount: number
  /** Sizes we hold that Omnitron has never given us an id for. They cannot be
   *  removed and would stay on sale, so they are named rather than counted. */
  unresolvedSkus: string[]
  isEmpty: boolean
  /** Both server switches are on. False means the button explains instead. */
  enabled: boolean
  confirmPhrase: string
}

export interface TakedownRow {
  id: number
  modelColor: string
  markets: Market[]
  catalogs: number[]
  /** planned | removing | done | partial | failed | restored. `partial` is the
   *  one that needs a person: some sizes are off sale and some are not. */
  status: string
  reason: string | null
  createdAt: string | null
  createdBy: string | null
  finishedAt: string | null
  restoredAt: string | null
  restoredBy: string | null
  plannedCount: number
  removedCount: number
  failedCount: number
  errorSummary: string | null
  canRestore: boolean
  sizes: string[]
  failures: { sku: string; catalog: number; error: string | null }[]
}

export const api = {
  // ── Taking a product offline (developer-only) ───────────────────────────
  //
  // There is no per-market product flag upstream: `is_active` would remove
  // the product from every channel including Turkey's. Availability is a
  // catalogue question, so this deletes catalogue rows — and puts them back.

  takedownGroups: () =>
    request<{ groups: TakedownGroup[]; enabled: boolean; confirmPhrase: string }>(
      '/api/takedown/groups',
    ),

  /** What removing this option from this group would actually delete. Read
   *  live from Omnitron, so it is the dialog's content and not a guess. */
  takedownPlan: (modelColor: string, group: string) =>
    request<TakedownPlan>(
      `/api/takedown/${encodeURIComponent(modelColor)}/plan?group=${encodeURIComponent(group)}`,
    ),

  takeOffline: (
    modelColor: string,
    body: { group: string; confirm: string; reason?: string },
  ) =>
    request<TakedownRow>(`/api/takedown/${encodeURIComponent(modelColor)}/offline`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  putBackOnline: (takedownId: number) =>
    request<TakedownRow>(`/api/takedown/restore/${takedownId}`, { method: 'POST' }),

  takedownHistory: (modelColor: string) =>
    request<{ takedowns: TakedownRow[] }>(
      `/api/takedown/${encodeURIComponent(modelColor)}/history`,
    ),

  // ── Auth ────────────────────────────────────────────────────────────────

  me: () => request<{ user: AuthUser }>('/api/auth/me'),

  login: (email: string, password: string) =>
    request<{ user: AuthUser }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  logout: () =>
    request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),

  // ── User management (developer-only) ────────────────────────────────────

  listUsers: () => request<AdminUser[]>('/api/users'),

  /** What each tier reaches, for the page to explain the choice where it is
   *  made rather than in a comment nobody reading the form will see. */
  roles: () => request<{ roles: RoleInfo[] }>('/api/users/roles'),

  createUser: (body: { email: string; name?: string; role: Role; password: string }) =>
    request<AdminUser>('/api/users', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** Absent means "leave it alone"; `name: null` clears it. The two have to
   *  stay distinguishable or patching one field wipes the others. */
  updateUser: (
    userId: number,
    patch: {
      email?: string
      name?: string | null
      role?: Role
      is_active?: boolean
    },
  ) =>
    request<AdminUser>(`/api/users/${userId}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  resetUserPassword: (userId: number, password: string) =>
    request<{ ok: boolean }>(`/api/users/${userId}/password`, {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  // No `deleteUser`. There is no endpoint behind it — see `AdminUser`.

  // ── Versioning + changelog ────────────────────────────────────────────────

  version: () => request<{ version: string }>('/api/version'),

  productHistory: (modelColor: string, limit = 200) =>
    request<ProductHistory>(
      `/api/products/${encodeURIComponent(modelColor)}/history?limit=${limit}`,
    ),

  /** The option's cells as the database holds them now, plus which may be
   *  edited. Laid over the extract by `loadProductDetail`. */
  productValues: (modelColor: string) =>
    request<ValueOverlay>(`/api/products/${encodeURIComponent(modelColor)}/values`),

  /** Set one cell of one option. Lands as `approved`: the sync will not
   *  overwrite it and the next push carries it to Omnitron. */
  setValue: (body: { modelColor: string; attribute: string; market: string; value: string }) =>
    request<SetValueResult>('/api/imports/values', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** The sibling panels this account also reaches. Server-side config, so a
   *  new address needs no rebuild. */
  modules: () => request<ModuleList>('/api/modules'),

  /** ModelColors whose own SKUs disagreed, left unwritten by the importer. */
  conflicts: (limit = 100, offset = 0) =>
    request<ConflictList>(`/api/conflicts?limit=${limit}&offset=${offset}`),

  /** Accept one candidate as the answer. Writes it `approved`, which the
   *  sync will not overwrite, and clears the queue row. */
  decideConflict: (body: {
    modelColor: string
    attribute: string
    market: string
    value: string
  }) =>
    request<{ ok: boolean }>('/api/conflicts/decide', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** Ingest history, and whether what the panel shows is actually current. */
  syncRuns: (limit = 50) =>
    request<SyncRunsPayload>(`/api/sync/runs?limit=${limit}`),

  /** Products whose price or stock Omnitron no longer has — what went dark. */
  retirements: (limit = 200) =>
    request<RetirementsPayload>(`/api/sync/retirements?limit=${limit}`),

  // ── Pushes to Omnitron ──────────────────────────────────────────────────

  /** What is waiting to go out, what went out unconfirmed, and whether a
   *  push can start from this server. */
  importsPending: () => request<ImportsPending>('/api/imports/pending'),

  /** Start a push. Refused with a sentence (409/422/502) rather than started
   *  when the configuration, the selection or the network says no.
   *
   *  `values` omitted means the whole queue. `values: []` means nothing was
   *  ticked and pushes nothing — the two are deliberately not the same. */
  importsPush: (
    body: {
      modelColors?: string[]
      values?: { modelColor: string; attribute: string; market: string }[]
      note?: string
    } = {},
  ) =>
    request<{ started: boolean; batchId: number; items: number; skus: number; products: number }>(
      '/api/imports/push',
      { method: 'POST', body: JSON.stringify(body) },
    ),

  importsBatches: (limit = 20) =>
    request<{ rows: PushBatchRow[] }>(`/api/imports/batches?limit=${limit}`),

  importsBatch: (batchId: number) =>
    request<PushBatchDetail>(`/api/imports/batches/${batchId}`),

  /** Take one approved value out of the queue. It is deleted, not flagged, and
   *  the cell reads as missing until the next sync restores what Omnitron
   *  holds. Only an approved value can go this way. */
  /** Every waiting suggestion, newest first, with the value it would replace. */
  importsProposals: (
    opts: { limit?: number; market?: string; attribute?: string } = {},
  ) => {
    const q = new URLSearchParams()
    if (opts.limit) q.set('limit', String(opts.limit))
    if (opts.market) q.set('market', opts.market)
    if (opts.attribute) q.set('attribute', opts.attribute)
    const qs = q.toString()
    return request<ProposalsWaiting>(`/api/imports/proposals${qs ? `?${qs}` : ''}`)
  },

  /** Take a proposed translation into the queue as your own approved value.
   *  Omit `value` to accept it as proposed; send one to edit it first. */
  importsAcceptProposal: (body: {
    modelColor: string
    attribute: string
    market: string
    value?: string
  }) =>
    request<{ ok: boolean; value: string; edited: boolean }>(
      '/api/imports/proposals/accept',
      { method: 'POST', body: JSON.stringify(body) },
    ),

  /** Drop a proposal. The live value is untouched and nothing is recorded
   *  against the product, because nothing about it changed. */
  importsRejectProposal: (body: { modelColor: string; attribute: string; market: string }) =>
    request<{ ok: boolean; rejected: string | null }>('/api/imports/proposals/reject', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  importsDiscard: (body: { modelColor: string; attribute: string; market: string }) =>
    request<{ ok: boolean; discarded: string | null }>('/api/imports/discard', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** The attributes and markets a person may set from the grid. */
  importsTargets: () => request<{ attributes: ImportTarget[] }>('/api/imports/targets'),

  /** What a set of edits would change. Writes nothing. */
  importsPreview: (edits: ValueEdit[]) =>
    request<EditPreview>('/api/imports/preview', {
      method: 'POST',
      body: JSON.stringify({ edits }),
    }),

  /** Write the applicable edits as approved. The server re-checks every row. */
  importsApply: (edits: ValueEdit[]) =>
    request<{ ok: boolean; applied: number; unchanged: number; skipped: number }>(
      '/api/imports/apply',
      { method: 'POST', body: JSON.stringify({ edits }) },
    ),

  /** An edited Values / SKU Detail sheet, turned into a preview. Multipart,
   *  so it bypasses `request`, which forces a JSON content type. */
  importsUpload: async (file: File): Promise<EditPreview> => {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch('/api/imports/upload', {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    })
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`
      try {
        const body = (await res.json()) as { detail?: string }
        if (body?.detail) message = body.detail
      } catch {
        /* keep the status line */
      }
      throw new ApiError(message, res.status)
    }
    return (await res.json()) as EditPreview
  },

  // ── Images ──────────────────────────────────────────────────────────────

  imagesStatus: () => request<ImagesStatus>('/api/images/status'),

  imagesPublished: (limit = 50) =>
    request<{ total: number; rows: PublishedImageRow[] }>(
      `/api/images/published?limit=${limit}`,
    ),

  imagesAbsent: (limit = 50) =>
    request<{ total: number; rows: AbsentRow[] }>(`/api/images/absent?limit=${limit}`),

  imagesBundles: (limit = 20) =>
    request<{ rows: BundleRow[] }>(`/api/images/bundles?limit=${limit}`),

  /** Starts a run and returns — it reads ~1,200 pages and moves a gigabyte,
   *  so the browser watches `lastRun` rather than waiting on the response. */
  imagesPublish: (dryRun = false) =>
    request<{ started: boolean; options: number; dryRun: boolean }>(
      `/api/images/publish?dry_run=${dryRun}`,
      { method: 'POST' },
    ),

  /** Publish photos for a supplied list of ModelColors.
   *
   *  Multipart for the same reason `resolveFile` is: `request` sets a JSON
   *  content type, and a file needs the browser to write its own boundary.
   *  Takes a File or a pasted block, since the two reach the same endpoint. */
  imagesPublishList: async (
    input: File | string,
    dryRun = false,
  ): Promise<ImageListPublish> => {
    const form = new FormData()
    if (typeof input === 'string') form.append('text', input)
    else form.append('file', input)
    form.append('dry_run', String(dryRun))
    const res = await fetch('/api/images/publish/upload', {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    })
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`
      try {
        const body = (await res.json()) as { detail?: string }
        if (body?.detail) message = body.detail
      } catch {
        /* keep the status line */
      }
      throw new ApiError(message, res.status)
    }
    return (await res.json()) as ImageListPublish
  },

  imagesMarkSent: (bundleId: number) =>
    request<{ ok: boolean }>(`/api/images/bundles/${bundleId}/sent`, {
      method: 'POST',
    }),

  /** Replace the brand library cookie. Tested against the library before it is
   *  stored — a bad one saved silently becomes a failed run next morning that
   *  nobody connects to this paste. */
  imagesSetCookie: (text: string) =>
    request<{ ok: boolean; message: string }>('/api/images/cookie', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),

  changelog: () => request<ChangelogPayload>('/api/changelog'),

  acknowledgeChangelog: () =>
    request<{ user: AuthUser }>('/api/auth/acknowledge_changelog', { method: 'POST' }),


  // ── Coverage ─────────────────────────────────────────────────────────────

  /** Resolve a pasted block of codes against the options we hold. */
  resolveCodes: (text: string) =>
    request<CodeResolution>('/api/coverage/resolve/text', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),

  /** Resolve an uploaded file. Multipart, so `request` is bypassed — it sets a
   *  JSON content type, and a multipart body needs the browser to write its
   *  own boundary. */
  resolveFile: async (file: File): Promise<CodeResolution> => {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch('/api/coverage/resolve', {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    })
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`
      try {
        const body = (await res.json()) as { detail?: string }
        if (body?.detail) message = body.detail
      } catch {
        /* keep the status line */
      }
      throw new ApiError(message, res.status)
    }
    return (await res.json()) as CodeResolution
  },

  /** Download the workbook for a scope.
   *
   *  Returns the blob and the option count the server put in a header, so the
   *  page can confirm the file describes what was on screen rather than
   *  asserting it. */
  /** Rebuild the generated extract from the database, on the server.
   *
   * Exists because the extract and the running image are two artefacts that
   * can disagree: a release that adds a file to the extract ships a page
   * reading something the extract does not contain, and until this button the
   * only cure was a shell on the box and a wait until the 03:00 cron.
   *
   * Minutes, not seconds — the caller should show that it is working.
   */
  rebuildExtract: () =>
    request<{ ok: boolean; files: Record<string, number>; seconds: number }>(
      '/api/coverage/rebuild',
      { method: 'POST' },
    ),

  coverageReport: async (
    body: CoverageRequest,
  ): Promise<{
    blob: Blob
    filename: string
    options: number
    notFound: number
    valuesReadAt: string
    valuesFilled: number
  }> => {
    const res = await fetch('/api/coverage/report', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`
      try {
        const parsed = (await res.json()) as { detail?: string }
        if (parsed?.detail) message = parsed.detail
      } catch {
        /* keep the status line */
      }
      throw new ApiError(message, res.status)
    }
    const disposition = res.headers.get('content-disposition') ?? ''
    const match = /filename="?([^"]+)"?/.exec(disposition)
    return {
      blob: await res.blob(),
      filename: match?.[1] ?? 'coverage.xlsx',
      options: Number(res.headers.get('x-coverage-options') ?? 0),
      notFound: Number(res.headers.get('x-coverage-not-found') ?? 0),
      // Absent unless values were asked for, which is how the caller tells a
      // report with a Values sheet from one without.
      valuesReadAt: res.headers.get('x-coverage-values-read-at') ?? '',
      valuesFilled: Number(res.headers.get('x-coverage-values-filled') ?? 0),
    }
  },


  // ── Audit / activity log (developer-only) ─────────────────────────────────

  audit: (params: {
    user_email?: string
    action?: string
    /** 0 — the default — means the whole record. This log is young and a
     *  default window would hide most of it. */
    days?: number
    limit?: number
    offset?: number
  } = {}) => {
    const qs = new URLSearchParams()
    if (params.user_email) qs.set('user_email', params.user_email)
    if (params.action) qs.set('action', params.action)
    if (params.days) qs.set('days', String(params.days))
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    const qstr = qs.toString()
    return request<AuditList>(`/api/audit${qstr ? `?${qstr}` : ''}`)
  },
}

/* ── Images ─────────────────────────────────────────────────────────────── */

/** The brand library session. There is no password flow and no API token on
 *  this instance — a browser cookie is the whole of authentication, and it
 *  expires within hours, which is why the page can replace it. */
export interface ImagesCookie {
  rotatedAt: string | null
  rotatedBy: string | null
  /** When the library last actually accepted it. Diverges from `rotatedAt`
   *  within the day, and is the question people actually have. */
  lastOkAt: string | null
  /** Came from the server's .env rather than from this page — nobody has
   *  rotated it here, so it is as old as the deploy. */
  fromEnv: boolean
}

/** Which halves of the setup are present. They fail separately and have
 *  different remedies, so they are reported separately: without a cookie
 *  nothing can be found, and without a public base URL files can be uploaded
 *  that nobody can reference. */
export interface ImagesConfigured {
  library: boolean
  photoServer: boolean
  ready: boolean
  /** `oauth` means the run signs itself in and nobody touches it weekly;
   *  `cookie` means somebody pastes one. The page reads very differently in
   *  the two cases, which is why this is a field and not inferred. */
  mode: 'oauth' | 'cookie' | null
  cookie: ImagesCookie | null
}

export interface ImagesRun {
  id: number
  status: RunStatus
  startedAt: string | null
  finishedAt: string | null
  assetsRead: number
  published: number
  failed: number
  error: string | null
  triggeredBy: string | null
}

export interface ImagesStatus {
  configured: ImagesConfigured
  /** When Omnitron's image column was last read. Null means never, and then
   *  `blocked` explains why no count is given. */
  imageSourceAt: string | null
  /** Set when no count can honestly be produced. The page renders a different
   *  screen for this rather than showing zeros. */
  blocked: string | null
  counts: {
    missing: number | null
    banned: number | null
    published: number | null
    unclassified: number | null
    targets: number | null
  }
  absent: number
  awaitingBundle: number
  /** Options with a published photo not yet seen in Omnitron — what is
   *  genuinely sitting with Turkey. Not `counts.published`, which counts
   *  options this run skipped because they were already done. */
  withTurkey: number
  photosPublished: number
  lastRun: ImagesRun | null
  running: boolean
}

export interface PublishedImageRow {
  modelColor: string
  asset: string
  edition: string
  file: string
  url: string
  bytes: number
  publishedAt: string | null
  bundleId: number | null
  /** Set when a later Omnitron sync proved Turkey uploaded it. */
  landedAt: string | null
}

export interface AbsentRow {
  modelColor: string
  attempts: number
  firstMissedAt: string | null
  lastCheckedAt: string | null
  /** Every spelling the library was searched under. More than one means a
   *  footwear code, which we hold with a leading `7` and Bynder holds without.
   *  Shown so a reader can tell "not there" from "looked under the wrong
   *  name" — a wrong alias rule otherwise reads as a photo nobody ever took. */
  searchedAs: string[]
}

export interface BundleRow {
  id: number
  builtAt: string | null
  builtBy: string | null
  options: number
  files: number
  bytes: number
  status: string
  sentAt: string | null
  hasZip: boolean
  hasXlsx: boolean
}
