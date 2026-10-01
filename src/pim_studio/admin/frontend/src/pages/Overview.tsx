import { AlertTriangle } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'

import DeltaChip from '../components/DeltaChip'
import RebuildExtract from '../components/RebuildExtract'
import StatCard from '../components/StatCard'
import { useAuth } from '../lib/auth'
import { cn } from '../lib/cn'
import { loadOverview } from '../lib/demoData'
import type {
  Bucket,
  Drop,
  OverviewExtract,
  Reason,
  Slice,
  StageMeta,
} from '../lib/overview'
import {
  ALL_SLICE,
  biggestDrop,
  bucketFor,
  dropSideFor,
  hrefForInSeason,
} from '../lib/overview'
import { useApi } from '../lib/useApi'
import type { Market } from '../lib/api'

/**
 * NOT ROUTED. `/` redirects to Coverage — see the note in `App.tsx`.
 *
 * Kept rather than deleted because the rework reuses most of what is below
 * the funnel: the extract types and selectors in `lib/overview.ts`, the
 * StatCard/DeltaChip wiring, the empty state, the season picker, and the
 * deep-link helpers that were checked against real data. What is being cut is
 * the funnel rendering itself.
 *
 * What replaces it: a briefing that states three to five conclusions in plain
 * sentences, each linking to its queue — "Slovakia and Hungary are the
 * constraint: 94 options sit in another market's catalogue but not theirs",
 * "Turkey owes 109 product cards" — and keeps only what Coverage structurally
 * cannot say: the prerequisite ordering, the four markets side by side, the
 * per-market liveness verdict, and the week-over-week deltas. Everything that
 * duplicated Coverage links to Coverage instead.
 *
 * Deleting this file is fine once that lands. Leaving it routed was not.
 *
 * ── the original header follows ───────────────────────────────────────────
 *
 * The morning page: how far each option got, and whose queue the rest sit in.
 *
 * Everything here is read from `overview.json`. No count is computed in the
 * browser — the stage verdicts, the drop reasons, the owners and the deep-link
 * facet values are all decided in `rules/funnel` and `reports/overview`, where
 * they are tested, and `python -m pim_studio.reports funnel` prints the same
 * numbers from the same code.
 *
 * Three things about the design are load-bearing rather than aesthetic:
 *
 * **The bars carry two numbers.** A solid bar for options through the stage
 * and a pale one for options no source could judge. Folding the second into
 * either the first or the drop is the mistake the whole codebase is arranged
 * against — and on a funnel it is worse than elsewhere, because the shape is
 * read at a glance and one mis-bucketed stage bends the whole curve.
 *
 * **The first stage is drawn empty.** No assortment source is ingested, so the
 * drop into Omnitron cannot be measured. Omitting the stage would make the
 * funnel look complete; drawing it greyed says a source is missing, which is
 * the actual finding.
 *
 * **Live appears twice.** The last stage is "live with every stage complete";
 * the headline is what is live on the site. They differ because the site
 * enforces the six liveness rules and not our content pipeline, and the gap
 * between them is stated under the funnel rather than reconciled away.
 */
