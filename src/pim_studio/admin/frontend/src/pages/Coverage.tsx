import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { AlertTriangle, Download, Info, Search } from 'lucide-react'
import { cn } from '../lib/cn'
import { api, MARKETS } from '../lib/api'
import type { CodeResolution } from '../lib/api'
import RebuildExtract from '../components/RebuildExtract'
import CoverageUpload from '../components/CoverageUpload'
import RequirementPicker from '../components/RequirementPicker'
import ExportPicker, {
  MAX_EXPORT_CELLS,
  MAX_SIZE_EXPORT_CELLS,
} from '../components/ExportPicker'
import { OwnerTag, columnsIn } from '../components/AttributeMarketGrid'
import {
  applyScope,
  computeCoverage,
  decodeRequirements,
  emptyScope,
  encodeRequirements,
  facetCounts,
  scopeIsEmpty,
  PRESENCE_ANY,
} from '../lib/coverage'
import type {
  AttributeMarkets,
  CoverageExtract,
  CoverageScope,
  ProfileSpec,
  RequirementSpec,
  Standing,
} from '../lib/coverage'
import { loadCoverage } from '../lib/demoData'
import { useApi } from '../lib/useApi'

/**
 * Coverage — what is wrong, with how many options, and whose job it is.
 *
 * The page answers three questions in that order, and the order is the design:
 *
 * 1. **How many options fall short**, against a definition of complete the
 *    user chose rather than one baked in here.
 * 2. **On what** — one row per required attribute and market, worst first.
 * 3. **Who fixes it** — every gap routed by who may write the attribute, which
 *    turns a count of problems into a count of import files and requests.
 *
 * ## Three standings, and why the third exists
 *
 * `complete` and `incomplete` are the obvious two. `indeterminate` is an
 * option with nothing missing and at least one requirement we have never read
 * a source for. It is not complete — claiming a pass on a column nobody has
 * loaded is exactly the confident-and-wrong number the README says this
 * codebase has produced four times — and it is not a gap either, because
 * nobody can act on it.
 *
 * Every percentage on this page divides by `judged`, which is complete plus
 * incomplete. The indeterminate count sits beside it rather than inside it.
 *
 * ## Where the numbers come from
 *
 * `coverage.json`, computed in the browser so a facet click costs nothing. The
 * download button posts the same scope to the server, which reads the *same
 * extract* and builds the workbook — so the file and the screen cannot drift.
 * That is the whole reason the report endpoint does not re-query the database.
 */
