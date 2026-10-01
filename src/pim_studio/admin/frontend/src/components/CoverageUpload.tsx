import { useRef, useState } from 'react'
import { AlertTriangle, FileSpreadsheet, Upload, X } from 'lucide-react'
import { cn } from '../lib/cn'
import { api } from '../lib/api'
import type { CodeResolution } from '../lib/api'

/**
 * Scope the analysis to a list of ModelColors somebody already has.
 *
 * The list is always to hand and never in our format: a column out of the
 * order file, a sheet the Turkey team sent back, forty codes in an email. So
 * this accepts a file or a paste and resolves both against the options we hold
 * — dots, underscores, spaces and a size suffix all match, because the five
 * spellings of a ModelColor are a fact of this business rather than a mistake.
 *
 * **Resolution is shown before it is used.** Which column was read, how many
 * codes matched, and every one that did not. An upload that quietly reported
 * on 380 of 500 codes would look exactly like one that worked, and the missing
 * 120 are usually the interesting ones — products nobody has opened yet.
 */
export default function CoverageUpload({
  resolution,
  onResolved,
  onClear,
}: {
  resolution: CodeResolution | null
  onResolved: (resolution: CodeResolution) => void
  onClear: () => void
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const [showMissing, setShowMissing] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  async function run(work: () => Promise<CodeResolution>) {
    setBusy(true)
    setError('')
    try {
      onResolved(await work())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not read that.')
    } finally {
      setBusy(false)
    }
  }

  function onFile(file: File | undefined) {
    if (!file) return
    void run(() => api.resolveFile(file))
  }

  if (resolution) {
    return (
      <div className="rounded-md border border-brand-600 bg-brand-50 px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <FileSpreadsheet size={14} className="text-brand-700" />
          <span className="font-mono text-[12px] tabular-nums text-brand-900">
            {resolution.matched.toLocaleString('pl-PL')}
          </span>
          <span className="text-[12.5px] text-ink-700">
            {resolution.matched === 1 ? 'option' : 'options'} from your list
          </span>
          {resolution.column && (
            <span className="text-[12px] text-ink-500">
              · read from{' '}
              <b className="font-medium text-ink-700">{resolution.column}</b>
              {resolution.sheet ? ` in ${resolution.sheet}` : ''}
            </span>
          )}
          <button
            type="button"
            onClick={onClear}
            className="ml-auto flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-500 hover:text-danger-600"
          >
            <X size={11} />
            use whole catalogue
          </button>
        </div>

        {resolution.notFound.length > 0 && (
          <div className="mt-2 border-t border-brand-600/25 pt-2">
            <button
              type="button"
              onClick={() => setShowMissing((v) => !v)}
              className="flex items-center gap-1.5 text-[12px] text-warn-600 hover:underline"
            >
              <AlertTriangle size={12} />
              {resolution.notFound.length} code
              {resolution.notFound.length === 1 ? '' : 's'} matched no product we
              hold — excluded from every number below
            </button>
            {showMissing && (
              <div className="mt-1.5 max-h-28 overflow-y-auto rounded-2xs bg-paper-card p-2">
                <div className="flex flex-wrap gap-1">
                  {resolution.notFound.map((code) => (
                    <span
                      key={code}
                      className="rounded-xs bg-warn-100 px-1.5 py-0.5 font-mono text-[11px] text-warn-600"
                    >
                      {code}
                    </span>
                  ))}
                </div>
                <p className="mt-1.5 text-[11.5px] text-ink-500">
                  Matching ignores dots, underscores and a size suffix, so these
                  are usually products nobody has opened in Omnitron yet, or
                  products from a catalogue we do not sync.
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    )
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragging(false)
        onFile(e.dataTransfer.files?.[0])
      }}
      className={cn(
        'rounded-md border bg-paper-card px-3 py-2.5 shadow-card transition-colors',
        dragging ? 'border-brand-600 bg-brand-50' : 'hairline',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow">Scope to a list</span>

        <input
          ref={fileInput}
          type="file"
          accept=".xlsx,.xlsm,.xls,.csv,.tsv,.txt"
          className="hidden"
          onChange={(e) => {
            onFile(e.target.files?.[0])
            e.target.value = ''
          }}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => fileInput.current?.click()}
          className="flex items-center gap-1.5 rounded-2xs border hairline px-2 py-1 text-[12px] font-medium text-ink-700 hover:border-ink-300 disabled:opacity-50"
        >
          <Upload size={12} />
          {busy ? 'Reading…' : 'Upload .xlsx / .xls / .csv'}
        </button>

        <span className="text-[12px] text-ink-400">or paste codes</span>
        <input
          type="text"
          value={text}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && text.trim()) void run(() => api.resolveCodes(text))
          }}
          placeholder="L3558.132, TH2568_144, 60KFA0791.83J.T4…"
          aria-label="Paste ModelColors"
          className="min-w-[240px] flex-1 rounded-2xs border hairline bg-paper px-2 py-1 font-mono text-[12px] text-ink-900 placeholder:font-sans placeholder:text-ink-400 focus:border-brand-600 focus:outline-none"
        />
        {text.trim() && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void run(() => api.resolveCodes(text))}
            className="rounded-2xs bg-brand-700 px-2.5 py-1 text-[12px] font-medium text-white hover:bg-brand-600 disabled:opacity-50"
          >
            Use these
          </button>
        )}
      </div>

      {error && (
        <p className="mt-2 flex items-start gap-1.5 text-[12px] text-danger-600">
          <AlertTriangle size={12} className="mt-0.5 flex-none" />
          {error}
        </p>
      )}
    </div>
  )
}