export default function Overview() {
  const { data: extract, loading } = useApi(loadOverview, [])
  const [params, setParams] = useSearchParams()
  const { user } = useAuth()

  // The scope lives in the URL and is read fresh every render, so an analysis
  // is a link somebody can send rather than a screenshot, and Back walks
  // through the seasons you looked at.
  const requested = params.get('season') ?? ''
  const season = extract?.slices[requested] ? requested : ''
  const slice = extract?.slices[season || ALL_SLICE]

  if (loading) return <OverviewSkeleton />

  if (!extract || !slice || !extract.population) {
    return (
      <div className="mx-auto max-w-content px-10 py-12">
        <div className="eyebrow mb-[7px]">Overview</div>
        <h1 className="mb-3 font-serif text-[28px] font-medium tracking-tight">
          No overview extract
        </h1>
        <p className="max-w-prose text-[13px] text-ink-500">
          Nothing has been generated yet. Run{' '}
          <code className="rounded-xs bg-paper-sunk px-1 font-mono text-[12px]">
            python scripts/build_demo_data_from_db.py
          </code>{' '}
          to build it from the mirror, or press the button below to run exactly
          that on the server. This page shows nothing rather than a fixture on
          purpose — an invented funnel reads exactly like a real one.
        </p>
        <RebuildExtract />
      </div>
    )
  }

  const generated = extract.generatedAt
    ? new Date(extract.generatedAt).toISOString().slice(0, 16).replace('T', ' ')
    : '—'
  const deltas = extract.deltas?.[season || ALL_SLICE] ?? null
  const worst = biggestDrop(slice)
  const stockCaveat = extract.caveats.find((c) => c.key === 'stock')

  function chooseSeason(next: string) {
    const updated = new URLSearchParams(params)
    if (next) updated.set('season', next)
    else updated.delete('season')
    setParams(updated, { replace: true })
  }

  return (
    <div className="mx-auto max-w-content px-10 py-12">
      <div className="mb-4">
        <div className="eyebrow mb-[7px]">Overview</div>
        <h1 className="font-serif text-[28px] font-medium tracking-tight">
          Go-live funnel
        </h1>
        <p className="mt-1 font-mono text-[11px] text-ink-400">
          data as at {generated} UTC ·{' '}
          {extract.population.toLocaleString('pl-PL')} options in the mirror
        </p>
      </div>

      <SeasonPicker
        seasons={extract.seasons}
        total={extract.population}
        value={season}
        onChange={chooseSeason}
      />

      <div className="mb-3 grid grid-cols-4 gap-3">
        <StatCard
          label="Live on the site"
          value={slice.headline.live}
          tone="ok"
          hint={extract.markets
            .map((m) => `${m} ${slice.liveNow[m]?.toLocaleString('pl-PL') ?? 0}`)
            .join(' · ')}
          delta={<DeltaChip count={deltas?.live} good="up" />}
        />
        <StatCard
          label="Blocked everywhere"
          value={slice.headline.blocked}
          tone="danger"
          hint="Live in none of the four markets."
          delta={<DeltaChip count={deltas?.blocked} good="down" />}
        />
        <StatCard
          label="Waiting on Turkey"
          value={slice.headline.waitingOnPartner}
          tone="warn"
          hint="Stopped at base attributes — category, sub-category, gender or composition. We can request these, not write them."
          delta={<DeltaChip count={deltas?.waitingOnPartner} good="down" />}
        />
        <StatCard
          label="In scope"
          value={slice.population}
          // A delta with no stated period is a delta nobody can check, and
          // "no movement" and "no baseline" must never look the same.
          sub={
            deltas
              ? `vs ${deltas.baselineDate} (${deltas.ageDays}d)`
              : 'no baseline yet'
          }
        />
      </div>

      {stockCaveat && (
        <div className="mb-3 flex items-start gap-2 rounded-md border border-warn-600/40 bg-warn-100 px-3 py-2.5">
          <AlertTriangle size={14} className="mt-0.5 flex-none text-warn-600" />
          <p className="text-[12.5px] leading-relaxed text-warn-600">
            {stockCaveat.text}
          </p>
        </div>
      )}

      <Funnel
        extract={extract}
        slice={slice}
        season={season}
        worstStage={worst?.to ?? null}
      />

      <Defects
        extract={extract}
        slice={slice}
        season={season}
        canSeeConflicts={user?.role === 'developer' || user?.role === 'planning'}
      />

      <div className="mt-4 space-y-2">
        {extract.caveats
          .filter((c) => c.key !== 'stock')
          .map((caveat) => (
            <p
              key={caveat.key}
              className="max-w-prose text-[12px] leading-relaxed text-ink-500"
            >
              {caveat.text}
            </p>
          ))}
      </div>
    </div>
  )
}

// ── The funnel ─────────────────────────────────────────────────────────────

