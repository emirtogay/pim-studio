/**
 * Formatting helpers shared across the admin panel pages.
 *
 * All display strings go through here so we can tweak locale / precision
 * / thousands-separator conventions in one place. The target audience (PL
 * analyst, FR/TR managers) reads numbers formatted with space thousands
 * separators (`pl-PL`), so that's the default.
 */

/** Format a duration in seconds as e.g. "4m 12s" or "38s" for sub-minute. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s.toString().padStart(2, '0')}s`
}

/** `YYYY-MM-DD HH:MM` in UTC — matches the design's monospace timestamp
 *  format. Times are stored UTC-aware in the DB and we deliberately don't
 *  localise them: the operator team works across PL/FR/TR, so a single
 *  neutral reference is clearer than a browser-local surprise. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => n.toString().padStart(2, '0')
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  )
}

/** Polish-locale thousands separator (narrow non-breaking space). */
export function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString('pl-PL')
}

/** File sizes, for bundles that run to gigabytes.
 *
 *  Decimal units, not binary: the number sits next to a photo count in a
 *  sentence somebody reads aloud to the Turkey team, and "1,4 GB" is the
 *  figure their file manager will show them too. Polish decimal comma, to
 *  match `formatInt` on the same line. */
export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  if (n < 1000) return `${n} B`
  const units = ['kB', 'MB', 'GB', 'TB']
  let value = n / 1000
  let unit = 0
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000
    unit += 1
  }
  return `${value.toLocaleString('pl-PL', { maximumFractionDigits: 1 })} ${units[unit]}`
}

/** Success-rate percentage with one decimal — avoids the "99.9% ≈ 100"
 *  visual surprise without being falsely precise. */
export function formatPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return `${n.toFixed(1)}%`
}

/** Polish złoty with space-separated thousands, two decimal places.
 *  E.g. 1234.5 → "1 234,50 zł". Uses `pl-PL` locale which is what the
 *  target audience reads natively. */
export function formatPLN(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString('pl-PL', {
    style: 'currency',
    currency: 'PLN',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

/** Relative time: "3 hours ago", "2 days ago", "just now".
 *  Used under page titles where a compact reference beats a full timestamp.
 *  Always computes against the current moment, not a fixed reference — call
 *  inside a component (not at module level) if live updating matters. */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const diff = Date.now() - d.getTime()
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 2) return 'just now'
  if (minutes < 60) return `${minutes} minutes ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`
  const months = Math.floor(days / 30)
  return `${months} month${months === 1 ? '' : 's'} ago`
}
