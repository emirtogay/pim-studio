import { Loader2 } from 'lucide-react'
import { cn } from '../lib/cn'
import type { RunStatus } from '../lib/api'

/**
 * Colored status pill for scrape-run rows. The dot + text colour pairing
 * matches the style guide's four semantic palettes (ok / warn / danger /
 * info). "Running" is `info` plus an inline spinner instead of the dot,
 * since a static dot beside running text reads as done.
 */
export default function StatusPill({ status }: { status: RunStatus }) {
  const meta = STATUS_META[status]
  return (
    <span
      className={cn(
        'inline-flex h-5 items-center gap-1 rounded-[3px] px-1.5 font-mono text-[10.5px] font-semibold',
        meta.pill,
      )}
    >
      {status === 'running' ? (
        <Loader2 size={10} className="animate-spin" />
      ) : (
        <span className={cn('h-[7px] w-[7px] shrink-0 rounded-full', meta.dot)} />
      )}
      {status}
    </span>
  )
}

const STATUS_META: Record<
  RunStatus,
  { pill: string; dot: string }
> = {
  success: { pill: 'bg-ok-100 text-ok-600', dot: 'bg-ok-600' },
  partial: { pill: 'bg-warn-100 text-warn-600', dot: 'bg-warn-600' },
  failed: { pill: 'bg-danger-100 text-danger-600', dot: 'bg-danger-600' },
  running: { pill: 'bg-info-100 text-info-600', dot: 'bg-info-600' },
  // Neutral grey — a skip is neither good nor bad, just "deliberately nothing".
  skipped: { pill: 'bg-ink-100 text-ink-600', dot: 'bg-ink-400' },
}
