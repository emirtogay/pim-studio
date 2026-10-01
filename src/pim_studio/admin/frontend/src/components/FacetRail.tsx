import { useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '../lib/cn'
import type { Facet, FacetKey } from '../lib/api'
import type { Scope } from '../lib/scope'

/**
 * The facet rail. Sits inside the content area, not the app sidebar — the
 * sidebar is navigation and isn't ours to use.
 *
 * Counts are the important part: every value shows how many products it would
 * return, computed with its own facet's selection excluded, so the numbers
 * answer "what happens if I add this" rather than "what is already selected".
 */
export default function FacetRail({
  facets,
  scope,
  onToggle,
}: {
  facets: Facet[]
  scope: Scope
  onToggle: (key: FacetKey, value: string) => void
}) {
  return (
    <div className="flex w-[212px] flex-none flex-col gap-4">
      {facets
        .filter((f) => f.values.length > 0)
        .map((facet) => (
          <FacetBlock
            key={facet.key}
            facet={facet}
            selected={scope[facet.key] ?? []}
            onToggle={onToggle}
          />
        ))}
    </div>
  )
}

const INITIAL_VISIBLE = 6

function FacetBlock({
  facet,
  selected,
  onToggle,
}: {
  facet: Facet
  selected: string[]
  onToggle: (key: FacetKey, value: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const overflow = facet.values.length - INITIAL_VISIBLE
  const visible = expanded ? facet.values : facet.values.slice(0, INITIAL_VISIBLE)

  return (
    <div>
      <div className="eyebrow mb-1.5 flex items-center justify-between">
        <span>{facet.label}</span>
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
              onClick={() => onToggle(facet.key, v.value)}
              className={cn(
                'group flex items-center gap-2 rounded-2xs py-[3px] pl-1 pr-1.5 text-left',
                'hover:bg-paper-sunk',
                on && 'bg-brand-50',
              )}
            >
              <span
                className={cn(
                  'grid h-3 w-3 flex-none place-items-center rounded-[2px] border',
                  on ? 'border-brand-700 bg-brand-700' : 'border-ink-300 group-hover:border-ink-400',
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
                title={v.value}
              >
                {v.value}
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
          <ChevronDown
            size={11}
            className={cn('transition-transform', expanded && 'rotate-180')}
          />
          {expanded ? 'less' : `${overflow} more`}
        </button>
      )}
    </div>
  )
}
