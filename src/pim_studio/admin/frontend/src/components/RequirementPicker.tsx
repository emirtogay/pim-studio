import { useMemo, useState } from 'react'
import { ChevronDown, RotateCcw } from 'lucide-react'
import { cn } from '../lib/cn'
import AttributeMarketGrid, { columnsIn } from './AttributeMarketGrid'
import type {
  CoverageAttribute,
  ProfileSpec,
  RequirementSpec,
} from '../lib/coverage'

/**
 * What counts as complete, chosen rather than assumed.
 *
 * "Ready to launch in Poland" and "the product card is filled in" are
 * different sets of attributes, and next season there will be a third. So the
 * definition is an input to the analysis, not a constant inside it — presets
 * for the questions worth asking often, and a per-market grid for the ones
 * nobody wrote a preset for.
 *
 * The grid itself lives in `AttributeMarketGrid`, shared with the export
 * picker. What stays here is the part that is about requirements specifically:
 * the presets, and the promise that a market nobody has read is still tickable
 * and comes back as `unknown` rather than as a gap. Hiding those markets would
 * make the requirement look impossible to state; letting them look normal would
 * make the result look like a failure.
 */
export default function RequirementPicker({
  attributes,
  presets,
  requirements,
  activePreset,
  onChange,
  onPreset,
}: {
  attributes: CoverageAttribute[]
  presets: ProfileSpec[]
  requirements: RequirementSpec[]
  activePreset: string
  onChange: (next: RequirementSpec[]) => void
  onPreset: (preset: ProfileSpec) => void
}) {
  const [open, setOpen] = useState(false)

  const chosen = requirements.length
  // How many cells the definition actually asks for — the number that decides
  // how much work "incomplete" represents, and not the same as the number of
  // attributes ticked.
  const cells = useMemo(
    () => columnsIn(requirements, attributes),
    [requirements, attributes],
  )

  return (
    <div className="rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
        <span className="eyebrow">Complete means</span>

        {presets.map((preset) => (
          <button
            key={preset.key}
            type="button"
            onClick={() => onPreset(preset)}
            title={preset.blurb}
            className={cn(
              'rounded-2xs border px-2 py-1 text-[12px]',
              activePreset === preset.key
                ? 'border-brand-600 bg-brand-50 text-brand-900'
                : 'hairline bg-paper-card text-ink-700 hover:border-ink-300',
            )}
          >
            {preset.label}
          </button>
        ))}

        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={cn(
            'ml-auto flex items-center gap-1.5 rounded-2xs border px-2 py-1 text-[12px]',
            activePreset === ''
              ? 'border-brand-600 bg-brand-50 text-brand-900'
              : 'hairline text-ink-700 hover:border-ink-300',
          )}
        >
          {activePreset === '' ? 'Custom' : 'Choose attributes'}
          <span className="font-mono text-[10.5px] tabular-nums text-ink-400">
            {chosen} attr · {cells} checks
          </span>
          <ChevronDown
            size={12}
            className={cn('transition-transform', open && 'rotate-180')}
          />
        </button>
      </div>

      {open && (
        <div className="border-t hairline px-3 py-3">
          <div className="mb-2 flex items-center justify-between">
            <p className="max-w-prose text-[12px] text-ink-500">
              Tick what a product must have for this analysis. A struck-through
              market has never been read from any source — asking for it is
              fine, and the answer comes back as <b className="font-medium">unknown</b>{' '}
              rather than as a gap.
            </p>
            {chosen > 0 && (
              <button
                type="button"
                onClick={() => onChange([])}
                className="flex flex-none items-center gap-1 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 hover:text-danger-600"
              >
                <RotateCcw size={11} />
                clear
              </button>
            )}
          </div>

          <AttributeMarketGrid
            attributes={attributes}
            selection={requirements}
            onChange={onChange}
            unreadHint={(label) =>
              `${label} — never read from any source, so this reports as unknown`
            }
          />
        </div>
      )}
    </div>
  )
}

export { OwnerTag } from './AttributeMarketGrid'