export default function Coverage() {
  const [params, setParams] = useSearchParams()
  const { data, loading } = useApi(loadCoverage, [])
  const extract = data ?? null

  const [scope, setScope] = useState<CoverageScope>(emptyScope())
  const [resolution, setResolution] = useState<CodeResolution | null>(null)

  // Every code the user gave us, matched or not, in the spelling they wrote.
  // The download posts this rather than the resolved list: `apply_scope` then
  // reports the unmatched ones itself, so the workbook's Not-found sheet and
  // the banner above come from one implementation instead of two. Posting only
  // the matches lost them entirely — the file said 120 options and never
  // mentioned the three it could not find.
  const uploadedCodes = useMemo(
    () => (resolution ? [...resolution.modelColors, ...resolution.notFound] : []),
    [resolution],
  )
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState('')
  const [draft, setDraft] = useState('')

  // The requirement set lives in the URL, which is what makes an analysis
  // shareable: "the thing I am looking at" becomes a link rather than a
  // screenshot, and the back button walks through definitions you tried.
  const presetKey = params.get('profile') ?? ''
  const encoded = params.get('req') ?? ''

  const presets: ProfileSpec[] = extract?.profiles ?? []
  const requirements: RequirementSpec[] = useMemo(() => {
    if (encoded) return decodeRequirements(encoded)
    const preset = presets.find((p) => p.key === presetKey) ?? presets[0]
    return preset?.requirements ?? []
  }, [encoded, presetKey, presets])

  const activePreset = encoded ? '' : (presetKey || presets[0]?.key || '')

  // Which attributes' values ride along on their own sheet. Same grammar and
  // the same codec as `req`, so an export is as shareable as an analysis. The
  // one asymmetry: `req` falls back to a preset when absent, this has no
  // fallback — nothing ticked means no Values sheet, which is the right
  // default for a download most people want without one.
  const exported: AttributeMarkets[] = useMemo(
    () => decodeRequirements(params.get('vals') ?? ''),
    [params],
  )

  function setExported(next: AttributeMarkets[]) {
    const query = new URLSearchParams(params)
    if (next.length) query.set('vals', encodeRequirements(next))
    else query.delete('vals')
    setParams(query, { replace: true })
  }

  // Its own parameter rather than an entry in `vals`. It was briefly the
  // latter, keyed on `base_code_format` — which is a real attribute, listed in
  // the grid as "Article (MCS)", so ticking that row switched the grain and
  // swallowed the column. A request for a different shape of sheet is not an
  // attribute selection and must not share a namespace with one.
  const exportBySize = params.get('grain') === 'size'

  function setExportBySize(next: boolean) {
    const query = new URLSearchParams(params)
    if (next) query.set('grain', 'size')
    else query.delete('grain')
    setParams(query, { replace: true })
  }

  function setRequirements(next: RequirementSpec[]) {
    const query = new URLSearchParams(params)
    query.delete('profile')
    if (next.length) query.set('req', encodeRequirements(next))
    else query.delete('req')
    setParams(query, { replace: true })
  }

  function choosePreset(preset: ProfileSpec) {
    const query = new URLSearchParams(params)
    query.delete('req')
    query.set('profile', preset.key)
    setParams(query, { replace: true })
  }

  useEffect(() => {
    if (draft === scope.search) return
    const id = setTimeout(() => setScope((s) => ({ ...s, search: draft })), 250)
    return () => clearTimeout(id)
  }, [draft])

  const products = extract?.products ?? []
  const inScope = useMemo(() => applyScope(products, scope), [products, scope])
  const result = useMemo(
    () =>
      extract
        ? computeCoverage(inScope, requirements, extract)
        : null,
    [inScope, requirements, extract],
  )

  // What the *server* will report on, which is not `shown.length`. Issue
  // flags, price and stock now travel with the request; Standing and the
  // search box still do not, so a scope narrowed by either is wider on the
  // server than on screen. The export cap has to be predicted against the
  // number the server will see or the button looks fine and the request comes
  // back 413.
  //
  // Deliberately written out rather than hidden in a helper: when the dropped
  // filters are wired up this collapses to `shown.length`, and that is the
  // marker for having fixed it.
  //
  // Standing may never collapse into it — it is computed from the requirement
  // set after evaluation, so sending it would mean posting a population
  // defined by the output of the thing being reported on.
  const serverRows = useMemo(
    () => applyScope(products, { ...scope, standing: [], search: '' }),
    [products, scope],
  )
  const serverPopulation = serverRows.length
  // The same options counted as SKUs. A size-grain export is one row per SKU,
  // and the extract already carries the count per option, so the cap can be
  // predicted exactly rather than guessed at six-per.
  const serverSkus = useMemo(
    () => serverRows.reduce((total, p) => total + p.skus, 0),
    [serverRows],
  )
  const droppedFilters = scope.standing.length > 0 || scope.search.trim() !== ''

  const exportColumns = useMemo(
    () => columnsIn(exported, extract?.attributes ?? []),
    [exported, extract],
  )
  const exportCells =
    exportColumns * (exportBySize ? serverSkus : serverPopulation)
  const overExportCap =
    exportCells > (exportBySize ? MAX_SIZE_EXPORT_CELLS : MAX_EXPORT_CELLS)

  // Standing is filtered after evaluation, never inside `applyScope`: it
  // depends on the requirement set, and folding it in would make the
  // requirement table describe a population chosen by its own output.
  const shown = useMemo(() => {
    if (!result || !scope.standing.length) return inScope
    return inScope.filter((p) => {
      const verdict = result.verdicts.get(p.mc)
      return verdict ? scope.standing.includes(verdict.standing) : false
    })
  }, [inScope, result, scope.standing])

  /** Single-select, unlike `toggle`: clicking the active chip clears it, so
   *  every one of these filters can be undone from the chip that set it. */
  function togglePresence(key: 'priced' | 'stocked', value: string) {
    setScope((s) => ({ ...s, [key]: s[key] === value ? '' : value }))
  }

  function toggle(key: 'season' | 'sub' | 'gender' | 'flags', value: string) {
    setScope((s) => {
      const current = s[key]
      return {
        ...s,
        [key]: current.includes(value)
          ? current.filter((v) => v !== value)
          : [...current, value],
      }
    })
  }

  async function download() {
    setDownloading(true)
    setDownloadError('')
    try {
      const { blob, filename } = await api.coverageReport({
        ...(activePreset ? { profile: activePreset } : {}),
        requirements: requirements.map((r) => ({
          attribute: r.attribute,
          markets: r.markets,
        })),
        label: activePreset
          ? presets.find((p) => p.key === activePreset)?.label
          : 'Custom selection',
        modelColors: uploadedCodes.length ? uploadedCodes : scope.modelColors,
        season: scope.season,
        subCategory: scope.sub,
        gender: scope.gender,
        inCatalogue: scope.inCatalogue,
        priced: scope.priced,
        stocked: scope.stocked,
        flags: scope.flags,
        values: exported.map((r) => ({
          attribute: r.attribute,
          markets: r.markets,
        })),
        bySize: exportBySize,
      })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setDownloadError(
        err instanceof Error ? err.message : 'The report could not be built.',
      )
    } finally {
      setDownloading(false)
    }
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-table px-10 py-12">
        <div className="skeleton mb-4 h-8 w-64" />
        <div className="grid grid-cols-4 gap-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skeleton h-[76px]" />
          ))}
        </div>
      </div>
    )
  }

  if (!extract || !extract.products.length) {
    return (
      <div className="mx-auto max-w-content px-10 py-12">
        <div className="eyebrow mb-[7px]">Coverage</div>
        <h1 className="mb-3 font-serif text-[28px] font-medium tracking-tight">
          No coverage extract
        </h1>
        <p className="max-w-prose text-[13px] text-ink-500">
          Nothing has been generated yet. Run{' '}
          <code className="rounded-xs bg-paper-sunk px-1 font-mono text-[12px]">
            python scripts/build_demo_data_from_db.py
          </code>{' '}
          to build it from the mirror, or press the button below to run exactly
          that on the server. This page shows nothing rather than a fixture on
          purpose — a coverage number describing invented products reads
          exactly like one describing the catalogue.
        </p>
        <RebuildExtract />
      </div>
    )
  }

  const generated = extract.generatedAt
    ? new Date(extract.generatedAt).toISOString().slice(0, 16).replace('T', ' ')
    : '—'

  return (
    <div className="mx-auto max-w-table px-10 py-12">
      <div className="mb-4 flex items-end justify-between gap-4">
        <div>
          <div className="eyebrow mb-[7px]">Coverage</div>
          <h1 className="font-serif text-[28px] font-medium tracking-tight">
            {resolution ? 'Your list' : 'Completeness audit'}
          </h1>
          <p className="mt-1 font-mono text-[11px] text-ink-400">
            data as at {generated} UTC · {extract.products.length.toLocaleString('pl-PL')}{' '}
            options in the mirror
          </p>
        </div>
        <div className="flex flex-none flex-col items-end gap-1">
          <button
            type="button"
            onClick={() => void download()}
            disabled={downloading || !requirements.length || overExportCap}
            className="flex items-center gap-1.5 rounded-2xs bg-brand-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-brand-600 disabled:opacity-50"
          >
            <Download size={13} />
            {downloading ? 'Building…' : 'Download report'}
          </button>
          {downloadError && (
            <span className="max-w-[280px] text-right text-[11.5px] text-danger-600">
              {downloadError}
            </span>
          )}
          {!downloadError && exportColumns > 0 && (
            <span
              className={cn(
                'max-w-[280px] text-right text-[11.5px]',
                overExportCap ? 'text-danger-600' : 'text-ink-400',
              )}
            >
              {overExportCap
                ? `${exportCells.toLocaleString('pl-PL')} values is over the ${MAX_EXPORT_CELLS.toLocaleString('pl-PL')} limit — narrow the scope or untick some markets.`
                : `plus ${exportCells.toLocaleString('pl-PL')} values on a 'Values' sheet`}
            </span>
          )}
        </div>
      </div>

      <div className="mb-3">
        <CoverageUpload
          resolution={resolution}
          onResolved={(next) => {
            setResolution(next)
            setScope((s) => ({ ...s, modelColors: next.modelColors }))
          }}
          onClear={() => {
            setResolution(null)
            setScope((s) => ({ ...s, modelColors: [] }))
          }}
        />
      </div>

      <div className="mb-3">
        <RequirementPicker
          attributes={extract.attributes}
          presets={presets}
          requirements={requirements}
          activePreset={activePreset}
          onChange={setRequirements}
          onPreset={choosePreset}
        />
      </div>

      <div className="mb-3">
        <ExportPicker
          attributes={extract.attributes}
          selection={exported}
          population={serverPopulation}
          sizePopulation={serverSkus}
          bySize={exportBySize}
          onBySizeChange={setExportBySize}
          onChange={setExported}
        />
        {droppedFilters && (
          // Not a fix — the download genuinely ignores these three, and that
          // is its own piece of work. Saying so beats letting someone narrow
          // to three hundred options on screen and open a file with eighteen
          // thousand rows in it.
          <p className="mt-1.5 px-1 text-[11.5px] text-warn-600">
            The file will cover {serverPopulation.toLocaleString('pl-PL')}{' '}
            options — the Issue, Standing and search filters narrow this page
            but are not applied to downloads.
          </p>
        )}
      </div>

      {result && requirements.length === 0 && (
        <div className="mb-3 rounded-md border border-warn-600/40 bg-warn-100 px-3 py-2.5 text-[12.5px] text-warn-600">
          Nothing is required yet, so every option counts as complete. Pick a
          preset or tick some attributes — an empty definition gives a number
          that is technically true and means nothing.
        </div>
      )}

      {/* Provenance, not decoration. Stock is in the readiness numbers and
          the ERP is not connected, so the sentence sits above the figures it
          qualifies rather than in a tooltip. */}
      {result && extract.stockCaveat && (
        <div className="mb-3 flex items-start gap-2 rounded-md border border-warn-600/40 bg-warn-100 px-3 py-2.5">
          <AlertTriangle size={14} className="mt-0.5 flex-none text-warn-600" />
          <p className="text-[12.5px] leading-relaxed text-warn-600">
            {extract.stockCaveat}
          </p>
        </div>
      )}

      {/* search + scope line */}
      <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md border hairline bg-paper-card px-3 py-2.5 shadow-card">
        <div className="relative min-w-[260px] flex-1">
          <Search
            size={13}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-400"
          />
          <input
            type="search"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Search by code or name"
            aria-label="Search options"
            className="w-full rounded-2xs border hairline bg-paper py-1 pl-8 pr-2 text-[12.5px] text-ink-900 placeholder:text-ink-400 focus:border-brand-600 focus:outline-none"
          />
        </div>

        <span className="eyebrow">In catalogue</span>
        {MARKETS.map((market) => (
          <ScopeChip
            key={market}
            label={market}
            title={`Only options in the ${market} catalogue`}
            active={scope.inCatalogue === market}
            onClick={() =>
              setScope((s) => ({
                ...s,
                inCatalogue: s.inCatalogue === market ? '' : market,
              }))
            }
          />
        ))}

        {/* Presence, not membership — a deliberately adjacent, different
            question. An option sits in the PL catalogue and still carries no
            PL price; that gap is what the price list has to close, and it is
            invisible if the only market control is the one above. */}
        <span className="eyebrow">Priced</span>
        <ScopeChip
          label="Any"
          title="Priced in at least one market"
          active={scope.priced === PRESENCE_ANY}
          onClick={() => togglePresence('priced', PRESENCE_ANY)}
        />
        {MARKETS.map((market) => (
          <ScopeChip
            key={market}
            label={market}
            title={`Carries a price in ${market}`}
            active={scope.priced === market}
            onClick={() => togglePresence('priced', market)}
          />
        ))}

        {/* No market chips, and that is the point: one pool of stock feeds all
            four, so "in stock in PL" would be a control promising an answer
            the data cannot give. */}
        <ScopeChip
          label="In stock"
          title="Some SKU carries stock above zero — provisional, see the stock note"
          active={scope.stocked === PRESENCE_ANY}
          onClick={() => togglePresence('stocked', PRESENCE_ANY)}
        />

        <span className="ml-auto font-mono text-[11px] tabular-nums text-ink-900">
          {shown.length.toLocaleString('pl-PL')}
        </span>
        <span className="text-[12.5px] text-ink-500">
          {shown.length === 1 ? 'option' : 'options'} in scope
        </span>
        {!scopeIsEmpty(scope) && (
          <button
            type="button"
            onClick={() => {
              setDraft('')
              setResolution(null)
              setScope(emptyScope())
            }}
            className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 hover:text-danger-600"
          >
            clear all
          </button>
        )}
      </div>

      {result && (
        <>
          <StandingRow
            result={result}
            active={scope.standing}
            onToggle={(standing) =>
              setScope((s) => ({
                ...s,
                standing: s.standing.includes(standing)
                  ? s.standing.filter((v) => v !== standing)
                  : [...s.standing, standing],
              }))
            }
          />

          <div className="mt-4 flex items-start gap-6">
            <div className="flex w-[212px] flex-none flex-col gap-4">
              <Facet
                label="Issue"
                values={facetCounts(products, scope, 'flags')}
                selected={scope.flags}
                onToggle={(v) => toggle('flags', v)}
                rename={(key) =>
                  extract.issues.find((i) => i.key === key)?.label ?? key
                }
              />
              <Facet
                label="Season"
                values={facetCounts(products, scope, 'season')}
                selected={scope.season}
                onToggle={(v) => toggle('season', v)}
              />
              <Facet
                label="Sub-category"
                values={facetCounts(products, scope, 'sub')}
                selected={scope.sub}
                onToggle={(v) => toggle('sub', v)}
              />
              <Facet
                label="Gender"
                values={facetCounts(products, scope, 'gender')}
                selected={scope.gender}
                onToggle={(v) => toggle('gender', v)}
              />
            </div>

            <div className="min-w-0 flex-1">
              <RequirementTable result={result} />
              <IssueTable extract={extract} result={result} />
              <OptionList
                products={shown}
                result={result}
                limit={40}
              />
            </div>
          </div>
        </>
      )}
    </div>
  )
}

