import { Fragment, useMemo } from 'react'
import { Check } from 'lucide-react'
import { cn } from '../lib/cn'
import { MARKETS } from '../lib/api'
import type { AttributeOwner } from '../lib/api'
import { OWNER_LABEL, isRequired, marketsOf, toggleRequirement } from '../lib/coverage'
import type { AttributeMarkets, CoverageAttribute } from '../lib/coverage'

/**
 * The attribute-by-market grid, shared by the two controls that need one.
 *
 * `RequirementPicker` asks "must this be present"; `ExportPicker` asks "show me
 * what it says". Different questions, identical mechanics — the same grouping,
 * the same four market columns against one ALL column, and above all the same
 * `toggleRequirement`, whose rule that ticking every market stores all four
 * rather than collapsing to an empty array is exactly the sort of thing two
 * copies would come to disagree about silently, because both readings produce a
 * plausible number of columns.
 *
 * Two things the grid has to stay honest about, because both are invisible in a
 * plain checkbox:
 *
 * **A market-agnostic attribute has one value, not four.** It gets a single ALL
 * cell rather than four that would multiply one answer into four.
 *
 * **A column nobody has read is drawn differently.** What that *means* differs
 * between the two callers, so the wording comes in as `unreadHint` rather than
 * being asserted here.
 */
export default function AttributeMarketGrid({
  attributes,
  selection,
  onChange,
  unreadHint,
}: {
  attributes: CoverageAttribute[]
  selection: AttributeMarkets[]
  onChange: (next: AttributeMarkets[]) => void
  unreadHint: (label: string) => string
}) {
  const groups = useMemo(() => {
    const byGroup = new Map<string, CoverageAttribute[]>()
    for (const attribute of attributes) {
      if (!byGroup.has(attribute.group)) byGroup.set(attribute.group, [])
      byGroup.get(attribute.group)!.push(attribute)
    }
    return [...byGroup.entries()]
  }, [attributes])

  return (
    <div className="max-h-[380px] overflow-y-auto">
      <table className="w-full border-separate border-spacing-0">
        <thead className="sticky top-0 z-10 bg-paper-card">
          <tr>
            <th className="border-b hairline px-1 py-1.5 text-left">
              <span className="eyebrow">Attribute</span>
            </th>
            <th className="w-[86px] border-b hairline px-1 py-1.5 text-left">
              <span className="eyebrow">Owner</span>
            </th>
            {MARKETS.map((market) => (
              <th
                key={market}
                className="w-[46px] border-b hairline px-1 py-1.5 text-center"
              >
                <span className="font-mono text-[10.5px] font-semibold tracking-[0.08em] text-ink-500">
                  {market}
                </span>
              </th>
            ))}
            <th className="w-[52px] border-b hairline px-1 py-1.5 text-center">
              <span className="font-mono text-[10.5px] tracking-[0.08em] text-ink-500">
                ALL
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          {groups.map(([group, rows]) => (
            // Keyed on the Fragment, not on the first row inside it. A bare
            // <> is the list item here, so a key on a child does not satisfy
            // React and the console has been saying so.
            <Fragment key={group}>
              <tr>
                <td colSpan={7} className="bg-paper-sunk px-1 py-1">
                  <span className="eyebrow">{group}</span>
                </td>
              </tr>
              {rows.map((attribute) => {
                const sourced = marketsOf(attribute.sourced)
                return (
                  <tr key={attribute.key} className="hover:bg-paper-sunk">
                    <td className="border-b hairline px-1 py-1 text-[12.5px] text-ink-900">
                      {attribute.label}
                    </td>
                    <td className="border-b hairline px-1 py-1">
                      <OwnerTag owner={attribute.owner} />
                    </td>
                    {MARKETS.map((market) => (
                      <td
                        key={market}
                        className="border-b hairline px-1 py-1 text-center"
                      >
                        {attribute.perMarket ? (
                          <Box
                            on={isRequired(selection, attribute, market)}
                            unread={!sourced.includes(market)}
                            label={`${attribute.label} in ${market}`}
                            hint={unreadHint}
                            onClick={() =>
                              onChange(toggleRequirement(selection, attribute, market))
                            }
                          />
                        ) : (
                          <span className="text-ink-200">·</span>
                        )}
                      </td>
                    ))}
                    <td className="border-b hairline px-1 py-1 text-center">
                      {attribute.perMarket ? (
                        <span className="text-ink-200">·</span>
                      ) : (
                        <Box
                          on={isRequired(selection, attribute, null)}
                          unread={!sourced.length}
                          label={attribute.label}
                          hint={unreadHint}
                          onClick={() =>
                            onChange(toggleRequirement(selection, attribute, null))
                          }
                        />
                      )}
                    </td>
                  </tr>
                )
              })}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Box({
  on,
  unread,
  label,
  hint,
  onClick,
}: {
  on: boolean
  unread: boolean
  label: string
  hint: (label: string) => string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={on}
      title={unread ? hint(label) : label}
      className={cn(
        'grid h-4 w-4 place-items-center rounded-[2px] border transition-colors',
        on
          ? 'border-brand-700 bg-brand-700 text-white'
          : 'border-ink-300 hover:border-ink-500',
        unread && !on && 'border-dashed border-ink-200',
      )}
    >
      {on ? (
        <Check size={11} strokeWidth={3} />
      ) : unread ? (
        <span className="h-px w-2 bg-ink-300" />
      ) : null}
    </button>
  )
}

const OWNER_TONE: Record<AttributeOwner, string> = {
  us: 'bg-brand-50 text-brand-900',
  partner: 'bg-info-100 text-info-600',
  system: 'bg-ink-100 text-ink-500',
}

export function OwnerTag({ owner }: { owner: AttributeOwner }) {
  return (
    <span
      className={cn(
        'inline-block rounded-xs px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.08em]',
        OWNER_TONE[owner],
      )}
    >
      {OWNER_LABEL[owner]}
    </span>
  )
}

/** How many columns a selection actually asks for.
 *
 *  Not the same as the number of attributes ticked, and it is the number that
 *  matters to both callers: for a requirement it is how many checks the
 *  definition makes, for an export it is how wide the sheet gets. */
export function columnsIn(
  selection: AttributeMarkets[],
  attributes: CoverageAttribute[],
): number {
  return selection.reduce((total, chosen) => {
    const attribute = attributes.find((a) => a.key === chosen.attribute)
    if (!attribute) return total
    if (!attribute.perMarket) return total + 1
    return total + (chosen.markets.length || MARKETS.length)
  }, 0)
}
