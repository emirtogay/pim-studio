/**
 * Imports — values on their way into Omnitron.
 *
 * Three questions, top to bottom: what is waiting to go out, what went out
 * and has not been confirmed, and what every push so far did. The page never
 * writes on its own: a push is an explicit step with the numbers in front of
 * the person pressing it, and the server refuses it with a sentence when the
 * configuration, the selection or the network says no.
 *
 * A batch that `halted` is the one row here that matters more than the rest.
 * It means a write moved something it did not send — a key of another store,
 * or the Turkish copy of a localizable one — and the run stopped before the
 * next product. It stays red until somebody has looked.
 */
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  Send,
  Trash2,
  Upload,
} from 'lucide-react'
import EditPreview from '../components/EditPreview'
import PageContainer from '../components/PageContainer'
import StatusPill from '../components/StatusPill'
import { api } from '../lib/api'
import type {
  EditPreview as EditPreviewPayload,
  ImportsPending,
  PendingValueRow,
  ProposalsWaiting,
  PushBatchDetail,
  PushBatchRow,
  PushBatchStatus,
  RunStatus,
  WaitingProposal,
} from '../lib/api'
import { cn } from '../lib/cn'
import { formatDateTime, formatInt, formatRelative } from '../lib/format'
import { useApi, useMutation } from '../lib/useApi'

/** Poll while a batch is open so the person watching sees it finish. */
const POLL_MS = 8_000

/** Whether the proposals queue is collapsed. Per browser, not per account. */
const PROPOSALS_HIDDEN_KEY = 'pim.imports.proposalsHidden'

export default function Imports() {
  // One fetch each; the poll interval follows whether a batch is open, and
  // changing it re-runs the effect rather than mounting a second fetcher.
  const [polling, setPolling] = useState(false)
  const pending = useApi(() => api.importsPending(), [], { pollMs: polling ? POLL_MS : 0 })
  const batches = useApi(() => api.importsBatches(20), [], { pollMs: polling ? POLL_MS : 0 })
  const [proposalMarket, setProposalMarket] = useState<string | null>(null)
  // Collapsed across navigations, per browser. This is a view preference and
  // nobody else's business, so it does not belong in `module_user_state` with
  // the things the server has to know. Every access is guarded: a private
  // window or blocked site data makes `localStorage` throw on read as well as
  // write, and a page that will not load because of a remembered toggle is a
  // worse bug than a toggle that forgets.
  const [proposalsHidden, setProposalsHidden] = useState(() => {
    try {
      return localStorage.getItem(PROPOSALS_HIDDEN_KEY) === '1'
    } catch {
      return false
    }
  })
  const hideProposals = (v: boolean) => {
    setProposalsHidden(v)
    try {
      localStorage.setItem(PROPOSALS_HIDDEN_KEY, v ? '1' : '0')
    } catch {
      /* not remembering is fine; not rendering is not */
    }
  }
  const proposals = useApi(
    () =>
      api.importsProposals({
        limit: 100,
        ...(proposalMarket ? { market: proposalMarket } : {}),
      }),
    [proposalMarket],
  )
  useEffect(() => {
    setPolling(pending.data?.openBatch != null)
  }, [pending.data])
  const data = pending.data
  const refetch = () => {
    pending.refetch()
    batches.refetch()
    proposals.refetch()
  }

  return (
    <PageContainer>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-[28px] font-medium tracking-tight text-ink-900">
            Imports
          </h1>
          <p className="mt-2 font-mono text-[12px] text-ink-500">
            Values on their way into Omnitron
          </p>
        </div>
        <button
          type="button"
          onClick={refetch}
          className="inline-flex items-center gap-1.5 rounded-[3px] border hairline bg-paper-card px-2.5 py-1.5 font-mono text-[11px] text-ink-700 hover:bg-paper"
        >
          <RefreshCw size={12} />
          Refresh
        </button>
      </div>

      {pending.error && (
        <div className="mt-6 rounded-md border border-[#e0bdb7] bg-danger-100 px-4 py-3 text-[12.5px] text-danger-600">
          {pending.error.message}
        </div>
      )}

      {pending.loading && !data && (
        <div className="mt-8 h-24 animate-pulse rounded-md border hairline bg-paper-card" />
      )}

      <UploadCard onApplied={refetch} />

      {data && (
        <>
          <PendingCard data={data} onStarted={refetch} />
          {data.exported.length > 0 && <UnconfirmedCard rows={data.exported} />}
        </>
      )}

      <ProposalsCard
        data={proposals.data}
        loading={proposals.loading}
        market={proposalMarket}
        onMarket={setProposalMarket}
        onAnswered={refetch}
        hidden={proposalsHidden}
        onHidden={hideProposals}
      />

      <HistoryCard rows={batches.data?.rows ?? []} loading={batches.loading} />
    </PageContainer>
  )
}

