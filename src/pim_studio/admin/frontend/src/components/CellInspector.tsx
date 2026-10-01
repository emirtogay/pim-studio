import { useState } from 'react'
import { Check, Copy, X } from 'lucide-react'
import { cn } from '../lib/cn'
import { copyText } from '../lib/clipboard'
import StateGlyph, { STATE_LABEL } from './StateGlyph'
import ValuePicker from './ValuePicker'
import { MARKET_NAMES } from '../lib/api'
import type { AttributeCell, AttributeRow, Market } from '../lib/api'

/**
 * The pinned inspector for a selected matrix cell — source, owner, proposed
 * against current, and the actions. This is where accept, edit and reject
 * live; the matrix itself is never an editable grid, because 160 inline
 * inputs is how live values get overwritten by accident.
 *
 * For vocabulary-backed attributes the editor is a ValuePicker over the
 * registry's allowed values rather than a text box, so fixing a missing or
 * wrong value means choosing from the list — or explicitly adding a new
 * candidate, never silently inventing a variant.
 *
 * The value itself is shown in full here, and nowhere else. The matrix cell
 * is one line and truncates — a description runs to well over a thousand
 * characters — so this panel is the only place the whole text is readable,
 * and the only place it can be copied out of. Descriptions are shown as the
 * raw HTML they are stored as, deliberately: that is what a person is
 * reviewing and what they would paste back into a sheet.
 */