/** The compact scope-line chip. One component for catalogue, price and stock
 *  so the three read as one row of controls rather than three near-identical
 *  className strings that drift apart on the next restyle. */
function ScopeChip({
  label,
  title,
  active,
  onClick,
}: {
  label: string
  title: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-pressed={active}
      className={cn(
        'rounded-2xs border px-1.5 py-0.5 font-mono text-[11px] tracking-[0.06em]',
        active
          ? 'border-brand-600 bg-brand-50 text-brand-900'
          : 'hairline text-ink-500 hover:border-ink-300',
      )}
    >
      {label}
    </button>
  )
}

const STANDING_LABEL: Record<Standing, string> = {
  complete: 'Complete',
  incomplete: 'Incomplete',
  indeterminate: 'Indeterminate',
}

function StandingRow({
  result,
  active,
  onToggle,
}: {
  result: NonNullable<ReturnType<typeof computeCoverage>>
  active: Standing[]
  onToggle: (standing: Standing) => void
}) {
  const cards: {
    standing: Standing
    value: number
    tone: string
    hint: string
  }[] = [
    {
      standing: 'complete',
      value: result.complete,
      tone: 'text-ok-600',
      hint: 'Every required value present.',
    },
    {
      standing: 'incomplete',
      value: result.incomplete,
      tone: 'text-danger-600',
      hint: 'At least one required value we hold a source for is empty. This is the work.',
    },
    {
      standing: 'indeterminate',
      value: result.indeterminate,
      tone: 'text-ink-500',
      hint:
        'Nothing missing, but at least one requirement has never been read from any source. ' +
        'Not counted as complete, and not counted against it — nobody can act on it.',
    },
  ]

  return (
    <div className="grid grid-cols-4 gap-3">
      {cards.map((card) => {
        const on = active.includes(card.standing)
        return (
          <button
            key={card.standing}
            type="button"
            onClick={() => onToggle(card.standing)}
            title={card.hint}
            className={cn(
              'rounded-md border bg-paper-card px-3.5 py-3 text-left shadow-card transition-colors',
              on ? 'border-brand-600 bg-brand-50' : 'hairline hover:border-ink-300',
            )}
          >
            <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ink-500">
              {STANDING_LABEL[card.standing]}
            </div>
            <div className={cn('font-mono text-[22px] font-semibold tabular-nums', card.tone)}>
              {card.value.toLocaleString('pl-PL')}
            </div>
          </button>
        )
      })}

      <div className="rounded-md border hairline bg-paper-card px-3.5 py-3 shadow-card">
        <div
          className="mb-1 flex items-center gap-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ink-500"
          title="Complete divided by (complete + incomplete). The indeterminate options are in neither half — they have not been judged."
        >
          Complete, of judged
          <Info size={11} className="text-ink-400" />
        </div>
        <div className="font-mono text-[22px] font-semibold tabular-nums text-ink-900">
          {result.completionRate === null
            ? '—'
            : `${(result.completionRate * 100).toFixed(1)}%`}
        </div>
        <div className="mt-0.5 font-mono text-[10px] tabular-nums text-ink-400">
          of {result.judged.toLocaleString('pl-PL')} judged
        </div>
      </div>
    </div>
  )
}