function Funnel({
  extract,
  slice,
  season,
  worstStage,
}: {
  extract: OverviewExtract
  slice: Slice
  season: string
  worstStage: string | null
}) {
  const rows: JSX.Element[] = []
  for (const [index, stage] of extract.stages.entries()) {
    const drop = slice.drops.find((d) => d.to === stage.key)
    if (drop) {
      rows.push(
        <DropBand
          key={`drop-${stage.key}`}
          drop={drop}
          markets={extract.markets}
          season={season}
          biggest={stage.key === worstStage}
        />,
      )
    }
    rows.push(
      <StageRow
        key={stage.key}
        stage={stage}
        slice={slice}
        markets={extract.markets}
        population={slice.population}
        first={index === 0}
      />,
    )
  }

  return (
    <div className="rounded-md border hairline bg-paper-card shadow-card">
      <div className="border-b hairline px-4 py-2.5">
        <div className="eyebrow">Pipeline</div>
        <p className="mt-1 max-w-prose text-[12px] leading-relaxed text-ink-500">
          Cumulative — an option reaches a stage only if every earlier one
          passed. Pale bars are stages no source could judge, which is not a
          gap and not a pass. A drop counts options stopping{' '}
          <em>here first</em>, so the queue behind it lists more: everything
          carrying that gap, including options already stopped further up.
        </p>
      </div>
      <div className="px-4 py-3">{rows}</div>
      {slice.liveDespiteGaps > 0 && (
        <div className="border-t hairline px-4 py-2.5">
          <p className="text-[12px] leading-relaxed text-ink-500">
            <span className="font-mono font-semibold tabular-nums text-ink-700">
              {slice.liveDespiteGaps.toLocaleString('pl-PL')}
            </span>{' '}
            options are live on the site with an earlier stage still
            incomplete. The site enforces the six go-live rules, not our
            content pipeline — so a product with no Czech description can still
            be selling in Czechia.{' '}
            <Link
              to={hrefForInSeason(
                { page: 'products', params: [['readiness', 'live somewhere']] },
                season,
              )}
              className="text-ink-700 underline underline-offset-2 hover:text-brand-700"
            >
              See them
            </Link>
            .
          </p>
        </div>
      )}
    </div>
  )
}