export default function CellInspector({
  row,
  market,
  cell,
  onClose,
  onSave,
  onAcceptProposal,
  onRejectProposal,
}: {
  row: AttributeRow
  market: Market | 'shared'
  cell: AttributeCell
  onClose: () => void
  /** Persist a value. Resolves when the server has it; rejects with the
   *  server's sentence otherwise. Absent in the design preview. */
  onSave?: (value: string) => Promise<void>
  /** Take the proposal into the queue. `value` is present when the person
   *  edited it first. Absent in the design preview. */
  onAcceptProposal?: (value?: string) => Promise<void>
  /** Drop the proposal and leave the live value alone. */
  onRejectProposal?: () => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(cell.value ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const marketLabel = market === 'shared' ? 'all markets' : MARKET_NAMES[market]
  const partner = row.owner === 'partner'
  const hasProposal = cell.proposedValue !== undefined
  // The registry's answer when the live overlay carried one; the owner
  // otherwise, which is the design preview's only source.
  const editable = cell.writable ?? (!partner && row.owner !== 'system')

  const save = async () => {
    const value = draft.trim()
    if (!value || !onSave) return
    setSaving(true)
    setError(null)
    try {
      await onSave(value)
      setEditing(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  /** Accepting and rejecting share this: both are one call that can fail with
   *  a sentence, and both leave the panel closed on success because the cell
   *  they describe no longer exists in the state it was opened in. */
  const answerProposal = async (run: () => Promise<void>) => {
    setSaving(true)
    setError(null)
    try {
      await run()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setSaving(false)
    }
  }

  /** Accept the text as edited in the box, rather than as proposed. */
  const acceptEdited = async () => {
    const value = draft.trim()
    if (!value || !onAcceptProposal) return
    await answerProposal(() => onAcceptProposal(value))
  }

  return (
    <div className="w-[440px] rounded-md border hairline bg-paper-card shadow-pop">
      {/* header */}
      <div className="flex items-start justify-between gap-2.5 border-b hairline px-3.5 py-[11px]">
        <div>
          <div className="text-[13px] font-semibold">
            {row.label} — {marketLabel}
          </div>
          <div className="mt-[3px] font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400">
            {market === 'shared' ? 'shared' : 'per-market'} · owner: {row.owner}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="pill-state flex items-center gap-1.5 rounded-2xs border hairline px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500">
            <StateGlyph state={cell.state} />
            {STATE_LABEL[cell.state]}
          </span>
          <button
            aria-label="Close"
            onClick={onClose}
            className="grid h-6 w-6 place-items-center rounded text-ink-400 hover:bg-paper-sunk hover:text-ink-700"
          >
            <X size={14} />
          </button>
        </div>
      </div>

      <div className="flex flex-col gap-[11px] px-3.5 py-3">
        {/* defect callout */}
        {cell.suspectReason && (
          <div className="flex items-start gap-2 rounded-2xs border border-[#e7d5aa] bg-warn-100 px-2.5 py-2 text-[12px] leading-snug text-[#6d4b0e]">
            <span className="font-mono font-semibold">!</span>
            <span>{cell.suspectReason}</span>
          </div>
        )}

        {/* current vs proposed */}
        {hasProposal && !editing && (
          <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xs border hairline bg-hairline">
            <div className="bg-paper-card px-2.5 py-2">
              <div className="mb-[5px] flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-ink-500">
                <StateGlyph state={cell.state} className="scale-[0.82]" />
                current{cell.changedAt && ` · ${cell.changedAt}`}
              </div>
              <div className={cn('text-[12px] leading-relaxed', !cell.value && 'font-mono text-[11px] text-ink-300')}>
                {cell.value ?? 'empty'}
              </div>
            </div>
            <div className="bg-[#fcfdfc] px-2.5 py-2">
              <div className="mb-[5px] flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-ink-500">
                <StateGlyph state="proposed" className="scale-[0.82]" />
                proposed{cell.proposedAt && ` · ${cell.proposedAt}`}
              </div>
              <div className="text-[12px] leading-relaxed">{cell.proposedValue}</div>
              {cell.proposedWhy && (
                <div className="mt-1.5 text-[11px] leading-snug text-ink-500">
                  {cell.proposedWhy}
                </div>
              )}
            </div>
          </div>
        )}

        {/* the value, in full — the matrix truncates and this is the only
            place it can be read whole or copied */}
        {!editing && !hasProposal && <FullValue cell={cell} />}

        {/* editor — vocabulary picker when the registry has a list, free text
            otherwise. Opened explicitly, never inline in the matrix. */}
        {editing &&
          (row.vocabulary ? (
            <ValuePicker
              options={row.vocabulary}
              current={cell.value}
              onPick={(v) => setDraft(v)}
            />
          ) : row.longText ? (
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={4}
              className="w-full rounded-xs border hairline bg-paper-card px-2.5 py-2 text-[12.5px] leading-relaxed outline-none focus:border-brand-600"
            />
          ) : (
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              className="w-full rounded-xs border hairline bg-paper-card px-2.5 py-1.5 text-[12.5px] outline-none focus:border-brand-600"
            />
          ))}

        {error && (
          <div className="rounded-2xs border border-[#e0bdb7] bg-danger-100 px-2.5 py-2 text-[12px] leading-snug text-danger-600">
            {error}
          </div>
        )}

        {/* provenance */}
        <div className="grid grid-cols-[82px_1fr] gap-x-3 gap-y-1 text-[12px]">
          {cell.source && (
            <>
              <span className="pt-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500">
                Source
              </span>
              <span className="text-ink-900">
                <span className="font-mono text-[11px] text-ink-700">{cell.source}</span>
                {cell.sourceDetail && <> · {cell.sourceDetail}</>}
              </span>
            </>
          )}
          {cell.changedBy && (
            <>
              <span className="pt-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500">
                Current from
              </span>
              <span className="text-ink-900">{cell.changedBy}</span>
            </>
          )}
          <span className="pt-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500">
            Owner
          </span>
          <span className="text-ink-900">
            {partner
              ? 'Partner team, Turkey — request only, written via product cards'
              : row.owner === 'system'
                ? 'System — derived, read-only'
                : editable
                  ? 'Us — editable here, pushed to Omnitron in the next batch'
                  : 'Us — not writable from the panel for this market'}
          </span>
        </div>
      </div>

      {/* actions */}
      <div className="flex items-center gap-2 border-t hairline px-3.5 py-2.5">
        {partner ? (
          <span className="text-[11.5px] text-ink-400">Supplied by the partner — nothing to edit here.</span>
        ) : hasProposal && !editing ? (
          <>
            <button
              disabled={saving || !onAcceptProposal}
              onClick={() => answerProposal(() => onAcceptProposal!())}
              title="Take this into the queue as your own value, to go out on the next push"
              className="rounded-2xs bg-brand-700 px-[11px] py-1.5 text-[12px] font-medium text-white hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {saving ? 'Working…' : 'Accept proposal'}
            </button>
            <button
              onClick={() => {
                setDraft(cell.proposedValue ?? '')
                setEditing(true)
              }}
              className="rounded-2xs border hairline bg-paper-card px-[11px] py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300"
            >
              Edit and accept
            </button>
            <button
              disabled={saving || !onRejectProposal}
              onClick={() => answerProposal(() => onRejectProposal!())}
              title="Drop the suggestion. The live value is left exactly as it is."
              className="rounded-2xs border hairline bg-paper-card px-[11px] py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Reject
            </button>
          </>
        ) : editing ? (
          <>
            <button
              onClick={hasProposal ? acceptEdited : save}
              disabled={saving || !draft.trim() || draft.trim() === (cell.value ?? '')}
              className="rounded-2xs bg-brand-700 px-[11px] py-1.5 text-[12px] font-medium text-white hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {saving ? 'Saving…' : hasProposal ? 'Accept as edited' : 'Save as approved'}
            </button>
            <button
              onClick={() => {
                setEditing(false)
                setError(null)
              }}
              className="rounded-2xs border hairline bg-paper-card px-[11px] py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300"
            >
              Cancel
            </button>
          </>
        ) : editable ? (
          <button
            onClick={() => setEditing(true)}
            className="rounded-2xs bg-brand-700 px-[11px] py-1.5 text-[12px] font-medium text-white hover:bg-brand-600"
          >
            {cell.state === 'missing' ? 'Set value' : 'Change value'}
          </button>
        ) : (
          <span className="text-[11.5px] text-ink-400">Derived — nothing to edit.</span>
        )}
        {draft && !editing && draft !== (cell.value ?? '') && (
          <span className="ml-auto text-[11.5px] text-ink-400">
            picked: <span className="font-mono text-[11px] text-ink-700">{draft}</span>
          </span>
        )}
      </div>
    </div>
  )
}


/**
 * The cell's value, whole, with a way to get it out.
 *
 * Scrolls rather than truncates: the point of opening the inspector is to see
 * everything. Selectable text as well as a copy button, because somebody
 * wanting one paragraph out of a description should not have to take all of
 * it. The character count is shown once the value is longer than a line — it
 * is the number that tells you whether a description is a real one or a
 * placeholder, and it costs nothing to render.
 */
function FullValue({ cell }: { cell: AttributeCell }) {
  const [copied, setCopied] = useState(false)
  const value = cell.value ?? ''

  const copy = async () => {
    if (await copyText(value)) {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    }
  }

  if (cell.perSize) {
    return (
      <div className="rounded-2xs border hairline bg-paper-sunk px-2.5 py-2 text-[12px] leading-snug text-ink-500">
        Held per size, and the sizes do not agree — the article code contains
        the size, so every one of them differs. The sizes table below has each
        value. Nothing is shown here rather than one size's value standing in
        for the option.
      </div>
    )
  }

  if (!value) {
    return (
      <div className="rounded-2xs border hairline bg-paper-sunk px-2.5 py-2 font-mono text-[11px] text-ink-400">
        empty — no value is held for this cell
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-2xs border hairline">
      <div className="flex items-center gap-2 border-b hairline bg-paper-sunk px-2.5 py-1">
        <span className="eyebrow">Value</span>
        {value.length > 60 && (
          <span className="font-mono text-[10px] tabular-nums text-ink-400">
            {value.length.toLocaleString('pl-PL')} characters
          </span>
        )}
        <button
          type="button"
          onClick={copy}
          className="ml-auto flex items-center gap-1 rounded-2xs px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500 hover:bg-paper-card hover:text-ink-900"
        >
          {copied ? <Check size={11} className="text-brand-700" /> : <Copy size={11} />}
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <div
        className={cn(
          'max-h-[220px] select-text overflow-y-auto whitespace-pre-wrap break-words bg-paper-card px-2.5 py-2 leading-relaxed',
          cell.code ? 'font-mono text-[11.5px]' : 'text-[12.5px]',
        )}
      >
        {value}
      </div>
    </div>
  )
}
