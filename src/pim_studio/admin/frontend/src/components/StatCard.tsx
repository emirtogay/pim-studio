import type { ReactNode } from 'react'

import { cn } from '../lib/cn'

/**
 * Single KPI card, used in the 4-across row at the top of Overview.
 * Label is rendered in the mono-uppercase "eyebrow" style; the value is
 * large tabular-numeric so numbers line up vertically across the row.
 *
 * `tone` chooses a semantic accent for the number — neutral by default.
 * `delta` takes a chip showing movement since a baseline, rendered under the
 * number so the row stays one line tall whether or not history exists yet.
 */
export type StatTone = 'neutral' | 'ok' | 'warn' | 'danger'

export default function StatCard({
  label,
  value,
  tone = 'neutral',
  loading,
  hint,
  delta,
  sub,
}: {
  label: string
  value: string | number | null | undefined
  tone?: StatTone
  loading?: boolean
  hint?: string
  delta?: ReactNode
  /** A line of context under the number — the baseline date, typically. */
  sub?: ReactNode
}) {
  const display =
    value === null || value === undefined || value === ''
      ? '—'
      : typeof value === 'number'
        ? value.toLocaleString('pl-PL')
        : value

  return (
    <div
      className={cn(
        'rounded-md border hairline bg-paper-card px-3.5 py-3 shadow-card',
        hint && 'cursor-help',
      )}
      title={hint}
    >
      <div className="mb-1 flex items-center gap-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ink-500">
        {label}
        {hint && <span aria-hidden className="text-ink-400">ⓘ</span>}
      </div>
      {loading ? (
        <div className="skeleton h-[26px] w-20" />
      ) : (
        <div
          className={cn(
            'font-mono text-[22px] font-semibold tabular-nums',
            TONE[tone],
          )}
        >
          {display}
        </div>
      )}
      {!loading && (delta || sub) && (
        <div className="mt-1 flex items-center gap-2">
          {delta}
          {sub && <span className="text-[11.5px] text-ink-500">{sub}</span>}
        </div>
      )}
    </div>
  )
}

const TONE: Record<StatTone, string> = {
  neutral: 'text-ink-900',
  ok: 'text-ok-600',
  warn: 'text-warn-600',
  danger: 'text-danger-600',
}
