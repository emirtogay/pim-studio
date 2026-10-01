/**
 * Value conflicts — where a product's own sizes disagreed.
 *
 * Since 0.9.3 these are not holes. The importer writes a best value and flags
 * it here, because an empty cell next to a full Omnitron column reads as the
 * panel being broken rather than as caution. So this page is a review list:
 * each row shows what was written, which rule chose it, and every candidate it
 * beat with the number of sizes behind each.
 *
 * Choosing a different candidate writes it at `approved`, a state the sync
 * writer will not overwrite — so the decision survives every future run, and
 * the row leaves the queue rather than coming back tomorrow.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Check } from 'lucide-react'
import { api, type ConflictRow } from '../lib/api'
import { useApi } from '../lib/useApi'
import { cn } from '../lib/cn'
import PageContainer from '../components/PageContainer'

const RULE_NOTE: Record<string, string> = {
  language: 'the other candidates read as English',
  majority: 'most sizes said so',
}

export default function Conflicts() {
  const { data, error, loading, refetch } = useApi(() => api.conflicts(100), [])
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  async function decide(r: ConflictRow, value: string) {
    const id = `${r.modelColor}-${r.attribute}-${r.market}`
    setBusy(id)
    setFailed(null)
    try {
      await api.decideConflict({
        modelColor: r.modelColor,
        attribute: r.attribute,
        market: r.market,
        value,
      })
      await refetch()
    } catch (e) {
      setFailed(e instanceof Error ? e.message : 'Could not save that decision')
    } finally {
      setBusy(null)
    }
  }

  return (
    <PageContainer>
      <h1 className="font-serif text-[28px] font-medium tracking-tight text-ink-900">
        Value conflicts
      </h1>
      <p className="mt-2 font-mono text-[12px] text-ink-500">
        Sizes of one product disagreeing with each other
      </p>

      {loading && (
        <div className="mt-8 h-24 animate-pulse rounded-md border hairline bg-paper-card" />
      )}
      {error && (
        <div className="mt-8 rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
          Could not load conflicts — {error.message}
        </div>
      )}
      {failed && (
        <div className="mt-4 rounded-md border hairline bg-paper-card p-3 text-[13px] text-ink-900">
          {failed}
        </div>
      )}

      {data && (
        <>
          <div className="mt-6 rounded-md border hairline bg-paper-card p-6 shadow-card">
            <p className="text-[13px] leading-relaxed text-ink-500">
              <span className="font-medium text-ink-900">
                {data.total.toLocaleString()}
              </span>{' '}
              values were written from contradictory sources. A value is in the
              panel either way — this is where you correct the ones it got
              wrong. Picking a candidate keeps it through every future sync.
            </p>
            {data.byAttribute.length > 0 && (
              <div className="mt-4 flex flex-wrap gap-1.5">
                {data.byAttribute.map((a) => (
                  <span
                    key={a.attribute}
                    className="rounded border hairline bg-paper px-2 py-1 font-mono text-[11px] text-ink-700"
                  >
                    {a.attribute} · {a.count.toLocaleString()}
                  </span>
                ))}
              </div>
            )}
            {/* Counted, not listed, and never silently dropped — the number is
                the point. A row removed from a queue with nothing said reads
                exactly like a row that was never detected. */}
            {data.notAnOption && data.notAnOption.rows > 0 && (
              <p className="mt-4 border-t hairline pt-3 text-[12.5px] leading-relaxed text-ink-500">
                A further{' '}
                <span className="font-medium text-ink-900">
                  {data.notAnOption.rows.toLocaleString()}
                </span>{' '}
                are not shown, across{' '}
                <span className="font-medium text-ink-900">
                  {data.notAnOption.styles.toLocaleString()}
                </span>{' '}
                styles. Omnitron gives no colour in its base code for these, so
                every colour of the style arrives under one code and its sizes
                cannot be compared as one product — the candidates are different
                colours, each right for its own. There is no value to choose,
                so they are counted here rather than queued.
              </p>
            )}
          </div>

          {data.total === 0 ? (
            <div className="mt-4 rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
              Nothing to review. Either every product agrees with itself, or the
              value sync has not run against this database yet.
            </div>
          ) : (
            <div className="mt-4 space-y-3">
              {data.rows.map((r) => {
                const id = `${r.modelColor}-${r.attribute}-${r.market}`
                const entries = Object.entries(r.candidates).sort((a, b) => b[1] - a[1])
                return (
                  <div
                    key={id}
                    className="rounded-md border hairline bg-paper-card p-4 shadow-card"
                  >
                    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                      <Link
                        to={`/products/${r.modelColor}`}
                        className="font-mono text-[12px] text-brand-700 hover:underline"
                      >
                        {r.modelColor}
                      </Link>
                      <span className="font-mono text-[12px] text-ink-900">
                        {r.attribute}
                      </span>
                      <span className="font-mono text-[11px] text-ink-500">
                        {r.market} · {r.skuCount} sizes
                      </span>
                      {r.resolvedRule && (
                        <span className="rounded bg-paper-sunk px-1.5 py-0.5 font-mono text-[10px] text-ink-500">
                          in use: {RULE_NOTE[r.resolvedRule] ?? r.resolvedRule}
                        </span>
                      )}
                    </div>

                    <div className="mt-3 flex flex-col gap-1.5">
                      {entries.map(([value, n]) => {
                        const inUse = r.resolvedValue === value
                        return (
                          <div
                            key={value}
                            className={cn(
                              'flex items-start gap-2 rounded border p-2',
                              inUse ? 'border-brand-600 bg-brand-50' : 'hairline bg-paper',
                            )}
                          >
                            <span className="mt-0.5 shrink-0 rounded bg-paper-sunk px-1.5 py-0.5 font-mono text-[10px] text-ink-500">
                              {n}×
                            </span>
                            <span className="min-w-0 flex-1 break-words text-[13px] text-ink-700">
                              {value.length > 220 ? `${value.slice(0, 220)}…` : value}
                            </span>
                            {inUse ? (
                              <span className="mt-0.5 flex shrink-0 items-center gap-1 font-mono text-[10px] uppercase tracking-wide text-brand-700">
                                <Check size={12} /> in use
                              </span>
                            ) : (
                              <button
                                disabled={busy === id}
                                onClick={() => decide(r, value)}
                                className="mt-0.5 shrink-0 rounded border hairline px-2 py-0.5 text-[12px] text-ink-700 hover:border-brand-600 hover:text-brand-700 disabled:opacity-50"
                              >
                                {busy === id ? 'saving…' : 'use this'}
                              </button>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {data.total > data.rows.length && (
            <p className="mt-3 font-mono text-[11px] text-ink-500">
              Showing {data.rows.length.toLocaleString()} of{' '}
              {data.total.toLocaleString()}.
            </p>
          )}
        </>
      )}
    </PageContainer>
  )
}
