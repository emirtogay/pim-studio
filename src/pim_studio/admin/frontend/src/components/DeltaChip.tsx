/**
 * Small coloured chip showing movement since a baseline.
 *
 * Two modes. `pct` is a percentage delta, written for the price app's
 * inheritance: negative means a price drop, which is good, so it is green.
 * `count` is an absolute change — "+84 live", "−12 blocked" — where whether up
 * is good depends entirely on what is being counted, so the caller says which
 * with `good`.
 *
 * Zero renders neutral grey rather than green or red, and null renders
 * nothing at all. That distinction is load-bearing on the Overview page: "no
 * movement this week" and "we have no baseline to compare against" look
 * identical on a dashboard and mean opposite things.
 */

export default function DeltaChip({
  pct,
  count,
  good = 'down',
  className,
}: {
  /** A percentage delta. Negative is treated as good, per the price app. */
  pct?: number | null
  /** An absolute delta. Direction is judged by `good`. */
  count?: number | null
  /** Which direction counts as an improvement. Ignored in `pct` mode. */
  good?: 'up' | 'down'
  className?: string
}) {
  const usingCount = count !== null && count !== undefined
  const value = usingCount ? count : pct
  if (value === null || value === undefined) return null

  const label = usingCount
    ? `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toLocaleString('pl-PL')}`
    : value < 0
      ? `↓ ${Math.abs(value).toFixed(1)}%`
      : value > 0
        ? `↑ ${Math.abs(value).toFixed(1)}%`
        : `${value.toFixed(1)}%`

  // `.chip-drop` is the green one and `.chip-rise` the red one — named for the
  // price move that earned them, so the mapping is by *improvement*, not by
  // sign. In `pct` mode down is good, which is how they already read.
  const improving = usingCount
    ? good === 'up'
      ? value > 0
      : value < 0
    : value < 0
  const worsening = usingCount
    ? good === 'up'
      ? value < 0
      : value > 0
    : value > 0

  const chipClass = improving ? 'chip-drop' : worsening ? 'chip-rise' : ''
  const neutralStyle = !chipClass
    ? {
        display: 'inline-flex',
        alignItems: 'center',
        fontFamily: '"IBM Plex Mono",ui-monospace,monospace',
        fontVariantNumeric: 'tabular-nums',
        fontSize: 12,
        fontWeight: 700,
        height: 22,
        padding: '0 8px',
        borderRadius: 4,
        background: '#e7e9e5',
        color: '#585d59',
        whiteSpace: 'nowrap' as const,
      }
    : undefined

  return (
    <span className={`${chipClass} ${className ?? ''}`} style={neutralStyle}>
      {label}
    </span>
  )
}
