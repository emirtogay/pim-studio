import { cn } from '../lib/cn'
import type { AttributeState } from '../lib/api'

/**
 * The lifecycle mark for one attribute value.
 *
 * Nine conditions, nine distinct shapes — the geometry lives in `index.css`
 * under `.glyph-*`. Shape carries the state and hue only reinforces it,
 * because there are nine conditions against four status colours, brand green
 * is reserved for selection, and the matrix has to stay readable printed in
 * greyscale.
 *
 * Used on the attribute matrix, and reused at small size on product cards.
 */
export default function StateGlyph({
  state,
  className,
}: {
  state: AttributeState
  className?: string
}) {
  return (
    <span
      aria-hidden
      className={cn('glyph', `glyph-${state}`, className)}
    />
  )
}

/** Human label for a state, for tooltips, legends and screen readers. */
export const STATE_LABEL: Record<AttributeState, string> = {
  missing: 'missing',
  proposed: 'proposed',
  approved: 'approved',
  exported: 'exported',
  live: 'live',
  rejected: 'rejected',
  stale: 'stale',
  na: 'not applicable',
}

/** The inline legend above the matrix. Ordered by how far through the
 *  lifecycle each state is, not alphabetically — the sequence is the point. */
export function StateLegend({ className }: { className?: string }) {
  const order: AttributeState[] = [
    'live',
    'exported',
    'approved',
    'proposed',
    'stale',
    'rejected',
    'missing',
  ]
  return (
    <div className={cn('flex flex-wrap items-center gap-4', className)}>
      {order.map((s) => (
        <span
          key={s}
          className="flex items-center gap-1.5 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500"
        >
          <StateGlyph state={s} />
          {STATE_LABEL[s]}
        </span>
      ))}
    </div>
  )
}
