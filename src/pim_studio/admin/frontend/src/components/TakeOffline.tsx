import { useState } from 'react'
import { AlertTriangle, RotateCcw, X } from 'lucide-react'
import { api } from '../lib/api'
import type { TakedownGroup, TakedownRow } from '../lib/api'
import { useApi, useMutation } from '../lib/useApi'
import { cn } from '../lib/cn'

/**
 * Take an option off a market's website, and put it back.
 *
 * **The button is per group, not per market, and that is the whole point.**
 * There is no per-market switch on a product upstream: `is_active` removes it
 * from every channel at once, Turkey included. Availability is a catalogue
 * question, and since 2026-09-16 all four markets are served by catalogue 601
 * — so removing a product there removes it from every storefront, while
 * deleting it from one market's own catalogue usually leaves it on sale. Today
 * that makes a single group; the wording below counts markets rather than
 * assuming two.
 *
 * The groups come from the server rather than a copy of `scope.py` kept here,
 * so the day the tenant changes, these buttons change with it.
 *
 * Three things this dialog does before it will delete anything:
 *
 * - **It asks Omnitron what it would delete** and shows the answer — how many
 *   sizes, in which catalogues, and which markets those catalogues serve.
 *   Nobody should meet the consequences after pressing the button.
 * - **It names every size it cannot remove.** A size with no Omnitron id
 *   stays on sale, and "two sizes stayed up" is only useful if it says which.
 * - **It makes the person type.** The same dialog serves a group of any size;
 *   typing is the step that fails when the wrong row's button was clicked.
 *
 * What it does not claim is that the product is off the website. Omnitron
 * holding no catalogue row is not the customer no longer seeing it — the shop
 * lagged a write by two hours on 2026-09-08 — so it reports what it removed
 * and leaves liveness to the storefront crawl.
 */
