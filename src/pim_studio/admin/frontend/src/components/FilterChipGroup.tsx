/**
 * Single-select pill group. Wraps onto multiple lines so every option is
 * visible at once — no horizontal scroll, no hidden content. An "All" pill
 * is always prepended unless `hideAll` is set.
 */

import { cn } from '../lib/cn'

export interface FilterOption {
  value: string
  label: string
  count?: number
}

export default function FilterChipGroup({
  options,
  value,
  onChange,
  hideAll = false,
  className,
}: {
  options: FilterOption[]
  value: string | null
  onChange: (val: string | null) => void
  hideAll?: boolean
  className?: string
}) {
  const all: FilterOption[] = hideAll ? [] : [{ value: '__all__', label: 'All' }]
  const items = [...all, ...options]

  return (
    <div className={cn('flex flex-wrap gap-1.5', className)}>
      {items.map((opt) => {
        const isActive =
          opt.value === '__all__' ? value === null : value === opt.value
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value === '__all__' ? null : opt.value)}
            className={cn(
              'rounded-full px-3 py-1 text-[12.5px] font-medium transition-colors',
              'border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-700',
              isActive
                ? 'border-brand-700 bg-brand-700 text-white'
                : 'border-ink-200 bg-white text-ink-700 hover:border-ink-400 hover:bg-paper-sunk',
            )}
          >
            {opt.label}
            {opt.count !== undefined && (
              <span
                className={cn(
                  'ml-1.5 rounded-full px-1 py-0.5 font-mono text-[10px] tabular-nums',
                  isActive ? 'bg-brand-800 text-white/90' : 'bg-paper-sunk text-ink-500',
                )}
              >
                {opt.count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
