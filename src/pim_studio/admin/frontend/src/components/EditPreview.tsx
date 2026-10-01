import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'
import { cn } from '../lib/cn'
import { api } from '../lib/api'
import type { EditFlag, EditPreview as EditPreviewPayload, EditPreviewRow, ValueEdit } from '../lib/api'
import { useMutation } from '../lib/useApi'

/**
 * The review step every bulk change goes through — an uploaded sheet or a
 * value set across a selection. One row per cell: what it says now, what it
 * would say, and what is wrong with it. The person unticks what they do not
 * want and applies the rest; the server refuses the rows it flagged as
 * blocking whatever was ticked, so the checkboxes only ever narrow.
 *
 * Suspicious rows are shown, not hidden: a Slovak description that reads as
 * English, a value off the attribute's list. They apply if left ticked, which
 * is a decision the person makes with the flag in front of them.
 */
export default function EditPreview({
  preview,
  onApplied,
  onCancel,
}: {
  preview: EditPreviewPayload
  onApplied: (result: { applied: number; skipped: number }) => void
  onCancel: () => void
}) {
  const [unticked, setUnticked] = useState<Set<number>>(new Set())
  const apply = useMutation((edits: ValueEdit[]) => api.importsApply(edits))

  const chosen = useMemo(
    () => preview.rows.filter((r, i) => r.applicable && !unticked.has(i)),
    [preview.rows, unticked],
  )
  const warned = chosen.filter((r) => r.flags.length > 0).length

  const run = async () => {
    try {
      const out = await apply.mutate(
        chosen.map((r) => ({
          modelColor: r.modelColor,
          attribute: r.attribute,
          market: r.market,
          value: r.value,
        })),
      )
      onApplied(out)
    } catch {
      /* the mutation keeps the server's sentence in `apply.error` */
    }
  }

  const toggle = (i: number) =>
    setUnticked((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })

  return (
    <div>
      {preview.file && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b hairline bg-paper-sunk px-5 py-2.5 text-[12px] text-ink-700">
          <span>
            Read <b className="font-medium">{preview.file.sheet}</b> in{' '}
            <span className="font-mono">{preview.file.name}</span>, header on row{' '}
            {preview.file.headerRow}, {preview.file.rowsRead} rows.
          </span>
          <span className="text-ink-500">
            Columns: {preview.file.columns.join(', ') || 'none'}
          </span>
          {preview.file.unknownColumns.length > 0 && (
            <span className="flex items-center gap-1 text-warn-600">
              <AlertTriangle size={12} />
              Ignored: {preview.file.unknownColumns.join(', ')}
            </span>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-b hairline px-5 py-3">
        <span className="text-[13px] font-semibold text-ink-900">
          {chosen.length} of {preview.total} cells will be set
        </span>
        {warned > 0 && (
          <span className="text-[12px] text-warn-600">· {warned} with a warning, still ticked</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={run}
            disabled={apply.loading || chosen.length === 0}
            className="rounded-2xs bg-brand-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-brand-600 disabled:opacity-50"
          >
            {apply.loading ? 'Applying…' : `Apply ${chosen.length} as approved`}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="rounded-2xs border hairline bg-paper-card px-3 py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300"
          >
            Cancel
          </button>
        </div>
      </div>
      {apply.error && (
        <div className="border-b hairline bg-danger-100 px-5 py-2 text-[12px] text-danger-600">
          {apply.error.message}
        </div>
      )}

      <div className="max-h-[520px] overflow-auto">
        <table className="w-full border-separate border-spacing-0 text-[12.5px]">
          <thead className="sticky top-0 bg-paper-card">
            <tr>
              <Th> </Th>
              <Th>Product</Th>
              <Th>Attribute</Th>
              <Th>Market</Th>
              <Th>Now</Th>
              <Th>Would become</Th>
              <Th>Flags</Th>
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((r, i) => (
              <Row key={i} row={r} ticked={r.applicable && !unticked.has(i)} onToggle={() => toggle(i)} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Row({ row, ticked, onToggle }: { row: EditPreviewRow; ticked: boolean; onToggle: () => void }) {
  return (
    <tr className={cn('hover:bg-[#fbfbf8]', !row.applicable && 'text-ink-400')}>
      <Td>
        <input
          type="checkbox"
          checked={ticked}
          disabled={!row.applicable}
          onChange={onToggle}
          aria-label={`Apply ${row.label} ${row.market} on ${row.modelColor}`}
        />
      </Td>
      <Td>
        <Link to={`/products/${encodeURIComponent(row.modelColor)}`} className="font-mono text-brand-800 hover:underline">
          {row.modelColor}
        </Link>
      </Td>
      <Td>{row.label}</Td>
      <Td>
        <span className="font-mono text-[11px]">{row.market === '*' ? 'all' : row.market}</span>
      </Td>
      <Td>
        <span className="block max-w-[260px] truncate text-ink-500" title={row.current ?? ''}>
          {row.current ?? <span className="italic">empty</span>}
        </span>
      </Td>
      <Td>
        <span className="block max-w-[320px] truncate" title={row.value}>
          {row.value || <span className="italic text-ink-400">empty</span>}
        </span>
      </Td>
      <Td>
        <span className="flex flex-wrap gap-1">
          {row.flags.map((f) => (
            <FlagChip key={f} flag={f} />
          ))}
        </span>
      </Td>
    </tr>
  )
}

const FLAG_TEXT: Record<EditFlag, { text: string; block: boolean }> = {
  not_writable: { text: 'not writable', block: true },
  unknown_attribute: { text: 'unknown attribute', block: true },
  unknown_product: { text: 'unknown product', block: true },
  unchanged: { text: 'unchanged', block: true },
  empty: { text: 'empty', block: true },
  language_suspect: { text: 'reads as English', block: false },
  vocabulary_miss: { text: 'not on the list', block: false },
  sizes_disagree: { text: 'sizes disagree', block: true },
}

function FlagChip({ flag }: { flag: EditFlag }) {
  const meta = FLAG_TEXT[flag] ?? { text: flag, block: false }
  return (
    <span
      className={cn(
        'rounded-[2px] border px-[4px] font-mono text-[9.5px] uppercase leading-[15px] tracking-[0.08em]',
        meta.block
          ? 'border-ink-200 bg-paper-sunk text-ink-500'
          : 'border-[#ddc48a] bg-warn-100 text-warn-600',
      )}
    >
      {meta.text}
    </span>
  )
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="border-b border-ink-200 px-3 py-2 text-left font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-ink-500">
      {children}
    </th>
  )
}

function Td({ children }: { children: React.ReactNode }) {
  return <td className="border-b border-ink-100 px-3 py-1.5 align-top">{children}</td>
}