// ── an edited sheet ────────────────────────────────────────────────────────

function UploadCard({ onApplied }: { onApplied: () => void }) {
  const [preview, setPreview] = useState<EditPreviewPayload | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const onFile = async (file: File | undefined) => {
    if (!file) return
    setBusy(true)
    setError('')
    setDone('')
    try {
      setPreview(await api.importsUpload(file))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not read that file.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragging(false)
        void onFile(e.dataTransfer.files?.[0])
      }}
      className={cn(
        'mt-6 rounded-md border bg-paper-card shadow-card transition-colors',
        dragging ? 'border-brand-600 bg-brand-50' : 'hairline',
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
        <div>
          <h2 className="eyebrow">Upload an edited sheet</h2>
          <p className="mt-1 text-[12.5px] text-ink-500">
            Download a Values or SKU Detail sheet from Coverage, change the cells you want,
            and drop it here. You see every cell it would change before anything is set;
            a blank cell means leave it.
          </p>
        </div>
        <div>
          <input
            ref={fileInput}
            type="file"
            accept=".xlsx,.xlsm,.csv,.tsv,.txt"
            className="hidden"
            onChange={(e) => {
              void onFile(e.target.files?.[0])
              e.target.value = ''
            }}
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => fileInput.current?.click()}
            className="inline-flex items-center gap-1.5 rounded-2xs border hairline bg-paper-card px-3 py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300 disabled:opacity-50"
          >
            <Upload size={13} />
            {busy ? 'Reading…' : 'Choose .xlsx or .csv'}
          </button>
        </div>
      </div>
      {error && (
        <div className="border-t hairline bg-danger-100 px-5 py-2.5 text-[12.5px] text-danger-600">
          {error}
        </div>
      )}
      {done && (
        <div className="border-t hairline bg-ok-100 px-5 py-2.5 text-[12.5px] text-ok-600">{done}</div>
      )}
      {preview && (
        <div className="border-t hairline">
          <EditPreview
            preview={preview}
            onCancel={() => setPreview(null)}
            onApplied={(out) => {
              setPreview(null)
              setDone(
                `${out.applied} value${out.applied === 1 ? '' : 's'} set as approved` +
                  (out.skipped ? `, ${out.skipped} left as they were` : '') +
                  '. They are listed below, waiting to go out.',
              )
              onApplied()
            }}
          />
        </div>
      )}
    </section>
  )
}

// ── waiting to go out ──────────────────────────────────────────────────────

/** A row's identity, and the key the selection is held by. */
const cellKey = (r: PendingValueRow) => `${r.modelColor}\u0000${r.attribute}\u0000${r.market}`