/** One row per required attribute and market — the "what is wrong" table.
 *
 *  `unknown` has its own column and is excluded from the fill rate, so a
 *  column nobody has read shows as "never read" rather than as 0%. Those are
 *  different problems for different people, and only one of them has anybody
 *  to give it to. */
function RequirementTable({
  result,
}: {
  result: NonNullable<ReturnType<typeof computeCoverage>>
}) {
  if (!result.requirements.length) return null
  const worst = Math.max(...result.requirements.map((r) => r.missing), 1)

  return (
    <div className="mb-4 overflow-hidden rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex items-baseline gap-2 border-b hairline px-3 py-2">
        <span className="eyebrow">What is missing</span>
        <span className="text-[11.5px] text-ink-400">worst first</span>
      </div>
      <table className="w-full">
        <thead>
          <tr className="border-b hairline">
            <th className="px-3 py-1.5 text-left"><span className="eyebrow">Attribute</span></th>
            <th className="w-[46px] px-1 py-1.5 text-left"><span className="eyebrow">Mkt</span></th>
            <th className="w-[150px] px-3 py-1.5 text-left"><span className="eyebrow">Fill</span></th>
            <th className="w-[104px] px-2 py-1.5 text-right"><span className="eyebrow">Missing</span></th>
            <th className="w-[70px] px-2 py-1.5 text-right"><span className="eyebrow">Unknown</span></th>
            <th className="w-[56px] px-2 py-1.5 text-right"><span className="eyebrow">N/A</span></th>
            <th className="w-[82px] px-2 py-1.5 text-left"><span className="eyebrow">Owner</span></th>
          </tr>
        </thead>
        <tbody>
          {result.requirements.map((row) => (
            <tr
              key={`${row.attribute}-${row.market}`}
              className="border-b hairline last:border-0 hover:bg-paper-sunk"
            >
              <td className="px-3 py-1.5 text-[12.5px] text-ink-900">
                <span className="flex items-center gap-1.5">
                  {row.label}
                  {row.blind && (
                    <span
                      title={
                        'Every option in scope came back not applicable, so this ' +
                        'requirement contributed nothing. Either the scope holds ' +
                        'none of the sub-categories it applies to, or the ' +
                        'sub-category names do not match the registry — in which ' +
                        'case the gap is hidden, not absent. Check one product ' +
                        'before treating this as clean.'
                      }
                      className="rounded-xs bg-warn-100 px-1 py-px font-mono text-[9px] uppercase tracking-[0.08em] text-warn-600"
                    >
                      matched none
                    </span>
                  )}
                </span>
              </td>
              <td className="px-1 py-1.5 font-mono text-[11px] text-ink-500">
                {row.market === '*' ? 'ALL' : row.market}
              </td>
              <td className="px-3 py-1.5">
                {row.blind ? (
                  // Not "never read" — the column may be perfectly well
                  // populated. No option in scope was applicable, which sends
                  // you somewhere else entirely.
                  <span
                    className="font-mono text-[11px] text-warn-600"
                    title="No option in scope was applicable, so there was nothing to measure."
                  >
                    no match
                  </span>
                ) : row.fillRate === null ? (
                  <span
                    className="font-mono text-[11px] text-ink-400"
                    title="No source has ever been read for this. It is not 0% — nobody can fill in a column we have never loaded."
                  >
                    never read
                  </span>
                ) : (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 w-[74px] overflow-hidden rounded-full bg-ink-100">
                      <div
                        className={cn(
                          'h-full rounded-full',
                          row.fillRate >= 0.95
                            ? 'bg-ok-600'
                            : row.fillRate >= 0.7
                              ? 'bg-warn-600'
                              : 'bg-danger-600',
                        )}
                        style={{ width: `${Math.round(row.fillRate * 100)}%` }}
                      />
                    </div>
                    <span className="font-mono text-[11.5px] tabular-nums text-ink-700">
                      {(row.fillRate * 100).toFixed(1)}%
                    </span>
                  </div>
                )}
              </td>
              <td className="px-2 py-1.5">
                {/* Bar before the number, both on one flex line. Appended
                    after the number it wrapped underneath at this width, which
                    read as a second row of data. */}
                <div className="flex items-center justify-end gap-1.5">
                  {row.missing > 0 && (
                    <span
                      aria-hidden
                      className="h-1.5 flex-none rounded-sm bg-danger-100"
                      style={{ width: `${Math.max(2, (row.missing / worst) * 34)}px` }}
                    />
                  )}
                  <span
                    className={cn(
                      'font-mono text-[12px] tabular-nums',
                      row.missing ? 'text-danger-600' : 'text-ink-300',
                    )}
                  >
                    {row.missing.toLocaleString('pl-PL')}
                  </span>
                </div>
              </td>
              <td className="px-2 py-1.5 text-right font-mono text-[12px] tabular-nums text-ink-400">
                {row.unknown ? row.unknown.toLocaleString('pl-PL') : '·'}
              </td>
              <td className="px-2 py-1.5 text-right font-mono text-[12px] tabular-nums text-ink-300">
                {row.na ? row.na.toLocaleString('pl-PL') : '·'}
              </td>
              <td className="px-2 py-1.5">
                <OwnerTag owner={row.owner} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The standing issue flags — asked of every option whatever the profile is,
 *  because these five are the questions the coverage report has always led
 *  with and none of them is fixed by an import file. */
function IssueTable({
  extract,
  result,
}: {
  extract: CoverageExtract
  result: NonNullable<ReturnType<typeof computeCoverage>>
}) {
  if (!result.issues.length) return null

  return (
    <div className="mb-4 overflow-hidden rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex items-baseline gap-2 border-b hairline px-3 py-2">
        <span className="eyebrow">Issues</span>
        <span className="text-[11.5px] text-ink-400">
          asked of every option, whatever the definition above
        </span>
      </div>
      <table className="w-full">
        <tbody>
          {result.issues.map(({ issue, count }) => (
            <tr key={issue.key} className="border-b hairline last:border-0">
              <td className="w-[190px] px-3 py-2 align-top">
                <div className="flex items-center gap-1.5 text-[12.5px] text-ink-900">
                  {issue.label}
                  {issue.stockBacked && (
                    <span
                      title={extract.stockCaveat}
                      className="rounded-xs bg-warn-100 px-1 py-px font-mono text-[9px] uppercase tracking-[0.08em] text-warn-600"
                    >
                      provisional
                    </span>
                  )}
                </div>
                <div className="mt-0.5 flex items-baseline gap-1.5">
                  <span
                    className={cn(
                      'font-mono text-[15px] font-semibold tabular-nums',
                      count ? 'text-danger-600' : 'text-ink-300',
                    )}
                  >
                    {count.toLocaleString('pl-PL')}
                  </span>
                  <span className="font-mono text-[10.5px] tabular-nums text-ink-400">
                    {result.population
                      ? `${((count / result.population) * 100).toFixed(1)}%`
                      : '—'}
                  </span>
                </div>
              </td>
              <td className="px-3 py-2 align-top text-[12px] leading-relaxed text-ink-500">
                <div>{issue.definition}</div>
                <div className="mt-0.5 text-ink-700">{issue.remedy}</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The options themselves — the drill-down from any number above.
 *
 *  Paged, because the scope bounds the data and not the render: an unpaged
 *  list of 6,242 rows is the mistake the Products page already made once. */
function OptionList({
  products,
  result,
  limit,
}: {
  products: { mc: string; name: string; season: string; sub: string; flags: string[] }[]
  result: NonNullable<ReturnType<typeof computeCoverage>>
  limit: number
}) {
  const [shown, setShown] = useState(limit)
  useEffect(() => setShown(limit), [products, limit])

  if (!products.length) {
    return (
      <div className="rounded-md border hairline bg-paper-card px-6 py-10 text-center shadow-card">
        <div className="font-serif text-[17px] text-ink-900">Nothing in this scope</div>
        <p className="mx-auto mt-1.5 max-w-prose text-[12.5px] text-ink-500">
          Every filter is AND-ed. Clear one to widen it.
        </p>
      </div>
    )
  }

  const visible = products.slice(0, shown)

  return (
    <div className="overflow-hidden rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex items-baseline gap-2 border-b hairline px-3 py-2">
        <span className="eyebrow">Options</span>
        <span className="font-mono text-[11px] tabular-nums text-ink-400">
          {products.length.toLocaleString('pl-PL')}
        </span>
      </div>
      <table className="w-full">
        <tbody>
          {visible.map((product) => {
            const verdict = result.verdicts.get(product.mc)
            return (
              <tr key={product.mc} className="border-b hairline last:border-0 hover:bg-paper-sunk">
                <td className="w-[140px] px-3 py-1.5">
                  <Link
                    to={`/products/${product.mc}`}
                    className="font-mono text-[12px] text-ink-900 hover:text-brand-700 hover:underline"
                  >
                    {product.mc}
                  </Link>
                </td>
                <td className="px-2 py-1.5 text-[12.5px] text-ink-700">
                  <span className="line-clamp-1">{product.name || '—'}</span>
                </td>
                <td className="w-[70px] px-2 py-1.5 font-mono text-[11px] text-ink-400">
                  {product.season || '—'}
                </td>
                <td className="w-[100px] px-2 py-1.5 text-[12px] text-ink-500">
                  {product.sub || '—'}
                </td>
                <td className="w-[300px] px-3 py-1.5">
                  {verdict && verdict.gaps.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {verdict.gaps.slice(0, 4).map((gap, i) => (
                        <span
                          key={`${gap.attribute}-${gap.scope}-${i}`}
                          className="rounded-xs bg-danger-100 px-1.5 py-px text-[11px] text-danger-600"
                        >
                          {gap.scope === 'ALL' ? '' : `${gap.scope} `}
                          {gap.label.toLowerCase()}
                        </span>
                      ))}
                      {verdict.gaps.length > 4 && (
                        <span className="font-mono text-[10.5px] text-ink-400">
                          +{verdict.gaps.length - 4}
                        </span>
                      )}
                    </div>
                  ) : verdict?.standing === 'indeterminate' ? (
                    <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-ink-400">
                      not judgeable
                    </span>
                  ) : (
                    <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-ok-600">
                      complete
                    </span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {shown < products.length && (
        <div className="flex items-center justify-center gap-3 border-t hairline py-2.5">
          <span className="font-mono text-[11px] tabular-nums text-ink-400">
            showing {visible.length.toLocaleString('pl-PL')} of{' '}
            {products.length.toLocaleString('pl-PL')}
          </span>
          <button
            type="button"
            onClick={() => setShown((n) => n + limit)}
            className="rounded-2xs border hairline px-2.5 py-1 text-[12px] font-medium text-ink-700 hover:border-ink-300"
          >
            Load {Math.min(limit, products.length - visible.length)} more
          </button>
        </div>
      )}
    </div>
  )
}

function Facet({
  label,
  values,
  selected,
  onToggle,
  rename,
}: {
  label: string
  values: { value: string; count: number }[]
  selected: string[]
  onToggle: (value: string) => void
  rename?: (value: string) => string
}) {
  const [expanded, setExpanded] = useState(false)
  if (!values.length) return null
  const visible = expanded ? values : values.slice(0, 6)
  const overflow = values.length - 6

  return (
    <div>
      <div className="eyebrow mb-1.5 flex items-center justify-between">
        <span>{label}</span>
        {selected.length > 0 && (
          <span className="font-mono text-[9px] text-brand-700">{selected.length}</span>
        )}
      </div>
      <div className="flex flex-col">
        {visible.map((v) => {
          const on = selected.includes(v.value)
          return (
            <button
              key={v.value}
              type="button"
              onClick={() => onToggle(v.value)}
              className={cn(
                'group flex items-center gap-2 rounded-2xs py-[3px] pl-1 pr-1.5 text-left hover:bg-paper-sunk',
                on && 'bg-brand-50',
              )}
            >
              <span
                className={cn(
                  'grid h-3 w-3 flex-none place-items-center rounded-[2px] border',
                  on
                    ? 'border-brand-700 bg-brand-700'
                    : 'border-ink-300 group-hover:border-ink-400',
                )}
              >
                {on && (
                  <svg viewBox="0 0 10 10" className="h-2 w-2 text-white" aria-hidden>
                    <path
                      d="M1.5 5.2 L4 7.5 L8.5 2.6"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                )}
              </span>
              <span
                className={cn(
                  'min-w-0 flex-1 truncate text-[12.5px]',
                  on ? 'text-brand-900' : 'text-ink-700',
                  v.count === 0 && !on && 'text-ink-300',
                )}
                title={rename ? rename(v.value) : v.value}
              >
                {rename ? rename(v.value) : v.value}
              </span>
              <span className="flex-none font-mono text-[10.5px] tabular-nums text-ink-400">
                {v.count}
              </span>
            </button>
          )
        })}
      </div>
      {overflow > 0 && (
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="mt-1 flex items-center gap-1 pl-1 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 hover:text-ink-700"
        >
          {expanded ? 'less' : `${overflow} more`}
        </button>
      )}
    </div>
  )
}