export default function TakeOffline({
  modelColor,
  onClose,
  onDone,
}: {
  modelColor: string
  onClose: () => void
  onDone?: () => void
}) {
  const groups = useApi(() => api.takedownGroups(), [])
  const history = useApi(() => api.takedownHistory(modelColor), [modelColor])
  const [chosen, setChosen] = useState<TakedownGroup | null>(null)
  const [confirm, setConfirm] = useState('')
  const [reason, setReason] = useState('')
  const [result, setResult] = useState<TakedownRow | null>(null)

  const plan = useApi(
    () => (chosen ? api.takedownPlan(modelColor, chosen.key) : Promise.resolve(null)),
    [modelColor, chosen?.key],
  )
  const removing = useMutation((body: { group: string; confirm: string; reason?: string }) =>
    api.takeOffline(modelColor, body),
  )
  const restoring = useMutation((id: number) => api.putBackOnline(id))

  const phrase = groups.data?.confirmPhrase ?? 'take offline'
  const enabled = plan.data?.enabled ?? groups.data?.enabled ?? false
  const ready =
    !!chosen &&
    !!plan.data &&
    !plan.data.isEmpty &&
    enabled &&
    confirm.trim().toLowerCase() === phrase

  const run = async () => {
    if (!chosen) return
    try {
      const row = await removing.mutate({
        group: chosen.key,
        confirm: confirm.trim(),
        reason: reason.trim() || undefined,
      })
      setResult(row)
      history.refetch()
      onDone?.()
    } catch {
      /* shown from `removing.error` */
    }
  }

  const putBack = async (id: number) => {
    try {
      await restoring.mutate(id)
      setResult(null)
      history.refetch()
      onDone?.()
    } catch {
      /* shown from `restoring.error` */
    }
  }

  const past = history.data?.takedowns ?? []
  const live = past.filter((t) => t.canRestore)

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center bg-ink-900/30 p-8"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[720px] overflow-hidden rounded-md border hairline bg-paper-card shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b hairline px-5 py-4">
          <div>
            <h2 className="text-[15px] font-semibold text-ink-900">
              Take {modelColor} off the website
            </h2>
            <p className="mt-1 text-[12.5px] leading-[1.45] text-ink-500">
              Removes the option from a market's catalogue. Deactivating the
              product in Omnitron instead would pull it from every channel,
              Turkey included. Reversible from here.
            </p>
          </div>
          <button
            aria-label="Close"
            onClick={onClose}
            className="grid h-6 w-6 flex-none place-items-center rounded text-ink-400 hover:bg-paper-sunk hover:text-ink-700"
          >
            <X size={14} />
          </button>
        </div>

        {result ? (
          <Result row={result} onRestore={() => putBack(result.id)} busy={restoring.loading} />
        ) : (
          <div className="flex flex-col gap-4 px-5 py-4">
            {!enabled && !groups.loading && (
              <Notice tone="warn">
                Catalogue writes are switched off on this server. Set
                <code className="mx-1 font-mono text-[11.5px]">OMNITRON_TAKEDOWN_ENABLED</code>
                (and <code className="mx-1 font-mono text-[11.5px]">OMNITRON_WRITES_ENABLED</code>)
                to use this.
              </Notice>
            )}

            <div className="flex flex-col gap-1.5">
              <span className="eyebrow">Where</span>
              <div className="flex flex-wrap gap-2">
                {(groups.data?.groups ?? []).map((g) => (
                  <button
                    key={g.key}
                    type="button"
                    onClick={() => {
                      setChosen(g)
                      setConfirm('')
                    }}
                    className={cn(
                      'rounded-2xs border px-3 py-2 text-left text-[12.5px] transition-colors',
                      chosen?.key === g.key
                        ? 'border-brand-600 bg-brand-700/5 text-ink-900'
                        : 'hairline bg-paper-card text-ink-700 hover:bg-paper-sunk',
                    )}
                  >
                    <div className="font-mono font-semibold tracking-[0.04em]">{g.label}</div>
                    <div className="mt-0.5 text-[11px] text-ink-500">
                      {g.shared ? `${together(g.markets.length)} markets, together` : g.names[0]}
                    </div>
                  </button>
                ))}
                {groups.loading && <span className="text-[12px] text-ink-500">Loading…</span>}
              </div>
              {chosen?.shared && (
                <p className="mt-1 text-[11.5px] leading-[1.4] text-ink-500">
                  {sharedSentence(chosen)}
                </p>
              )}
            </div>

            {chosen && (
              <div className="flex flex-col gap-1.5">
                <span className="eyebrow">What this removes</span>
                {plan.loading && <span className="text-[12px] text-ink-500">Asking Omnitron…</span>}
                {plan.error && <Notice tone="danger">{plan.error.message}</Notice>}
                {plan.data && (
                  <div className="rounded-2xs border hairline bg-paper-sunk px-3 py-2.5 text-[12.5px] leading-[1.5] text-ink-700">
                    {plan.data.isEmpty ? (
                      <span>
                        {modelColor} is not in the {plan.data.markets.join(' or ')} catalogue
                        {plan.data.markets.length > 1 ? 's' : ''} — there is nothing to remove.
                      </span>
                    ) : (
                      <>
                        <div>
                          <strong className="font-semibold">{plan.data.sizeCount}</strong>{' '}
                          {plan.data.sizeCount === 1 ? 'size' : 'sizes'} across{' '}
                          <strong className="font-semibold">{plan.data.rowCount}</strong> catalogue{' '}
                          {plan.data.rowCount === 1 ? 'row' : 'rows'} (catalogue
                          {plan.data.catalogsHit.length > 1 ? 's' : ''}{' '}
                          {plan.data.catalogsHit.join(', ')}).
                        </div>
                        <div className="mt-0.5 text-ink-500">
                          Off sale in {spoken(plan.data.markets)}.
                        </div>
                      </>
                    )}
                    {plan.data.unresolvedSkus.length > 0 && (
                      <div className="mt-2 flex gap-1.5 text-[11.5px] text-danger-600">
                        <AlertTriangle size={13} className="mt-0.5 flex-none" />
                        <span>
                          {plan.data.unresolvedSkus.length}{' '}
                          {plan.data.unresolvedSkus.length === 1 ? 'size has' : 'sizes have'} no
                          Omnitron id and will stay on sale:{' '}
                          <span className="font-mono">{plan.data.unresolvedSkus.join(', ')}</span>
                        </span>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {chosen && plan.data && !plan.data.isEmpty && (
              <>
                <label className="flex flex-col gap-1">
                  <span className="eyebrow">Why (optional)</span>
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    maxLength={255}
                    placeholder="Recalled, wrong price, out of season…"
                    className="w-full rounded-xs border hairline bg-paper-card px-2.5 py-1.5 text-[12.5px] outline-none focus:border-brand-600"
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="eyebrow">
                    Type “{phrase}” to confirm
                  </span>
                  <input
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    autoComplete="off"
                    className="w-full rounded-xs border hairline bg-paper-card px-2.5 py-1.5 font-mono text-[12.5px] outline-none focus:border-brand-600"
                  />
                </label>
              </>
            )}

            {removing.error && <Notice tone="danger">{removing.error.message}</Notice>}

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={run}
                disabled={!ready || removing.loading}
                className="rounded-2xs bg-danger-600 px-3 py-1.5 text-[12px] font-medium text-white hover:opacity-90 disabled:opacity-40"
              >
                {removing.loading ? 'Removing…' : 'Take offline'}
              </button>
              <button
                type="button"
                onClick={onClose}
                className="rounded-2xs border hairline px-3 py-1.5 text-[12px] text-ink-700 hover:bg-paper-sunk"
              >
                Cancel
              </button>
            </div>

            {live.length > 0 && (
              <div className="border-t hairline pt-3">
                <span className="eyebrow">Currently removed</span>
                <div className="mt-1.5 flex flex-col gap-1.5">
                  {live.map((t) => (
                    <div
                      key={t.id}
                      className="flex items-center justify-between gap-3 rounded-2xs border hairline bg-paper-sunk px-2.5 py-1.5 text-[12px]"
                    >
                      <span className="text-ink-700">
                        <span className="font-mono font-semibold">{t.markets.join('+')}</span> ·{' '}
                        {t.removedCount} row{t.removedCount === 1 ? '' : 's'} ·{' '}
                        {t.createdAt?.slice(0, 16).replace('T', ' ')}
                        {t.createdBy ? ` · ${t.createdBy}` : ''}
                        {t.reason ? ` · ${t.reason}` : ''}
                      </span>
                      <button
                        type="button"
                        onClick={() => putBack(t.id)}
                        disabled={restoring.loading}
                        className="flex flex-none items-center gap-1 rounded-2xs border hairline bg-paper-card px-2 py-1 text-[11.5px] text-ink-700 hover:bg-paper-card/60 disabled:opacity-50"
                      >
                        <RotateCcw size={11} />
                        Put back
                      </button>
                    </div>
                  ))}
                </div>
                {restoring.error && (
                  <div className="mt-2">
                    <Notice tone="danger">{restoring.error.message}</Notice>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/** What happened, said plainly — including when it only half happened. */
function Result({
  row,
  onRestore,
  busy,
}: {
  row: TakedownRow
  onRestore: () => void
  busy: boolean
}) {
  const partial = row.status === 'partial' || row.status === 'failed'
  return (
    <div className="flex flex-col gap-3 px-5 py-4">
      <Notice tone={partial ? 'danger' : 'ok'}>
        {row.status === 'done' && (
          <>
            Removed {row.removedCount} catalogue row{row.removedCount === 1 ? '' : 's'} across{' '}
            {row.sizes.length} size{row.sizes.length === 1 ? '' : 's'}. Off sale in{' '}
            {spoken(row.markets)} once the storefront catches up — the shop has lagged a
            change by up to two hours.
          </>
        )}
        {row.status === 'partial' && (
          <>
            Only {row.removedCount} of {row.plannedCount} rows were removed. The option is
            part-way off sale, which is worse than either end — put it back and try again, or
            finish it by hand.
          </>
        )}
        {row.status === 'failed' && <>Nothing was removed. {row.errorSummary}</>}
      </Notice>

      {row.failures.length > 0 && (
        <div className="rounded-2xs border hairline bg-paper-sunk px-3 py-2 text-[12px] text-ink-700">
          <div className="eyebrow mb-1">Still on sale</div>
          {row.failures.map((f, i) => (
            <div key={i} className="font-mono text-[11.5px]">
              {f.sku} · catalogue {f.catalog}
              {f.error ? ` — ${f.error.slice(0, 120)}` : ''}
            </div>
          ))}
        </div>
      )}

      {row.canRestore && (
        <button
          type="button"
          onClick={onRestore}
          disabled={busy}
          className="flex w-fit items-center gap-1.5 rounded-2xs border hairline px-3 py-1.5 text-[12px] text-ink-700 hover:bg-paper-sunk disabled:opacity-50"
        >
          <RotateCcw size={12} />
          {busy ? 'Putting back…' : 'Put back online'}
        </button>
      )}
    </div>
  )
}

/** "PL and CZ", "PL, CZ, HU and SK" — a list as a sentence says it. */
function spoken(markets: readonly string[]): string {
  if (markets.length <= 2) return markets.join(' and ')
  return `${markets.slice(0, -1).join(', ')} and ${markets[markets.length - 1]}`
}

/** "both" for two, "all four" for more — never "both" for four markets. */
function together(count: number): string {
  if (count === 2) return 'both'
  const words = ['', 'one', 'two', 'three', 'four', 'five', 'six']
  return `all ${words[count] ?? count}`
}

/** Why the chosen markets move together. Written for any group size: the
 *  first version said "both … neither … the other", which was true of PL+CZ
 *  and false the day HU and SK joined them on catalogue 601. */
function sharedSentence(group: TakedownGroup): string {
  const two = group.markets.length === 2
  return (
    `${spoken(group.markets)} are ${two ? 'both' : 'all'} served by catalogue ` +
    `${group.sharedCatalogs.join(', ')}, so ` +
    `${two ? 'neither can be taken offline without the other' : 'none can be taken offline without the others'}. ` +
    `This removes it from ${together(group.markets.length)} storefronts.`
  )
}

function Notice({
  tone,
  children,
}: {
  tone: 'ok' | 'warn' | 'danger'
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        'rounded-2xs border px-2.5 py-2 text-[12px] leading-[1.45]',
        tone === 'ok' && 'border-[#b9d3c9] bg-ok-100 text-ok-600',
        tone === 'warn' && 'hairline bg-paper-sunk text-ink-700',
        tone === 'danger' && 'border-[#e0bdb7] bg-danger-100 text-danger-600',
      )}
    >
      {children}
    </div>
  )
}