function PendingCard({ data, onStarted }: { data: ImportsPending; onStarted: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [note, setNote] = useState('')
  const [started, setStarted] = useState<string | null>(null)
  // Unticked rather than ticked: everything waiting goes out by default, so
  // pressing Push without touching anything behaves as it always did.
  const [unticked, setUnticked] = useState<Set<string>>(new Set())
  const push = useMutation(
    (body: Parameters<typeof api.importsPush>[0]) => api.importsPush(body),
  )

  const chosen = data.approved.filter((r) => !unticked.has(cellKey(r)))
  const all = chosen.length === data.approved.length
  const sizes = chosen.reduce((n, r) => n + r.sizes, 0)
  const toggle = (r: PendingValueRow) =>
    setUnticked((prev) => {
      const next = new Set(prev)
      const k = cellKey(r)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })

  const start = async () => {
    try {
      const out = await push.mutate({
        note: note.trim() || undefined,
        // Omitted when everything is ticked, so the server sees "the whole
        // queue" rather than a list that could go stale between render and press.
        ...(all
          ? {}
          : {
              values: chosen.map((r) => ({
                modelColor: r.modelColor,
                attribute: r.attribute,
                market: r.market,
              })),
            }),
      })
      setStarted(
        `Batch #${out.batchId} started: ${formatInt(out.items)} writes across ${formatInt(out.skus)} sizes of ${formatInt(out.products)} products.`,
      )
      setConfirming(false)
      onStarted()
    } catch {
      /* the mutation keeps the server's sentence in `push.error` */
    }
  }

  return (
    <section className="mt-6 rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b hairline px-5 py-4">
        <div>
          <h2 className="eyebrow">Waiting to go out</h2>
          <p className="mt-1 text-[12.5px] text-ink-500">
            Values set in the panel and not yet in Omnitron. Each is written to its
            market&rsquo;s own key, under its market&rsquo;s locale, once per size.
          </p>
        </div>
        <button
          type="button"
          disabled={!data.canPush || confirming || chosen.length === 0}
          onClick={() => setConfirming(true)}
          title={data.canPush ? undefined : data.reason}
          className="inline-flex items-center gap-1.5 rounded-2xs bg-brand-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Send size={13} />
          {all
            ? 'Push to Omnitron'
            : `Push ${chosen.length} of ${data.approved.length}`}
        </button>
      </div>

      {!data.canPush && (
        <div className="flex items-start gap-2 border-b hairline bg-warn-100 px-5 py-3 text-[12.5px] leading-snug text-[#6d4b0e]">
          <AlertTriangle size={14} className="mt-px shrink-0" />
          <span>{data.reason}</span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-px border-b hairline bg-hairline sm:grid-cols-4">
        <Stat label="values" value={data.approvedCount} />
        <Stat label="products" value={data.productCount} />
        <Stat label="sizes to write" value={data.itemCount} />
        <Stat label="requests, roughly" value={data.requestEstimate} />
      </div>

      {confirming && (
        <div className="border-b hairline bg-[#fcfdfc] px-5 py-4">
          <div className="text-[13px] font-semibold text-ink-900">
            {all ? (
              <>
                Write {formatInt(data.itemCount)} values to{' '}
                {formatInt(data.skuCount)} sizes across{' '}
                {formatInt(data.productCount)} products?
              </>
            ) : (
              <>
                Write {formatInt(chosen.length)} selected{' '}
                {chosen.length === 1 ? 'value' : 'values'} to {formatInt(sizes)} sizes
                across {formatInt(new Set(chosen.map((r) => r.modelColor)).size)}{' '}
                products? The other {formatInt(data.approved.length - chosen.length)}{' '}
                stay in the queue.
              </>
            )}
          </div>
          <ul className="mt-2 flex list-disc flex-col gap-1 pl-5 text-[12.5px] leading-relaxed text-ink-700">
            <li>
              Every size is read first. One Omnitron changed since the value was set is
              skipped, not overwritten.
            </li>
            <li>
              Each write carries one market&rsquo;s keys under that market&rsquo;s locale.
              The Turkish, Russian and Kazakh keys and the shared description are never
              in a payload.
            </li>
            <li>
              Every size is read back under its market, the default view and{' '}
              <span className="font-mono">tr-tr</span>. If anything the push did not send
              moved, the batch halts before the next product.
            </li>
          </ul>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Note for the history, optional"
              maxLength={255}
              className="w-72 rounded-xs border hairline bg-paper-card px-2.5 py-1.5 text-[12.5px] outline-none focus:border-brand-600"
            />
            <button
              type="button"
              onClick={start}
              disabled={push.loading}
              className="rounded-2xs bg-brand-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-brand-600 disabled:opacity-50"
            >
              {push.loading ? 'Starting…' : 'Yes, push'}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-2xs border hairline bg-paper-card px-3 py-1.5 text-[12px] font-medium text-ink-700 hover:border-ink-300"
            >
              Cancel
            </button>
          </div>
          {push.error && (
            <div className="mt-3 rounded-2xs border border-[#e0bdb7] bg-danger-100 px-2.5 py-2 text-[12px] text-danger-600">
              {push.error.message}
            </div>
          )}
        </div>
      )}

      {started && (
        <div className="border-b hairline bg-ok-100 px-5 py-3 text-[12.5px] text-ok-600">
          {started}
        </div>
      )}

      {data.openBatch && (
        <div className="border-b hairline px-5 py-3 text-[12.5px] text-ink-700">
          Batch #{data.openBatch.id} is <BatchPill status={data.openBatch.status} /> —{' '}
          {formatInt(data.openBatch.confirmedCount)} confirmed so far. This page refreshes
          itself while it runs.
        </div>
      )}

      <ValueTable
        rows={data.approved}
        empty="Nothing is waiting. Set a value on a product page and it appears here."
        unticked={unticked}
        onToggle={toggle}
        onDiscarded={onStarted}
      />
    </section>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-paper-card px-5 py-3">
      <div className="eyebrow">{label}</div>
      <div className="mt-1 font-mono text-[20px] font-medium tabular-nums text-ink-900">
        {formatInt(value)}
      </div>
    </div>
  )
}

// ── published, not confirmed ───────────────────────────────────────────────

function UnconfirmedCard({ rows }: { rows: PendingValueRow[] }) {
  return (
    <section className="mt-6 rounded-md border border-[#e7d5aa] bg-paper-card shadow-card">
      <div className="border-b hairline bg-warn-100 px-5 py-4">
        <h2 className="eyebrow text-[#6d4b0e]">Published, not confirmed</h2>
        <p className="mt-1 text-[12.5px] leading-snug text-[#6d4b0e]">
          These went out in a batch and the read-back did not show them on every size. The
          nightly sync confirms them if they land later; until then they are the values to
          look at.
        </p>
      </div>
      <ValueTable rows={rows} empty="" />
    </section>
  )
}

function ValueTable({
  rows,
  empty,
  unticked,
  onToggle,
  onDiscarded,
}: {
  rows: PendingValueRow[]
  empty: string
  /** Absent on the read-only "published, not confirmed" table. */
  unticked?: Set<string>
  onToggle?: (row: PendingValueRow) => void
  onDiscarded?: () => void
}) {
  if (rows.length === 0) {
    return <div className="px-5 py-6 text-[12.5px] text-ink-400">{empty}</div>
  }
  const selectable = unticked !== undefined && onToggle !== undefined
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-separate border-spacing-0 text-[12.5px]">
        <thead>
          <tr>
            {selectable && <Th> </Th>}
            <Th>Product</Th>
            <Th>Attribute</Th>
            <Th>Market</Th>
            <Th>Value</Th>
            <Th right>Sizes</Th>
            <Th>Set by</Th>
            <Th>When</Th>
            {selectable && <Th> </Th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.modelColor}|${r.attribute}|${r.market}`} className="hover:bg-[#fbfbf8]">
              {selectable && (
                <Td>
                  <input
                    type="checkbox"
                    checked={!unticked!.has(cellKey(r))}
                    onChange={() => onToggle!(r)}
                    aria-label={`Push ${r.label} ${r.market} on ${r.modelColor}`}
                    className="h-3.5 w-3.5 accent-brand-700"
                  />
                </Td>
              )}
              <Td>
                <Link
                  to={`/products/${encodeURIComponent(r.modelColor)}`}
                  className="font-mono text-brand-800 hover:underline"
                >
                  {r.modelColor}
                </Link>
              </Td>
              <Td>{r.label}</Td>
              <Td>
                <span className="font-mono text-[11px]">{r.market === '*' ? 'all' : r.market}</span>
              </Td>
              <Td>
                <span className="block max-w-[420px] truncate" title={r.value ?? ''}>
                  {r.value}
                </span>
              </Td>
              <Td right>
                <span className="font-mono tabular-nums">{r.sizes}</span>
              </Td>
              <Td>
                <span className="font-mono text-[11px] text-ink-500">{r.updatedBy ?? '—'}</span>
              </Td>
              <Td>
                <span className="font-mono text-[11px] text-ink-500" title={formatDateTime(r.updatedAt)}>
                  {formatRelative(r.updatedAt)}
                </span>
              </Td>
              {selectable && (
                <Td>
                  <DiscardButton row={r} onDone={onDiscarded} />
                </Td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── history ────────────────────────────────────────────────────────────────

function HistoryCard({ rows, loading }: { rows: PushBatchRow[]; loading: boolean }) {
  const [open, setOpen] = useState<number | null>(null)
  return (
    <section className="mt-6 rounded-md border hairline bg-paper-card shadow-card">
      <div className="border-b hairline px-5 py-4">
        <h2 className="eyebrow">Pushes</h2>
        <p className="mt-1 text-[12.5px] text-ink-500">
          Every batch, newest first. A batch where nothing landed is the important failure,
          and a halted one is the one to read before anything else is pushed.
        </p>
      </div>
      {loading && rows.length === 0 ? (
        <div className="m-5 h-16 animate-pulse rounded-md border hairline" />
      ) : rows.length === 0 ? (
        <div className="px-5 py-6 text-[12.5px] text-ink-400">No pushes yet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-separate border-spacing-0 text-[12.5px]">
            <thead>
              <tr>
                <Th> </Th>
                <Th>Batch</Th>
                <Th>When</Th>
                <Th>By</Th>
                <Th>Status</Th>
                <Th right>Writes</Th>
                <Th right>Confirmed</Th>
                <Th right>On the shop</Th>
                <Th right>Failed</Th>
                <Th right>Skipped</Th>
                <Th>Summary</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <BatchRows key={b.id} batch={b} open={open === b.id} onToggle={() => setOpen(open === b.id ? null : b.id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function BatchRows({ batch, open, onToggle }: { batch: PushBatchRow; open: boolean; onToggle: () => void }) {
  const detail = useApi(
    () => (open ? api.importsBatch(batch.id) : Promise.resolve(null as PushBatchDetail | null)),
    [open, batch.id, batch.status],
  )
  const halted = batch.status === 'halted'
  return (
    <>
      <tr
        onClick={onToggle}
        className={cn('cursor-pointer hover:bg-[#fbfbf8]', halted && 'bg-danger-100/60')}
      >
        <Td>{open ? <ChevronDown size={13} className="text-ink-400" /> : <ChevronRight size={13} className="text-ink-400" />}</Td>
        <Td>
          <span className="font-mono">#{batch.id}</span>
          {batch.note && <span className="ml-2 text-ink-500">{batch.note}</span>}
        </Td>
        <Td>
          <span className="font-mono text-[11px] text-ink-500" title={formatDateTime(batch.createdAt)}>
            {formatRelative(batch.createdAt)}
          </span>
        </Td>
        <Td>
          <span className="font-mono text-[11px] text-ink-500">{batch.createdBy ?? batch.source}</span>
        </Td>
        <Td>
          <BatchPill status={batch.status} />
        </Td>
        <Td right>
          <span className="font-mono tabular-nums">{formatInt(batch.itemCount)}</span>
        </Td>
        <Td right>
          <span className="font-mono tabular-nums text-ok-600">{formatInt(batch.confirmedCount)}</span>
        </Td>
        <Td right>
          <ShopCount
            published={batch.publishedCount}
            confirmed={batch.confirmedCount}
            unlisted={batch.unlistedCount}
          />
        </Td>
        <Td right>
          <span className={cn('font-mono tabular-nums', batch.failedCount > 0 && 'text-danger-600')}>
            {formatInt(batch.failedCount)}
          </span>
        </Td>
        <Td right>
          <span className="font-mono tabular-nums text-ink-500">{formatInt(batch.skippedCount)}</span>
        </Td>
        <Td>
          <span className={cn('block max-w-[380px] truncate', halted ? 'text-danger-600' : 'text-ink-500')} title={batch.errorSummary ?? ''}>
            {batch.errorSummary ?? ''}
          </span>
        </Td>
      </tr>
      {open && (
        <tr>
          <td colSpan={11} className="border-b border-ink-100 bg-paper-sunk px-5 py-3">
            {detail.loading && !detail.data ? (
              <div className="h-10 animate-pulse rounded-md border hairline bg-paper-card" />
            ) : detail.data ? (
              <ItemTable items={detail.data.items} />
            ) : (
              <span className="text-[12px] text-ink-400">Could not load the batch.</span>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

function ItemTable({ items }: { items: PushBatchDetail['items'] }) {
  return (
    <table className="w-full border-separate border-spacing-0 text-[12px]">
      <thead>
        <tr>
          <Th>Size</Th>
          <Th>Key</Th>
          <Th>Locale</Th>
          <Th>Was</Th>
          <Th>Now</Th>
          <Th>Status</Th>
          <Th>On the shop</Th>
          <Th>Reason</Th>
        </tr>
      </thead>
      <tbody>
        {items.map((i, n) => (
          <tr key={n}>
            <Td>
              <span className="font-mono">{i.sku}</span>
            </Td>
            <Td>
              <span className="font-mono text-[11px]">{i.key}</span>
            </Td>
            <Td>
              <span className="font-mono text-[11px] text-ink-500">{i.locale ?? '—'}</span>
            </Td>
            <Td>
              <span className="block max-w-[260px] truncate text-ink-500" title={i.oldUpstream ?? ''}>
                {i.oldUpstream ?? <span className="italic">empty</span>}
              </span>
            </Td>
            <Td>
              <span className="block max-w-[260px] truncate" title={i.newValue ?? ''}>
                {i.newValue}
              </span>
            </Td>
            <Td>
              <span
                className={cn(
                  'font-mono text-[10.5px] uppercase tracking-[0.08em]',
                  i.status === 'confirmed' && 'text-ok-600',
                  i.status === 'failed' && 'text-danger-600',
                  i.status === 'skipped' && 'text-ink-400',
                  (i.status === 'written' || i.status === 'pending') && 'text-info-600',
                )}
              >
                {i.status}
              </span>
            </Td>
            <Td>
              {i.publishedAt ? (
                <span
                  className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-ok-600"
                  title={`The shop was serving this at ${formatDateTime(i.publishedAt)}`}
                >
                  live
                </span>
              ) : i.unlisted ? (
                <span
                  className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-ink-400"
                  title={`Written to Omnitron, but ${i.modelColor} is not in ${i.market === '*' ? "any market's" : `the ${i.market}`} listing feed — the only source the shop check reads. It will never go live here however long it is left. The product page may well be showing the value.`}
                >
                  unlisted
                </span>
              ) : i.status === 'confirmed' || i.status === 'written' ? (
                <span
                  className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-info-600"
                  title="Written to Omnitron. The shop had not started serving it when it was last asked — it is asked again on every sync."
                >
                  waiting
                </span>
              ) : (
                <span className="text-ink-300">—</span>
              )}
            </Td>
            <Td>
              <span className="block max-w-[360px] truncate text-ink-500" title={i.reason ?? ''}>
                {i.reason ?? ''}
              </span>
            </Td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** A batch status on the run-status pill, with `halted` said out loud. */
function BatchPill({ status }: { status: PushBatchStatus }) {
  const run: RunStatus =
    status === 'done'
      ? 'success'
      : status === 'partial'
        ? 'partial'
        : status === 'pushing'
          ? 'running'
          : status === 'pending'
            ? 'skipped'
            : 'failed'
  return (
    <span className="inline-flex items-center gap-1.5">
      <StatusPill status={run} />
      {status === 'halted' && (
        <span className="rounded-[3px] bg-danger-600 px-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-white">
          halted
        </span>
      )}
      {status === 'pending' && <span className="font-mono text-[10.5px] text-ink-500">pending</span>}
    </span>
  )
}

function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return (
    <th
      className={cn(
        'border-b border-ink-200 px-3 py-2 font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-ink-500',
        right ? 'text-right' : 'text-left',
      )}
    >
      {children}
    </th>
  )
}

function Td({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return (
    <td className={cn('border-b border-ink-100 px-3 py-1.5 align-top', right ? 'text-right' : 'text-left')}>
      {children}
    </td>
  )
}


/**
 * Take one value out of the queue for good.
 *
 * Asks first, and says what it means: the value is deleted rather than parked,
 * so the cell reads as missing until the next sync puts back whatever Omnitron
 * actually holds. For a value that only ever existed here — a name typed where
 * there was none — nothing comes back, and that is the case worth pausing on.
 */
function DiscardButton({ row, onDone }: { row: PendingValueRow; onDone?: () => void }) {
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState('')
  const discard = useMutation((r: PendingValueRow) =>
    api.importsDiscard({ modelColor: r.modelColor, attribute: r.attribute, market: r.market }),
  )

  if (!asking) {
    return (
      <button
        type="button"
        onClick={() => setAsking(true)}
        title="Take this value out of the queue"
        aria-label={`Discard ${row.label} ${row.market} on ${row.modelColor}`}
        className="rounded-2xs p-1 text-ink-400 hover:bg-danger-100 hover:text-danger-600"
      >
        <Trash2 size={13} />
      </button>
    )
  }
  return (
    <span className="flex items-center gap-1.5 whitespace-nowrap">
      <button
        type="button"
        disabled={discard.loading}
        onClick={async () => {
          setError('')
          try {
            await discard.mutate(row)
            onDone?.()
          } catch (e) {
            setError(e instanceof Error ? e.message : 'Could not discard it.')
            setAsking(false)
          }
        }}
        className="rounded-2xs bg-danger-600 px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em] text-white disabled:opacity-50"
      >
        {discard.loading ? '…' : 'discard'}
      </button>
      <button
        type="button"
        onClick={() => setAsking(false)}
        className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500 hover:text-ink-900"
      >
        keep
      </button>
      {error && <span className="text-[11px] text-danger-600">{error}</span>}
    </span>
  )
}


/**
 * How much of a batch a customer can actually see.
 *
 * The batch says `done` when Omnitron confirmed it, seconds after the write.
 * The shop follows on its own schedule — two hours on the first real push,
 * and 27 seconds on a different channel for the same product — so this number
 * keeps rising long after the batch stopped changing. Showing it beside
 * `confirmed` is the whole point: the two mean different things and the panel
 * used to say only the first, while calling the value live.
 *
 * The gap is two different things and used to be shown as one. The check
 * crawls each market's listing feed, so an option that has left that feed can
 * never match — `0 / 22` on a batch whose values were plainly on the site
 * meant "sixteen of these are on a product the listing no longer carries",
 * and the tooltip underneath it said they were asked again every sync, which
 * was untrue. So the unreachable part is subtracted out of the denominator
 * and named, and only what is genuinely late is counted as late.
 */
function ShopCount({
  published,
  confirmed,
  unlisted,
}: {
  published: number
  confirmed: number
  unlisted: number
}) {
  if (confirmed === 0) {
    return <span className="font-mono tabular-nums text-ink-300">—</span>
  }
  // What the check can still answer for. Clamped because `confirmed` and the
  // item scan are two queries and a push landing between them would otherwise
  // show a negative.
  const reachable = Math.max(published, confirmed - unlisted)
  const all = published >= reachable
  const waiting = reachable - published
  const unreachable =
    unlisted > 0
      ? ` ${formatInt(unlisted)} more ${unlisted === 1 ? 'is on an option' : 'are on options'} the market's listing no longer carries, so the check cannot see ${unlisted === 1 ? 'it' : 'them'} — the value is in Omnitron and the product page may well be showing it.`
      : ''
  return (
    <span
      className={cn(
        'font-mono tabular-nums',
        all ? (unlisted > 0 ? 'text-ink-500' : 'text-ok-600') : 'text-info-600',
      )}
      title={
        (all
          ? 'Every value the shop check can reach is being served.'
          : `${formatInt(waiting)} written to Omnitron but not yet seen on the shop. Publication is on Akinon's schedule and is asked again on every sync.`) + unreachable
      }
    >
      {formatInt(published)}
      {!all && <span className="text-ink-400"> / {formatInt(reachable)}</span>}
      {unlisted > 0 && (
        <span className="ml-1.5 text-[10.5px] uppercase tracking-[0.06em] text-ink-400">
          +{formatInt(unlisted)} unlisted
        </span>
      )}
    </span>
  )
}


/**
 * Suggestions waiting on wrong-language values.
 *
 * The proposals feature shipped without this and was unusable for it: a
 * suggestion was only visible if you already had the right product open, and
 * there are thousands across a hundred thousand options. Nothing here is new
 * behaviour - it is the same accept and reject the cell inspector already
 * has - it is the way in.
 *
 * The live value is shown against the proposed one because that comparison is
 * the entire decision. Editing before accepting stays in the inspector: a
 * textarea per row would turn a review queue into a writing surface, and the
 * product page is where you can see the rest of the option anyway.
 */
function ProposalsCard({
  data,
  loading,
  market,
  onMarket,
  onAnswered,
  hidden,
  onHidden,
}: {
  data: ProposalsWaiting | undefined
  loading: boolean
  market: string | null
  onMarket: (m: string | null) => void
  onAnswered: () => void
  hidden: boolean
  onHidden: (v: boolean) => void
}) {
  if (loading && !data) {
    return <div className="h-24 animate-pulse rounded-md border hairline bg-paper-card" />
  }
  if (!data) return null
  const markets = Object.entries(data.byMarket).sort((a, b) => b[1] - a[1])
  const waiting = Object.values(data.byMarket).reduce((n, v) => n + v, 0)

  return (
    <section className="overflow-hidden rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b hairline px-5 py-3.5">
        <div>
          {/* "Proposed values", not "translations". Two of the four sources do
              not translate anything: the grouping row hands down text a person
              already typed on the style, and the shared key fills a market
              field that is simply empty. Calling the queue translations made
              those look like machine output, which is the opposite of how much
              to trust them. */}
          <h2 className="text-[13.5px] font-semibold text-ink-900">Proposed values</h2>
          <p className="mt-1 text-[12px] text-ink-500">
            {waiting === 0
              ? 'Nothing waiting. Run the proposals pass to look for values in the wrong language or missing entirely.'
              : `${formatInt(data.total)} waiting${market ? ` in ${market}` : ''}${
                  data.shown < data.total ? `, showing ${formatInt(data.shown)}` : ''
                }`}
          </p>
        </div>
        <div className="flex items-center gap-1">
          {!hidden && markets.length > 1 && (
            <>
              <FilterChip label="All" active={market === null} onClick={() => onMarket(null)} />
              {markets.map(([m, n]) => (
                <FilterChip
                  key={m}
                  label={`${m} ${n}`}
                  active={market === m}
                  onClick={() => onMarket(market === m ? null : m)}
                />
              ))}
            </>
          )}
          {/* The queue runs to thousands and is not everyone's job. Collapsing
              it leaves the count visible, so hiding the work never hides that
              there is work — the number above stays whatever this says. */}
          <button
            type="button"
            onClick={() => onHidden(!hidden)}
            aria-expanded={!hidden}
            className="ml-1 rounded border hairline bg-paper px-2 py-1 font-mono text-[11px] text-ink-500 hover:text-ink-900"
          >
            {hidden ? 'Show' : 'Hide'}
          </button>
        </div>
      </div>

      {hidden ? null : data.rows.length === 0 ? (
        <div className="px-5 py-6 text-[12.5px] text-ink-400">
          {waiting === 0
            ? 'A proposal appears here when a value is in the wrong language, or a listed product has none at all, and something can replace it.'
            : 'Nothing waiting in this market.'}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-separate border-spacing-0 text-[12.5px]">
            <thead>
              <tr>
                <Th>Product</Th>
                <Th>Field</Th>
                <Th>Now</Th>
                <Th>Proposed</Th>
                <Th> </Th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <ProposalRowView
                  key={`${r.modelColor}|${r.attribute}|${r.market}`}
                  row={r}
                  onAnswered={onAnswered}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'rounded-2xs border px-2 py-1 font-mono text-[10.5px] uppercase tracking-[0.08em]',
        active
          ? 'border-brand-700 bg-brand-50 text-brand-700'
          : 'hairline bg-paper-card text-ink-500 hover:text-ink-900',
      )}
    >
      {label}
    </button>
  )
}

/**
 * Where a proposal came from, and how much that is worth.
 *
 * These rendered as the raw source names the sync writes — `from sibling`,
 * `from grouping_row`, `from shared_key` — which name the mechanism and not
 * the evidence. The evidence is the whole decision: whether to trust a
 * suggested value depends entirely on what stands behind it.
 *
 * **Ordered by how much corroboration each one carries**, which is not the
 * same as how human the text is:
 *
 * 1. `sibling` is the only source with *independent agreement* behind it —
 *    two or more colours of the style carry the text identically, which is
 *    positive evidence that it describes the style rather than one colour.
 *    Three guards, and 17% of candidates survive them (840 of 5,076).
 * 2. `grouping_row` is human-typed and deliberate — somebody wrote it on this
 *    style — but nothing corroborates it, and nothing stops a person typing a
 *    colour into a field that names the style.
 * 3. `shared_key` is human-typed too, and comes from the one field no shop
 *    renders, so nobody has ever had a reason to keep it right. Storing it as
 *    the description is what put Turkish in the panel while example.com showed
 *    English (784RMA0950.2A9).
 * 4. `machine` has no human and no corroboration.
 *
 * `trust` leads the tooltip because it is what a reviewer wants first; `why`
 * follows with the mechanism.
 */
const PROPOSAL_SOURCE: Record<string, { label: string; trust: string; why: string }> = {
  sibling: {
    label: 'another colour',
    trust: 'Strongest — two or more colours agree on this text independently.',
    why:
      'Another colour of this same style already carries this text in this ' +
      'language. Only offered when two or more colours carry it identically, ' +
      'so text naming a colour cannot qualify, and only when the two agree on ' +
      'fit, pattern, sleeve, collar, category, gender and composition — a base ' +
      'code can hold two different garments.',
  },
  grouping_row: {
    label: "the style's own row",
    trust: 'Strong — a person typed it, but nothing else confirms it.',
    why:
      'Omnitron keeps one row per style, behind the listing card. It is where ' +
      'a translator most often typed, so it frequently holds the market ' +
      'language while every size still holds English. It names the style, not ' +
      'the colour, so check it if the text should mention one.',
  },
  shared_key: {
    label: 'the all-languages field',
    trust: 'Weaker — from a field no shop renders, so nobody maintains it.',
    why:
      'Omnitron keeps one description field holding every language at once. ' +
      'The storefronts show the market field only and ignore this one, which ' +
      'is why it can sit stale or in the wrong language. Read it before ' +
      'accepting.',
  },
  machine: {
    label: 'machine translation',
    trust: 'Weakest — nothing in the catalogue vouches for this one.',
    why: 'Translated by a model from the value in another market. Always worth reading.',
  },
}

function ProposalRowView({
  row,
  onAnswered,
}: {
  row: WaitingProposal
  onAnswered: () => void
}) {
  const [error, setError] = useState('')
  const cell = {
    modelColor: row.modelColor,
    attribute: row.attribute,
    market: row.market,
  }
  const accept = useMutation(() => api.importsAcceptProposal(cell))
  const reject = useMutation(() => api.importsRejectProposal(cell))
  const busy = accept.loading || reject.loading

  const answer = async (run: () => Promise<unknown>) => {
    setError('')
    try {
      await run()
      onAnswered()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not do that.')
    }
  }

  return (
    <tr className="align-top hover:bg-[#fbfbf8]">
      <Td>
        <Link
          to={`/products/${row.modelColor}`}
          className="font-mono text-[11.5px] text-brand-700 hover:underline"
        >
          {row.modelColor}
        </Link>
      </Td>
      <Td>
        <span className="whitespace-nowrap">{row.label}</span>
        <span className="ml-1.5 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400">
          {row.market}
        </span>
        {row.flag && (
          <span className="ml-1.5 rounded-[2px] border border-[#ddc48a] bg-warn-100 px-[3px] font-mono text-[9.5px] font-semibold text-warn-600">
            {row.flag}
          </span>
        )}
      </Td>
      <Td>
        <span className="block max-w-[300px] truncate text-ink-500" title={row.current ?? ''}>
          {row.current ?? <span className="font-mono text-[11px] text-ink-300">empty</span>}
        </span>
      </Td>
      <Td>
        <span className="block max-w-[300px] truncate" title={row.proposed}>
          {row.proposed}
        </span>
        {row.source && (
          <span
            className="cursor-help font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 underline decoration-dotted decoration-ink-200 underline-offset-2"
            title={
              PROPOSAL_SOURCE[row.source]
                ? `${PROPOSAL_SOURCE[row.source].trust}

${PROPOSAL_SOURCE[row.source].why}`
                : undefined
            }
          >
            from {PROPOSAL_SOURCE[row.source]?.label ?? row.source}
          </span>
        )}
        {error && <div className="mt-1 text-[11px] text-danger-600">{error}</div>}
      </Td>
      <Td>
        <span className="flex items-center gap-1.5 whitespace-nowrap">
          <button
            type="button"
            disabled={busy}
            onClick={() => answer(() => accept.mutate(undefined))}
            title="Take it into the queue as your value, to go out on the next push"
            className="rounded-2xs bg-brand-700 px-2 py-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-white hover:bg-brand-600 disabled:opacity-50"
          >
            {accept.loading ? '…' : 'accept'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => answer(() => reject.mutate(undefined))}
            title="Drop it. The live value is left exactly as it is."
            className="rounded-2xs border hairline bg-paper-card px-2 py-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-500 hover:text-ink-900 disabled:opacity-50"
          >
            reject
          </button>
        </span>
      </Td>
    </tr>
  )
}
