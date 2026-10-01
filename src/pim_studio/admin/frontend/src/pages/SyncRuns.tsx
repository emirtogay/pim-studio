/**
 * Sync runs — is what this panel shows actually current?
 *
 * Every number in PIM Studio is as old as the last sync, and a stale figure
 * looks exactly like a fresh one once it is rendered. This page is the only
 * place that difference is visible, which is why the health strip sits above
 * the table rather than below it: the question people arrive with is "is this
 * up to date", not "what happened on 14 August".
 *
 * Two ages, deliberately shown separately, because they fail independently:
 *
 * - **Last success** — how old the data is. Past ~36 hours a nightly job has
 *   missed at least one run. The push is the exception: a person starts it,
 *   so its age is when it was last used and it is never called stale.
 * - **Last full** — when we last checked whether anything was *removed*. Only
 *   a full pass may retire a membership or a withdrawn price; an incremental
 *   one reads a window and can only add. So a month of green nights can hide
 *   a month of unrecorded departures, and every count drifts upward while the
 *   page looks perfectly healthy. That is the failure this second age exists
 *   to make visible.
 */
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type SyncKindHealth, type SyncRunsPayload } from '../lib/api'
import { useApi } from '../lib/useApi'
import { cn } from '../lib/cn'
import PageContainer from '../components/PageContainer'
import StatusPill from '../components/StatusPill'
import { formatDateTime, formatDuration, formatInt, formatRelative } from '../lib/format'

/** Poll while the page is open: a run in flight should visibly finish. */
const POLL_MS = 30_000

