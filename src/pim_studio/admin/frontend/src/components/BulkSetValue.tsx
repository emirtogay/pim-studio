import { useState } from 'react'
import { X } from 'lucide-react'
import EditPreview from './EditPreview'
import { api, MARKETS } from '../lib/api'
import type { EditPreview as EditPreviewPayload, ImportTarget } from '../lib/api'
import { useApi, useMutation } from '../lib/useApi'

/**
 * Set one attribute to one value across a selection of products.
 *
 * The attribute list comes from the registry's write targets, so only what a
 * push can carry is offered. The value is previewed against every selected
 * product before anything is written — a bulk edit is the fastest way to
 * put a wrong value on 300 products, and the preview is where that is caught.
 *
 * The grid reads the nightly extract, so a value set here shows on the product
 * pages at once and in the grid after the next rebuild. The dialog says so.
 */
export default function BulkSetValue({
  modelColors,
  onClose,
  onApplied,
}: {
  modelColors: string[]
  onClose: () => void
  onApplied: (result: { applied: number; skipped: number }) => void
}) {
  const targets = useApi(() => api.importsTargets(), [])
  const [attribute, setAttribute] = useState<ImportTarget | null>(null)
  const [market, setMarket] = useState<string>('PL')
  const [value, setValue] = useState('')
  const [preview, setPreview] = useState<EditPreviewPayload | null>(null)
  const previewing = useMutation((edits: Parameters<typeof api.importsPreview>[0]) =>
    api.importsPreview(edits),
  )

  const runPreview = async () => {
    if (!attribute) return
    const m = attribute.perMarket ? market : '*'
    try {
      setPreview(
        await previewing.mutate(
          modelColors.map((mc) => ({ modelColor: mc, attribute: attribute.key, market: m, value })),
        ),
      )
    } catch {
      /* shown from `previewing.error` */
    }
  }

  const options = targets.data?.attributes ?? []

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center bg-ink-900/30 p-8" onClick={onClose}>
      <div
        className="w-full max-w-[1100px] overflow-hidden rounded-md border hairline bg-paper-card shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b hairline px-5 py-4">
          <div>
            <h2 className="text-[15px] font-semibold text-ink-900">
              Set a value on {modelColors.length} {modelColors.length === 1 ? 'product' : 'products'}
            </h2>
            <p className="mt-1 text-[12.5px] text-ink-500">
              Lands as approved on each product page at once, and in this grid after the next
              extract rebuild. Nothing reaches Omnitron until it is pushed from Imports.
            </p>
          </div>
          <button aria-label="Close" onClick={onClose} className="grid h-6 w-6 place-items-center rounded text-ink-400 hover:bg-paper-sunk hover:text-ink-700">
            <X size={14} />
          </button>
        </div>

        {!preview && (
          <div className="flex flex-col gap-3 px-5 py-4">
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1">
                <span className="eyebrow">Attribute</span>
                <select
                  value={attribute?.key ?? ''}
                  onChange={(e) => {
                    const found = options.find((o) => o.key === e.target.value) ?? null
                    setAttribute(found)
                    if (found && !found.markets.includes(market)) setMarket(found.markets[0] ?? 'PL')
                  }}
                  className="min-w-[220px] rounded-xs border hairline bg-paper-card px-2.5 py-1.5 text-[12.5px] outline-none focus:border-brand-600"
                >
                  <option value="">{targets.loading ? 'Loading…' : 'Choose…'}</option>
                  {options.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
              {attribute?.perMarket && (
                <label className="flex flex-col gap-1">
                  <span className="eyebrow">Market</span>
                  <select
                    value={market}
                    onChange={(e) => setMarket(e.target.value)}
                    className="rounded-xs border hairline bg-paper-card px-2.5 py-1.5 text-[12.5px] outline-none focus:border-brand-600"
                  >
                    {MARKETS.filter((m) => attribute.markets.includes(m)).map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <label className="flex flex-col gap-1">
              <span className="eyebrow">Value</span>
              {attribute?.longText ? (
                <textarea
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  rows={4}
                  className="w-full rounded-xs border hairline bg-paper-card px-2.5 py-2 text-[12.5px] leading-relaxed outline-none focus:border-brand-600"
                />
              ) : (
                <input
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  className="w-full rounded-xs border hairline bg-paper-card px-2.5 py-1.5 text-[12.5px] outline-none focus:border-brand-600"
                />
              )}
            </label>
            {previewing.error && (
              <div className="rounded-2xs border border-[#e0bdb7] bg-danger-100 px-2.5 py-2 text-[12px] text-danger-600">
                {previewing.error.message}
              </div>
            )}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={runPreview}
                disabled={!attribute || !value.trim() || previewing.loading}
                className="rounded-2xs bg-brand-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-brand-600 disabled:opacity-50"
              >
                {previewing.loading ? 'Checking…' : 'Preview'}
              </button>
              <button
                type="button"
                onClick={onClose}
                className="rounded-2xs border hairline bg-paper-card px-3 py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {preview && (
          <EditPreview preview={preview} onApplied={onApplied} onCancel={() => setPreview(null)} />
        )}
      </div>
    </div>
  )
}