function StageRow({
  stage,
  slice,
  markets,
  population,
  first,
}: {
  stage: StageMeta
  slice: Slice
  markets: Market[]
  population: number
  first: boolean
}) {
  if (!stage.computable) {
    return (
      <div className={cn('flex items-center gap-3 py-1.5', !first && 'mt-1')}>
        <StageLabel stage={stage} />
        <div className="flex flex-1 items-center gap-2">
          <span className="w-[18px]" aria-hidden />
          <div className="h-[18px] flex-1 rounded-2xs border border-dashed border-ink-200" />
          <div className="w-[120px] flex-none text-right font-mono text-[11.5px] text-ink-400">
            no source
          </div>
        </div>
      </div>
    )
  }

  const buckets = markets.map((m) => bucketFor(slice, stage, m))
  const shared = !stage.perMarket

  return (
    <div className="py-1.5">
      <div className="flex items-center gap-3">
        <StageLabel stage={stage} />
        {/* Shared and per-market rows share one geometry — the same market
            gutter, the same bar box, the same count column — so every bar on
            the page is on one x-scale. Without that the eye compares two
            different scales and the funnel's shape lies. */}
        <div className="flex-1 space-y-[3px]">
          {shared ? (
            <div className="flex items-center gap-2">
              <span className="w-[18px]" aria-hidden />
              <Bar bucket={buckets[0]} population={population} />
              <Count bucket={buckets[0]} />
            </div>
          ) : (
            markets.map((market, i) => (
              <div key={market} className="flex items-center gap-2">
                <span className="w-[18px] font-mono text-[10px] text-ink-500">
                  {market}
                </span>
                <Bar bucket={buckets[i]} population={population} slim />
                <Count bucket={buckets[i]} slim />
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}

function StageLabel({ stage }: { stage: StageMeta }) {
  return (
    <div className="w-[210px] flex-none" title={stage.blurb}>
      <div
        className={cn(
          'text-[12.5px]',
          stage.computable ? 'text-ink-900' : 'text-ink-400',
        )}
      >
        {stage.label}
      </div>
      {stage.owner && (
        <div className="mt-[1px] font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400">
          {stage.owner === 'partner' ? 'Turkey' : 'ours'}
        </div>
      )}
    </div>
  )
}

/**
 * One stage as a bar. Two segments: through, and could-not-say.
 *
 * Ink rather than a status colour — the funnel is a quantity, not a verdict,
 * and brand green is reserved for navigation and selection so it never
 * competes with the ok/warn/danger the drops use.
 */
function Bar({
  bucket,
  population,
  slim,
}: {
  bucket: Bucket | null
  population: number
  slim?: boolean
}) {
  const passed = bucket?.passed ?? 0
  const indeterminate = bucket?.indeterminate ?? 0
  const scale = population || 1
  return (
    <div
      className={cn(
        'flex-1 overflow-hidden rounded-2xs bg-ink-100',
        slim ? 'h-[9px]' : 'h-[18px]',
      )}
    >
      <div className="flex h-full">
        <div
          className="h-full bg-ink-700"
          style={{ width: `${(passed / scale) * 100}%` }}
        />
        <div
          className="h-full bg-ink-200"
          style={{ width: `${(indeterminate / scale) * 100}%` }}
          title={
            indeterminate
              ? `${indeterminate.toLocaleString('pl-PL')} with no source to judge`
              : undefined
          }
        />
      </div>
    </div>
  )
}

function Count({ bucket, slim }: { bucket: Bucket | null; slim?: boolean }) {
  const passed = bucket?.passed ?? 0
  const indeterminate = bucket?.indeterminate ?? 0
  return (
    <div
      className={cn(
        'w-[120px] flex-none text-right font-mono tabular-nums',
        slim ? 'text-[11px]' : 'text-[13px] font-semibold',
      )}
    >
      <span className="text-ink-900">{passed.toLocaleString('pl-PL')}</span>
      {indeterminate > 0 && (
        <span className="ml-1.5 text-[10.5px] text-ink-400">
          +{indeterminate.toLocaleString('pl-PL')}?
        </span>
      )}
    </div>
  )
}

/**
 * The gap between two stages, and the queues behind it.
 *
 * The brief asks that the biggest bottleneck be obvious in under two seconds
 * and that every drop lead somewhere without arrows everywhere. So the count
 * is set in danger red, the reasons underneath are ordinary links, and only
 * the largest drop carries a tag.
 */
function DropBand({
  drop,
  markets,
  season,
  biggest,
}: {
  drop: Drop
  markets: Market[]
  season: string
  biggest: boolean
}) {
  const sides = drop.shared
    ? [{ market: null as Market | null, side: drop.shared }]
    : markets
        .map((market) => ({ market, side: dropSideFor(drop, market) }))
        .filter((entry) => entry.side && entry.side.count > 0)

  const total = sides.reduce((sum, entry) => sum + (entry.side?.count ?? 0), 0)
  if (!total) return null

  return (
    <div className="my-1 ml-[210px] border-l-2 border-danger-600/25 pl-3">
      {biggest && (
        <div className="mb-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-danger-600">
          Biggest drop
        </div>
      )}
      {sides.map(({ market, side }) => (
        <div key={market ?? 'shared'} className="mb-1 flex items-start gap-3">
          <div className="flex w-[64px] flex-none items-baseline gap-1">
            {market && (
              <span className="font-mono text-[10px] text-ink-500">
                {market}
              </span>
            )}
            <span
              className={cn(
                'font-mono tabular-nums text-danger-600',
                biggest ? 'text-[14px] font-semibold' : 'text-[12px]',
              )}
            >
              −{side?.count.toLocaleString('pl-PL')}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
            {side?.reasons.map((reason) => (
              <ReasonLink key={reason.key} reason={reason} season={season} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function ReasonLink({ reason, season }: { reason: Reason; season: string }) {
  const body = (
    <>
      <span className="font-mono tabular-nums">
        {reason.count.toLocaleString('pl-PL')}
      </span>{' '}
      {reason.label}
      <span className="ml-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-400">
        {reason.owner === 'partner' ? 'Turkey' : reason.owner}
      </span>
    </>
  )
  // No link where no facet would find them — a queue that comes back empty
  // reads exactly like a job already done.
  if (!reason.link) {
    return (
      <span className="text-[12px] text-ink-500" title={reason.remedy}>
        {body}
      </span>
    )
  }
  return (
    <Link
      to={hrefForInSeason(reason.link, season)}
      title={reason.remedy}
      className="text-[12px] text-ink-700 hover:text-brand-700 hover:underline hover:underline-offset-2"
    >
      {body}
    </Link>
  )
}

// ── Everything else ────────────────────────────────────────────────────────

function SeasonPicker({
  seasons,
  total,
  value,
  onChange,
}: {
  seasons: { key: string; label: string; count: number }[]
  total: number
  value: string
  onChange: (next: string) => void
}) {
  if (!seasons.length) return null
  const options = [{ key: '', label: 'All seasons', count: total }, ...seasons]
  return (
    <div className="mb-3 flex flex-wrap items-center gap-1.5">
      {options.map((option) => {
        const active = option.key === value
        return (
          <button
            key={option.key || 'all'}
            type="button"
            onClick={() => onChange(option.key)}
            className={cn(
              'rounded-2xs border px-2.5 py-1 text-[12px]',
              active
                ? 'border-brand-700 bg-brand-50 text-brand-900'
                : 'hairline bg-paper-card text-ink-700 hover:bg-paper-sunk',
            )}
          >
            {option.label}
            <span className="ml-1.5 font-mono text-[10.5px] tabular-nums text-ink-400">
              {option.count.toLocaleString('pl-PL')}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/**
 * Filled, and wrong. Never part of the funnel.
 *
 * A Slovak description holding English passes every not-null check there is,
 * so it would count as a pass at the Localised stage — correctly, because the
 * field is filled. It is still work, but different work for a different
 * person, which is why it sits in its own panel with its own counts.
 */
function Defects({
  extract,
  slice,
  season,
  canSeeConflicts,
}: {
  extract: OverviewExtract
  slice: Slice
  season: string
  canSeeConflicts: boolean
}) {
  const showConflicts = canSeeConflicts && extract.conflicts.productName > 0
  if (!slice.defects.length && !showConflicts) return null

  return (
    <div className="mt-3 rounded-md border hairline bg-paper-card shadow-card">
      <div className="border-b hairline px-4 py-2.5">
        <div className="eyebrow">Wrong, not missing</div>
        <p className="mt-1 text-[12px] text-ink-500">
          Fields that are filled and defective. Not part of the funnel — a
          value in the wrong language passes every not-null check there is.
        </p>
      </div>
      <div className="divide-y divide-hairline">
        {slice.defects.map((defect) => (
          <div
            key={defect.key}
            className="flex items-center justify-between px-4 py-1.5"
          >
            {defect.link ? (
              <Link
                to={hrefForInSeason(defect.link, season)}
                className="text-[12.5px] text-ink-700 hover:text-brand-700 hover:underline hover:underline-offset-2"
              >
                {defect.label}
              </Link>
            ) : (
              <span className="text-[12.5px] text-ink-700">{defect.label}</span>
            )}
            <span className="font-mono text-[12.5px] tabular-nums text-danger-600">
              {defect.count.toLocaleString('pl-PL')}
            </span>
          </div>
        ))}
        {showConflicts && (
          <div className="flex items-center justify-between px-4 py-1.5">
            <Link
              to="/admin/conflicts"
              className="text-[12.5px] text-ink-700 hover:text-brand-700 hover:underline hover:underline-offset-2"
            >
              Name differs between sizes
            </Link>
            <span className="font-mono text-[12.5px] tabular-nums text-danger-600">
              {extract.conflicts.productName.toLocaleString('pl-PL')}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

function OverviewSkeleton() {
  return (
    <div className="mx-auto max-w-content px-10 py-12">
      <div className="skeleton mb-4 h-8 w-64" />
      <div className="mb-3 grid grid-cols-4 gap-3">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="skeleton h-[76px]" />
        ))}
      </div>
      <div className="skeleton h-[420px]" />
    </div>
  )
}