export default function SyncRuns() {
  const { data, error, loading, refetch } = useApi(() => api.syncRuns(50), [], {
    pollMs: POLL_MS,
  })

  return (
    <PageContainer>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-[28px] font-medium tracking-tight text-ink-900">
            Sync runs
          </h1>
          <p className="mt-2 font-mono text-[12px] text-ink-500">
            Where the panel&rsquo;s data comes from, and how old it is
          </p>
        </div>
        <button
          type="button"
          onClick={refetch}
          className="inline-flex items-center gap-1.5 rounded-[3px] border hairline bg-paper-card px-2.5 py-1.5 font-mono text-[11px] text-ink-700 hover:bg-paper"
        >
          <RefreshCw size={12} />
          Refresh
        </button>
      </div>

      {loading && !data && (
        <div className="mt-8 h-24 animate-pulse rounded-md border hairline bg-paper-card" />
      )}
      {error && (
        <div className="mt-8 rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
          Could not load sync runs — {error.message}
        </div>
      )}

      {data && (
        <>
          <Warnings data={data} />

          <div className="mt-6 grid gap-px overflow-hidden rounded-md border hairline bg-hairline sm:grid-cols-2 lg:grid-cols-3">
            {data.kinds.map((k) => (
              <KindCard key={k.kind} kind={k} staleAfter={data.staleAfterHours} />
            ))}
          </div>

          <WentDark />

          <h2 className="mt-10 font-mono text-[11px] uppercase tracking-[0.12em] text-ink-500">
            History
          </h2>

          <div className="mt-3 overflow-x-auto rounded-md border hairline bg-paper-card shadow-card">
            <table className="w-full min-w-[860px] border-collapse text-[13px]">
              <thead>
                <tr className="border-b hairline">
                  {['Started', 'Stage', 'Mode', 'Status', 'Took', 'Read', 'Written', 'Dropped', 'Retired'].map(
                    (h) => (
                      <th
                        key={h}
                        className={cn(
                          'px-3 py-2 font-mono text-[10.5px] font-semibold uppercase tracking-[0.1em] text-ink-500',
                          ['Read', 'Written', 'Dropped', 'Retired', 'Took'].includes(h)
                            ? 'text-right'
                            : 'text-left',
                        )}
                      >
                        {h}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {data.runs.map((r) => (
                  <tr key={r.id} className="border-b hairline last:border-0 align-top">
                    <td className="whitespace-nowrap px-3 py-2 font-mono text-[12px] text-ink-700">
                      {formatDateTime(r.startedAt)}
                    </td>
                    <td className="px-3 py-2 font-mono text-[12px] text-ink-900">
                      {r.kind.replace(/^omnitron_/, '')}
                    </td>
                    <td className="px-3 py-2">
                      <ModeTag mode={r.mode} />
                    </td>
                    <td className="px-3 py-2">
                      <StatusPill status={r.status} />
                      {r.errorSummary && (
                        <p className="mt-1 max-w-[38ch] font-mono text-[11px] leading-snug text-ink-500">
                          {r.errorSummary.slice(0, 160)}
                        </p>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-[12px] text-ink-700 tabular-nums">
                      {formatDuration(r.durationSeconds)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-[12px] text-ink-700 tabular-nums">
                      {formatInt(r.rowsSeen)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-[12px] text-ink-700 tabular-nums">
                      {formatInt(r.rowsImported)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-[12px] text-ink-500 tabular-nums">
                      {r.rowsDropped ? formatInt(r.rowsDropped) : '—'}
                    </td>
                    {/* Retirements are the only column here that means data
                        left. Emphasised rather than greyed for that reason. */}
                    <td
                      className={cn(
                        'px-3 py-2 text-right font-mono text-[12px] tabular-nums',
                        r.rowsRetired ? 'font-semibold text-warn-600' : 'text-ink-500',
                      )}
                    >
                      {r.rowsRetired ? formatInt(r.rowsRetired) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.runs.length === 0 && (
              <p className="px-3 py-6 text-center text-[13px] text-ink-500">
                No sync has ever run against this database.
              </p>
            )}
          </div>
        </>
      )}
    </PageContainer>
  )
}

/**
 * What went dark — the products a full pass proved are no longer sellable.
 *
 * This exists because "retired 500 rows" is a notification and a list of 500
 * products is a work queue. Every row opens the product, and carries what it
 * lost rather than only that it lost something: "no PL price" and "no PL
 * price, was 349.00" are different amounts of help.
 *
 * Empty is the normal state on any day the weekly full pass did not run —
 * only a full pass may retire anything — so the empty text says that rather
 * than implying nothing has ever gone dark.
 */
function WentDark() {
  const { data, error } = useApi(() => api.retirements(200), [])

  if (error || !data) return null

  return (
    <>
      <h2 className="mt-10 font-mono text-[11px] uppercase tracking-[0.12em] text-ink-500">
        Went dark{' '}
        {data.total > 0 && (
          <span className="text-ink-700">· {formatInt(data.total)} total</span>
        )}
      </h2>

      {data.products.length === 0 ? (
        <p className="mt-3 rounded-md border hairline bg-paper-card p-4 text-[13px] text-ink-500 shadow-card">
          Nothing recorded. Only a full pass can retire a value, so this stays
          empty until one runs.
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-md border hairline bg-paper-card shadow-card">
          <table className="w-full min-w-[640px] border-collapse text-[13px]">
            <thead>
              <tr className="border-b hairline">
                {['Product', 'Lost', 'Markets', 'Last value', 'When'].map((h) => (
                  <th
                    key={h}
                    className="px-3 py-2 text-left font-mono text-[10.5px] font-semibold uppercase tracking-[0.1em] text-ink-500"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.products.map((p) => (
                <tr key={p.modelColor ?? p.sku} className="border-b hairline last:border-0">
                  <td className="px-3 py-2 font-mono text-[12px]">
                    {p.modelColor ? (
                      <Link
                        to={`/products/${p.modelColor}`}
                        className="text-ink-900 underline decoration-hairline underline-offset-2 hover:decoration-ink-500"
                      >
                        {p.modelColor}
                      </Link>
                    ) : (
                      // The product record is gone but the event is not.
                      <span className="text-ink-500" title="No product record — SKU only">
                        {p.sku}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-[12px] text-ink-700">
                    {p.fields.join(', ')}
                  </td>
                  <td className="px-3 py-2 font-mono text-[12px] text-ink-700">
                    {p.markets.join(' ')}
                  </td>
                  <td className="px-3 py-2 font-mono text-[12px] text-ink-500 tabular-nums">
                    {Object.entries(p.lastValues)
                      .map(([k, v]) => `${k} ${v ?? '—'}`)
                      .join(' · ')}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-[12px] text-ink-500">
                    {formatRelative(p.retiredAt)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.shown < data.total && (
            <p className="border-t hairline px-3 py-2 font-mono text-[11px] text-ink-500">
              Showing the {formatInt(data.shown)} most recent of{' '}
              {formatInt(data.total)}.
            </p>
          )}
        </div>
      )}
    </>
  )
}

function ModeTag({ mode }: { mode: 'full' | 'incremental' }) {
  const full = mode === 'full'
  return (
    <span
      title={
        full
          ? 'Read everything upstream holds, so removals were detected.'
          : 'Read only what changed. Cannot detect removals.'
      }
      className={cn(
        'rounded-[3px] px-1.5 py-0.5 font-mono text-[10.5px] font-semibold',
        full ? 'bg-info-100 text-info-600' : 'bg-ink-100 text-ink-600',
      )}
    >
      {full ? 'full' : 'incr'}
    </span>
  )
}

/** How old a stage has to be before the red banner speaks, on top of being
 *  past its own cadence.
 *
 *  The banner used to fire on any cadence breach, which put a red box across
 *  the top of the page whenever `storefront` was seven hours old — a stage
 *  whose cadence is two-hourly, so it breached every working day by design
 *  and said so in a colour that means "something is wrong". The cards below
 *  already carry the same fact per stage, in the right register, so the
 *  banner was repeating them in a louder one.
 *
 *  A day is the line because it is the point where every cadence here has
 *  missed enough runs to be a real fault rather than a late one: the
 *  two-hourly stages have missed twelve, the nightly one. Kept as a floor on
 *  top of `stale` rather than replacing it, because a flat 24h would drag in
 *  `brandlibrary_publish`, which runs weekly and is perfectly healthy at
 *  thirty hours. Both conditions, or no banner. */
const BANNER_AFTER_HOURS = 24

function Warnings({ data }: { data: SyncRunsPayload }) {
  const stale = data.kinds.filter((k) => k.stale)
  const { reconciliationOverdue: overdue, unfinished } = data
  // Never having succeeded is alarming at any age, hence the Infinity.
  const old = stale.filter(
    (k) => (k.ageHours ?? Number.POSITIVE_INFINITY) >= BANNER_AFTER_HOURS,
  )

  if (!stale.length && !overdue.length && !unfinished.length) {
    return (
      <div className="mt-6 rounded-md border hairline bg-paper-card p-4 text-[13px] text-ink-500 shadow-card">
        Every scheduled stage has run recently and been fully reconciled. What
        you are looking at is current.
      </div>
    )
  }

  // Past a cadence but not by a day, and nothing else to report. Deliberately
  // silent rather than reassuring: the cards below show which stage and how
  // old, and "everything is current" would not be true.
  if (!old.length && !overdue.length && !unfinished.length) return null

  return (
    <div className="mt-6 space-y-2">
      {old.length > 0 && (
        <Warning tone="danger" title="The data is old">
          {old
            .map((k) => `${shortKind(k.kind)} (over ${k.staleAfterHours}h)`)
            .join(', ')}{' '}
          &mdash; each more than a day past its own cadence. Everything in this
          panel is as old as those runs.
        </Warning>
      )}
      {overdue.length > 0 && (
        <Warning tone="warn" title="Nothing has been removed in a while">
          {overdue
            .map(
              (o) =>
                `${shortKind(o.kind)} (${o.days === null ? 'never reconciled' : `${o.days} days ago`})`,
            )
            .join(', ')}
          . Incremental runs only ever add, so departures and withdrawn prices
          are accumulating unrecorded and the live counts can only drift
          upward. A full pass is what clears this.
        </Warning>
      )}
      {unfinished.length > 0 && (
        <Warning tone="warn" title="A run never finished">
          {unfinished.map((u) => `#${u.id} ${shortKind(u.kind)}`).join(', ')} is
          still marked running. That is a process that died without closing its
          row, not one still working.
        </Warning>
      )}
    </div>
  )
}

function Warning({
  tone,
  title,
  children,
}: {
  tone: 'warn' | 'danger'
  title: string
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        'flex gap-2.5 rounded-md border hairline p-4 shadow-card',
        tone === 'danger' ? 'bg-danger-100' : 'bg-warn-100',
      )}
    >
      <AlertTriangle
        size={15}
        className={cn('mt-0.5 shrink-0', tone === 'danger' ? 'text-danger-600' : 'text-warn-600')}
      />
      <div className="text-[13px] leading-relaxed text-ink-900">
        <span className="font-medium">{title}.</span>{' '}
        <span className="text-ink-700">{children}</span>
      </div>
    </div>
  )
}

function KindCard({
  kind,
  staleAfter,
}: {
  kind: SyncKindHealth
  staleAfter: number
}) {
  return (
    <div className="bg-paper-card p-4">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-[12px] font-medium text-ink-900">
          {shortKind(kind.kind)}
        </span>
        {kind.lastStatus && <StatusPill status={kind.lastStatus as never} />}
      </div>

      <dl className="mt-3 space-y-1.5">
        <Row
          label="last success"
          value={kind.lastSuccessAt ? formatRelative(kind.lastSuccessAt) : 'never'}
          bad={kind.stale}
        />
        {/* Only shown for the kinds that own a sweep. For the others there is
            nothing a full pass would prove, so the row would be noise. */}
        {kind.needsReconcile && (
          <Row
            label="last full"
            value={kind.lastFullAt ? formatRelative(kind.lastFullAt) : 'never'}
            bad={!kind.lastFullAt}
          />
        )}
      </dl>

      {kind.stale && (
        <p className="mt-2 font-mono text-[10.5px] text-danger-600">
          over {kind.staleAfterHours ?? staleAfter}h old
        </p>
      )}
      {/* Said rather than left implicit: without it, a push last used nine
          days ago reads like a job that stopped nine days ago. */}
      {kind.staleAfterHours === null && (
        <p className="mt-2 font-mono text-[10.5px] text-ink-500">
          on demand · no schedule
        </p>
      )}
    </div>
  )
}

function Row({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-ink-500">
        {label}
      </dt>
      <dd
        className={cn(
          'font-mono text-[11.5px]',
          bad ? 'font-semibold text-danger-600' : 'text-ink-700',
        )}
      >
        {value}
      </dd>
    </div>
  )
}

/** `omnitron_catalogue` → `catalogue`. The prefix is on every row and carries
 *  no information on a page that is entirely about Omnitron. */
function shortKind(kind: string): string {
  return kind.replace(/^omnitron_/, '')
}
