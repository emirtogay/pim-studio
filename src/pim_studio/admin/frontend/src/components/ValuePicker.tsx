import { useMemo, useState } from 'react'
import { Check, Plus, Search } from 'lucide-react'
import { cn } from '../lib/cn'
import type { VocabularyOption } from '../lib/api'

/**
 * Picker for a vocabulary-backed attribute.
 *
 * Most classification attributes draw from short closed lists — sleeve type
 * has three values, fit ten, colour filter nine. Typing those by hand is how
 * `OUTLET` and `Outlet` end up as two different seasons, so the inspector
 * offers the list instead of a text box.
 *
 * The list is registry data, not a hardcoded union, because it grows. Values
 * seen in an export or a masterdata file but not yet blessed appear as
 * **candidates**, ranked below approved ones and labelled — so a typo used
 * once is visibly different from the value everyone uses. Anything genuinely
 * new can be added here and lands as a candidate for someone to promote.
 *
 * Usage counts are the tell. A candidate on 400 products is a value the
 * registry is missing; a candidate on one is probably a mistake.
 */
export default function ValuePicker({
  options,
  current,
  onPick,
}: {
  options: VocabularyOption[]
  current?: string
  onPick: (value: string) => void
}) {
  const [query, setQuery] = useState('')

  const { approved, candidates, canAdd } = useMemo(() => {
    const q = query.trim().toLowerCase()
    const match = (o: VocabularyOption) => !q || o.value.toLowerCase().includes(q)
    const exact = options.some((o) => o.value.toLowerCase() === q)
    return {
      approved: options.filter((o) => o.status === 'approved' && match(o)),
      candidates: options.filter((o) => o.status === 'candidate' && match(o)),
      canAdd: q.length > 0 && !exact,
    }
  }, [options, query])

  return (
    <div className="rounded-xs border hairline">
      <div className="flex items-center gap-2 border-b hairline px-2.5 py-1.5">
        <Search size={13} className="shrink-0 text-ink-400" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search values, or type a new one"
          className="w-full bg-transparent text-[12.5px] outline-none placeholder:text-ink-300"
        />
      </div>

      <div className="max-h-[184px] overflow-y-auto py-1">
        {approved.map((o) => (
          <Option key={o.value} option={o} current={current} onPick={onPick} />
        ))}

        {candidates.length > 0 && (
          <>
            <div className="mt-1 border-t hairline px-2.5 pb-1 pt-2">
              <span className="eyebrow">Seen in data, not yet approved</span>
            </div>
            {candidates.map((o) => (
              <Option key={o.value} option={o} current={current} onPick={onPick} />
            ))}
          </>
        )}

        {approved.length === 0 && candidates.length === 0 && !canAdd && (
          <div className="px-2.5 py-3 text-[12px] text-ink-400">No matching values.</div>
        )}

        {canAdd && (
          <button
            type="button"
            onClick={() => onPick(query.trim())}
            className="mt-1 flex w-full items-center gap-2 border-t hairline px-2.5 py-2 text-left hover:bg-paper-sunk"
          >
            <Plus size={13} className="shrink-0 text-info-600" />
            <span className="text-[12.5px] text-ink-700">
              Add <span className="font-mono text-[12px] text-ink-900">{query.trim()}</span>
            </span>
            <span className="ml-auto font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-400">
              new candidate
            </span>
          </button>
        )}
      </div>
    </div>
  )
}

function Option({
  option,
  current,
  onPick,
}: {
  option: VocabularyOption
  current?: string
  onPick: (value: string) => void
}) {
  const active = option.value === current
  return (
    <button
      type="button"
      onClick={() => onPick(option.value)}
      className={cn(
        'flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-paper-sunk',
        active && 'bg-brand-50',
      )}
    >
      <span className="w-3.5 shrink-0">
        {active && <Check size={13} className="text-brand-700" />}
      </span>
      <span
        className={cn(
          'truncate font-mono text-[12px]',
          option.status === 'candidate' ? 'text-ink-700' : 'text-ink-900',
        )}
      >
        {option.value}
      </span>
      {option.status === 'candidate' && (
        <span className="ml-auto shrink-0 rounded-[2px] border border-[#ddc48a] bg-warn-100 px-[3px] font-mono text-[9px] uppercase leading-[13px] tracking-[0.08em] text-warn-600">
          candidate
        </span>
      )}
      {option.usageCount !== undefined && (
        <span
          className={cn(
            'shrink-0 font-mono text-[10px] tabular-nums text-ink-400',
            option.status === 'candidate' ? 'ml-1.5' : 'ml-auto',
          )}
        >
          {option.usageCount.toLocaleString()}
        </span>
      )}
    </button>
  )
}
