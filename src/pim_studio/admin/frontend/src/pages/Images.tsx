/**
 * Images — options with no photo, sourced from the brand library and handed on.
 *
 * The page is built around one fact: we cannot write images into Omnitron. The
 * connection is read-only by decision, so publishing a photo to our own server
 * does not put it on a product page — somebody in Turkey has to upload it. The
 * bundle is therefore the deliverable, and it sits above the tables with its
 * download buttons on it rather than at the bottom of the page.
 *
 * Three things the layout is deliberate about:
 *
 * **Before anything has read Omnitron's image column, no number is shown.** An
 * option with no image on record and an option nobody has looked at are the
 * same row until then, and the plausible-looking answer is "all of them". The
 * API reports that as `blocked` and this renders a different screen for it.
 *
 * **The connection strip never moves.** Healthy it is a green dot and a
 * sentence; broken it turns red in the same place with the two ways out. A
 * failure that relocates the thing you were reading is a failure twice.
 *
 * **"Not in BrandLibrary" is a tab, not a footnote.** It is the only list here
 * that needs a person — no amount of re-running produces a photo the brand has
 * never shot.
 *
 * **A supplied list is obeyed, and the overrides are shown.** `Publish now`
 * works from what the database says needs a photo; the list box beside it works
 * from what somebody typed, and the two disagree on purpose. Codes merch
 * removed, codes Turkey already has and codes no PL catalogue carries are all
 * published when they are asked for by name, and reported back so the run's
 * count is never a mystery.
 */
import { useRef, useState } from 'react'
import { AlertTriangle, Download, FileSpreadsheet, RefreshCw, Upload } from 'lucide-react'

import {
  api,
  type AbsentRow,
  type BundleRow,
  type ImageListPublish,
  type ImagesStatus,
  type PublishedImageRow,
} from '../lib/api'
import { useApi } from '../lib/useApi'
import { useAuth } from '../lib/auth'
import { cn } from '../lib/cn'
import PageContainer from '../components/PageContainer'
import StatCard from '../components/StatCard'
import StatusPill from '../components/StatusPill'
import { formatBytes, formatDateTime, formatInt, formatRelative } from '../lib/format'

/** Poll while a run is in flight — it takes about eight minutes. */
const POLL_MS = 15_000

/** Rows of each table shown on the page. The manifest runs to thousands and
 *  nobody reads it here — the complete list is the Excel inside the bundle,
 *  which is the artefact built for reading. */
const PREVIEW_ROWS = 12

export default function Images() {
  const { user } = useAuth()
  const canRun = user?.role === 'developer' || user?.role === 'planning'

  const status = useApi(() => api.imagesStatus(), [], { pollMs: POLL_MS })
  const bundles = useApi(() => api.imagesBundles(), [])
  // A preview, not the manifest — the full list is the Excel in the bundle.
  const published = useApi(() => api.imagesPublished(PREVIEW_ROWS), [])
  const absent = useApi(() => api.imagesAbsent(PREVIEW_ROWS), [])

  const [tab, setTab] = useState<'published' | 'absent'>('published')
  const [dryRun, setDryRun] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')

  const data = status.data
  const running = data?.running ?? false

  async function start() {
    setStarting(true)
    setError('')
    try {
      await api.imagesPublish(dryRun)
      await status.refetch()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }

  return (
    <PageContainer>
      <div className="flex items-start justify-between gap-6">
        <div>
          <h1 className="font-serif text-[28px] font-medium tracking-tight text-ink-900">
            Images
          </h1>
          <p className="mt-2 font-mono text-[12px] text-ink-500">
            Options with no photo, sourced from the brand library and handed to Turkey
          </p>
        </div>
        {canRun && (
          <div className="flex flex-none items-center gap-2">
            <label className="flex cursor-pointer items-center gap-1.5 pr-1">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
                className="h-3.5 w-3.5 accent-brand-700"
              />
              <span className="font-mono text-[11px] text-ink-500">dry run</span>
            </label>
            <button
              type="button"
              onClick={start}
              disabled={running || starting || !data || !!data.blocked}
              className="inline-flex h-8 items-center gap-1.5 rounded-[5px] bg-brand-700 px-3.5 text-[13px] font-medium text-white disabled:opacity-50"
            >
              {running || starting ? (
                <RefreshCw size={13} className="animate-spin" />
              ) : (
                <Download size={13} />
              )}
              {running ? 'Publishing…' : starting ? 'Starting…' : 'Publish now'}
            </button>
          </div>
        )}
      </div>

      {status.loading && !data && (
        <div className="mt-6 h-28 animate-pulse rounded-md border hairline bg-paper-card" />
      )}
      {status.error && (
        <div className="mt-6 rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
          Could not load the image status — {status.error.message}
        </div>
      )}

      {data && (
        <>
          <Connection status={data} canRun={canRun} onPasted={status.refetch} />
          {error && (
            <p className="mt-3 font-mono text-[12px] text-danger-600">{error}</p>
          )}

          {/* Above the `blocked` branch on purpose. That branch exists because
              "who has no photo" is unanswerable until something has read the
              image column — a question a supplied list never asks, so this box
              works on a database the counts below cannot be shown for. */}
          {canRun && (
            <PublishList
              disabled={running || starting}
              dryRun={dryRun}
              onStarted={status.refetch}
            />
          )}

          {data.blocked ? (
            <Blocked reason={data.blocked} />
          ) : (
            <>
              <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <StatCard
                  label="Missing an image"
                  value={data.counts.missing}
                  tone="warn"
                  sub="options live in a catalogue"
                />
                <StatCard
                  label="Published, with Turkey"
                  value={data.withTurkey}
                  hint="Options whose photos are on the photo server and have not yet been seen in Omnitron."
                  sub={
                    data.awaitingBundle
                      ? `${formatInt(data.awaitingBundle)} photos not yet bundled`
                      : `${formatInt(data.photosPublished)} photos, all bundled`
                  }
                />
                <StatCard
                  label="Not in BrandLibrary"
                  value={data.absent}
                  tone={data.absent ? 'danger' : 'neutral'}
                  sub="searched, nothing there"
                />
                <StatCard
                  label="Last run"
                  value={
                    data.lastRun?.startedAt
                      ? formatRelative(data.lastRun.startedAt)
                      : 'never'
                  }
                  sub={data.lastRun?.triggeredBy ?? '06:00 daily, scheduled'}
                />
              </div>

              {data.counts.unclassified ? (
                <p className="mt-3 max-w-prose text-[12.5px] text-ink-500">
                  {formatInt(data.counts.unclassified)} of these carry a comment
                  nobody has classified. They are included &mdash; unknown means
                  someone decide, not skip it.
                </p>
              ) : null}

              <LastRun status={data} />
            </>
          )}

          <Handoff
            rows={bundles.data?.rows ?? []}
            canRun={canRun}
            onSent={async (id) => {
              await api.imagesMarkSent(id)
              await bundles.refetch()
            }}
          />

          <div className="mt-8 flex items-end gap-1 border-b hairline">
            <Tab
              label="Published"
              count={published.data?.total ?? 0}
              active={tab === 'published'}
              onClick={() => setTab('published')}
            />
            <Tab
              label="Not in BrandLibrary"
              count={absent.data?.total ?? 0}
              active={tab === 'absent'}
              onClick={() => setTab('absent')}
            />
          </div>

          {tab === 'published' ? (
            <PublishedTable rows={published.data?.rows ?? []} total={published.data?.total ?? 0} />
          ) : (
            <AbsentTable rows={absent.data?.rows ?? []} total={absent.data?.total ?? 0} />
          )}

          <p className="mt-6 max-w-prose text-[12px] text-ink-400">
            Counted against the last successful image sync from Omnitron
            {data.imageSourceAt ? ` (${formatDateTime(data.imageSourceAt)})` : ''}. Options
            merch has marked removed are left out; options nobody has read an image
            column for are not counted as missing.
          </p>
        </>
      )}
    </PageContainer>
  )
}

/**
 * The strip that never moves — same place healthy or broken.
 *
 * It leads on the cookie because the cookie is the thing that breaks. The
 * brand library has no password and no API token: a browser session is the
 * whole of authentication and it dies within two hours. Since 0.46.0 the 06:00
 * job logs in for itself immediately before it publishes, so the paste box is
 * for the morning that login is refused. It stays one click away at all times
 * rather than appearing only after a failure, because by then the scheduled
 * run has already failed.
 */
function Connection({
  status,
  canRun,
  onPasted,
}: {
  status: ImagesStatus
  canRun: boolean
  onPasted: () => void | Promise<void>
}) {
  const { configured } = status
  const cookie = configured.cookie
  const ok = configured.ready
  const oauth = configured.mode === 'oauth'
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)

  async function save() {
    setBusy(true)
    setResult(null)
    try {
      const body = await api.imagesSetCookie(text)
      setResult({ ok: true, message: body.message })
      setText('')
      await onPasted()
      setOpen(false)
    } catch (e) {
      setResult({ ok: false, message: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const missing = [
    configured.library ? null : 'no way into the brand library',
    configured.photoServer ? null : 'photo server not configured',
  ].filter(Boolean)

  return (
    <div
      className={cn(
        'mt-5 rounded-md border bg-paper-card shadow-card',
        ok ? 'hairline' : 'border-warn-600',
      )}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3.5 py-3">
        <span
          className={cn(
            'h-[7px] w-[7px] flex-none rounded-full',
            ok ? 'bg-ok-600' : 'bg-warn-600',
          )}
        />
        <span className="text-[13px] font-medium text-ink-900">
          {ok ? 'Signed in to BrandLibrary' : 'Not ready to run'}
        </span>
        <span className="font-mono text-[11.5px] text-ink-500">
          {!ok
            ? missing.join(' · ')
            : oauth
              ? 'signs itself in each run — nothing to paste'
              : cookie?.lastOkAt
                ? `worked ${formatRelative(cookie.lastOkAt)}${
                    cookie.rotatedBy ? ` · pasted by ${cookie.rotatedBy}` : ''
                  }`
                : `pasted ${
                    cookie?.rotatedAt ? formatRelative(cookie.rotatedAt) : ''
                  }, not yet used`}
        </span>
        {!oauth && cookie?.fromEnv && (
          <span className="inline-flex h-5 items-center rounded-[3px] bg-warn-100 px-1.5 font-mono text-[10.5px] font-semibold text-warn-600">
            from the server&rsquo;s .env
          </span>
        )}
        <span className="flex-1" />
        {canRun && !oauth && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="inline-flex h-[26px] items-center rounded-[3px] border hairline bg-paper-card px-2.5 font-mono text-[11px] text-ink-700 hover:bg-paper"
          >
            {open ? 'cancel' : 'paste a cookie'}
          </button>
        )}
      </div>

      {open && canRun && !oauth && (
        <div className="border-t hairline bg-paper px-3.5 py-3.5">
          <div className="eyebrow mb-1.5">Replacing the cookie</div>
          <p className="mb-2.5 max-w-prose text-[12.5px] leading-5 text-ink-500">
            The brand library has no password or API token &mdash; a browser session
            is the only way in, and it lasts hours. Open the library in Chrome, press
            F12, go to <span className="font-mono text-ink-700">Network</span>,
            right-click any <span className="font-mono text-ink-700">/api/v4/</span>{' '}
            request and choose <b className="font-medium text-ink-700">Copy as cURL</b>.
            Paste the whole thing here.
          </p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            spellCheck={false}
            placeholder="curl 'https://assets.example.test/api/v4/media/?type=image' -H 'cookie: …'"
            className="w-full rounded-[5px] border hairline bg-paper-card px-2.5 py-2 font-mono text-[11px] text-ink-900 focus:outline-none focus:ring-1 focus:ring-brand-700"
          />
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={save}
              disabled={busy || !text.trim()}
              className="inline-flex h-8 items-center rounded-[5px] bg-brand-700 px-3.5 text-[13px] font-medium text-white disabled:opacity-50"
            >
              {busy ? 'Checking…' : 'Save cookie'}
            </button>
            <span className="text-[12px] text-ink-400">
              Tried against the library before it is saved.
            </span>
          </div>
          {result && (
            <p
              className={cn(
                'mt-2 font-mono text-[11.5px]',
                result.ok ? 'text-ok-600' : 'text-danger-600',
              )}
            >
              {result.message}
            </p>
          )}
        </div>
      )}

      {ok && !oauth && !open && (
        <div className="border-t hairline bg-paper px-3.5 py-2.5 text-[12px] text-ink-400">
          A session cookie is the only way in without OAuth credentials, and it lasts
          under two hours &mdash; so the scheduled run logs in for itself just before it
          publishes, using a colleague&rsquo;s login. Ask whoever administers the brand
          library for a client id and secret scoped{' '}
          <span className="font-mono text-ink-500">asset:read</span> and it stops depending
          on anyone&rsquo;s password.
        </div>
      )}

      {!ok && !open && (
        <div className="border-t hairline bg-paper px-3.5 py-3.5">
          <div className="eyebrow mb-2">What is missing</div>
          <div className="space-y-3">
            {!configured.library && (
              <div>
                <div className="text-[13px] font-medium text-ink-900">
                  No way into the brand library
                </div>
                <p className="mt-1 max-w-prose text-[12.5px] leading-5 text-ink-500">
                  Nothing has been configured and no cookie has been pasted. Either
                  ask whoever administers the library for OAuth credentials scoped{' '}
                  <span className="font-mono text-ink-700">asset:read</span> and set{' '}
                  <span className="font-mono text-ink-700">BRAND_LIBRARY_CLIENT_ID</span>{' '}
                  and{' '}
                  <span className="font-mono text-ink-700">BRAND_LIBRARY_CLIENT_SECRET</span>{' '}
                  &mdash; after which this runs itself &mdash; or use{' '}
                  <b className="font-medium text-ink-700">paste a cookie</b> above to
                  get going today.
                </p>
              </div>
            )}
            {!configured.photoServer && (
              <div>
                <div className="text-[13px] font-medium text-ink-900">
                  The photo server is not configured
                </div>
                <p className="mt-1 max-w-prose text-[12.5px] leading-5 text-ink-500">
                  <span className="font-mono text-ink-700">FTP_HOST</span>,{' '}
                  <span className="font-mono text-ink-700">FTP_USER</span>,{' '}
                  <span className="font-mono text-ink-700">FTP_PASS</span> and{' '}
                  <span className="font-mono text-ink-700">FTP_BASE_URL</span> go in the
                  server&rsquo;s <span className="font-mono text-ink-700">.env</span> and
                  must be passed through to this container &mdash; see{' '}
                  <span className="font-mono text-ink-700">deploy/README.md</span>. They
                  have never been on this server: the old script ran from a laptop with
                  its own copy. Without{' '}
                  <span className="font-mono text-ink-700">FTP_BASE_URL</span> a run can
                  upload files nobody can reference, which is the one failure here that
                  is invisible afterwards.
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Publish photos for a list somebody has to hand.
 *
 * The weekly run asks the database who needs a photo. This asks the person, and
 * the difference is the point: a list assembled by hand carries a decision — a
 * season opening, a request from Turkey, forty codes a manager sent — that the
 * database has no way to know about. So nothing on the list is filtered out,
 * and what the automatic path *would* have skipped is reported instead.
 *
 * Which column holds the codes is worked out by matching values against
 * products we hold rather than by reading headers, so `column` comes back and
 * is shown. An upload that quietly read the wrong column looks exactly like one
 * that worked, and this is the step most likely to have got it wrong.
 */
function PublishList({
  disabled,
  dryRun,
  onStarted,
}: {
  disabled: boolean
  dryRun: boolean
  onStarted: () => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ImageListPublish | null>(null)
  const [error, setError] = useState('')
  const picker = useRef<HTMLInputElement>(null)

  async function send(input: File | string) {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const body = await api.imagesPublishList(input, dryRun)
      setResult(body)
      setText('')
      await onStarted()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      // Clear the picker either way, so choosing the same file again refires
      // the change event. Without this a retry after a failure does nothing.
      if (picker.current) picker.current.value = ''
    }
  }

  return (
    <div className="mt-3 rounded-md border hairline bg-paper-card shadow-card">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3.5 py-3">
        <span className="text-[13px] font-medium text-ink-900">Publish a list</span>
        <span className="font-mono text-[11.5px] text-ink-500">
          ModelColors from a file or a paste &mdash; searched whatever the rules say
        </span>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="ml-auto inline-flex h-7 items-center rounded-[5px] border hairline bg-paper px-2.5 font-mono text-[11px] text-ink-700"
        >
          {open ? 'close' : 'upload a list'}
        </button>
      </div>

      {open && (
        <div className="border-t hairline px-3.5 py-3.5">
          <input
            ref={picker}
            type="file"
            accept=".xlsx,.xlsm,.csv,.tsv,.txt"
            className="hidden"
            onChange={(e) => {
              const chosen = e.target.files?.[0]
              if (chosen) void send(chosen)
            }}
          />
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={disabled || busy}
              onClick={() => picker.current?.click()}
              className="inline-flex h-8 items-center gap-1.5 rounded-[5px] bg-brand-700 px-3.5 text-[13px] font-medium text-white disabled:opacity-50"
            >
              {busy ? <RefreshCw size={13} className="animate-spin" /> : <Upload size={13} />}
              {busy ? 'Reading…' : 'Choose a file'}
            </button>
            <span className="font-mono text-[11.5px] text-ink-400">
              .xlsx, .csv or .txt &mdash; or paste below
            </span>
          </div>

          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={3}
            spellCheck={false}
            placeholder="TSM1YS.33X, PH9619.001, 3196.130…"
            className="mt-2.5 w-full rounded-[5px] border hairline bg-paper px-2.5 py-2 font-mono text-[12px] text-ink-700"
          />
          <button
            type="button"
            disabled={disabled || busy || !text.trim()}
            onClick={() => void send(text)}
            className="mt-2 inline-flex h-8 items-center rounded-[5px] border hairline bg-paper px-3.5 text-[13px] font-medium text-ink-700 disabled:opacity-50"
          >
            Publish these
          </button>

          {error && <p className="mt-3 font-mono text-[12px] text-danger-600">{error}</p>}
          {result && <ListResult result={result} />}
        </div>
      )}
    </div>
  )
}

/** What the upload turned out to contain, once a run has been started from it. */
function ListResult({ result }: { result: ImageListPublish }) {
  const { overrides } = result
  const sheet = result.sheet ? ` on ${result.sheet}` : ''
  const read =
    result.column && result.column !== 'pasted'
      ? `read ${result.rowsRead} rows from the ${result.column} column${sheet}`
      : `read ${result.rowsRead} codes`
  const overridden =
    overrides.banned.length + overrides.published.length + overrides.outOfCatalogue.length

  return (
    <div className="mt-3 border-t hairline pt-3">
      <p className="font-mono text-[12px] text-ok-600">
        Publishing {formatInt(result.options)} option{result.options === 1 ? '' : 's'} from{' '}
        {result.source}
        {result.dryRun ? ' — dry run, nothing will be uploaded' : ''}
      </p>
      <p className="mt-1 font-mono text-[11.5px] text-ink-400">
        {read}
        {result.duplicates > 0 &&
          ` · ${result.duplicates} duplicate${result.duplicates === 1 ? '' : 's'} collapsed`}
      </p>

      {/* Reported, not excluded — the list asked for these by name. */}
      {overridden > 0 && (
        <div className="mt-2.5">
          <div className="eyebrow mb-1.5">Included, though the weekly run would not have</div>
          <div className="space-y-1">
            <Override label="merch removed" codes={overrides.banned} />
            <Override label="already with Turkey" codes={overrides.published} />
            <Override label="in no live catalogue" codes={overrides.outOfCatalogue} />
          </div>
        </div>
      )}

      {result.notOnRecord.length > 0 && (
        <div className="mt-2.5">
          <div className="eyebrow mb-1.5">
            Searched, though no product is on record with this code
          </div>
          <p className="font-mono text-[11.5px] leading-5 text-ink-500">
            {result.notOnRecord.slice(0, 40).join(' · ')}
            {result.notOnRecord.length > 40 &&
              ` … and ${result.notOnRecord.length - 40} more`}
          </p>
        </div>
      )}

      {result.unreadable.length > 0 && (
        <div className="mt-2.5">
          <div className="eyebrow mb-1.5">
            Not searched &mdash; these are not product codes
          </div>
          <p className="font-mono text-[11.5px] leading-5 text-warn-600">
            {result.unreadable.slice(0, 20).join(' · ')}
            {result.unreadable.length > 20 && ` … and ${result.unreadable.length - 20} more`}
          </p>
        </div>
      )}
    </div>
  )
}

function Override({ label, codes }: { label: string; codes: string[] }) {
  if (codes.length === 0) return null
  return (
    <p className="font-mono text-[11.5px] leading-5 text-ink-500">
      <span className="text-ink-700">{codes.length}</span> {label} &mdash;{' '}
      {codes.slice(0, 12).join(' · ')}
      {codes.length > 12 && ` … and ${codes.length - 12} more`}
    </p>
  )
}


/** Day one. No number is shown, and the reason is the content. */
function Blocked({ reason }: { reason: string }) {
  return (
    <div className="mt-5 rounded-md border hairline border-l-[3px] border-l-warn-600 bg-paper-card p-5 shadow-card">
      <div className="flex items-start gap-3">
        <AlertTriangle size={17} className="mt-0.5 flex-none text-warn-600" />
        <div>
          <div className="text-[15px] font-semibold text-ink-900">
            Nothing has read Omnitron&rsquo;s image column yet.
          </div>
          <p className="mt-1.5 max-w-prose text-[13px] leading-5 text-ink-500">{reason}</p>
          <p className="mt-2.5 text-[13px] text-ink-500">
            The nightly sync reads it. Run it now, or wait for 03:00.
          </p>
          <div className="mt-3 inline-block rounded-[5px] border hairline bg-paper px-3 py-2 font-mono text-[12px] text-ink-700">
            python -m pim_studio.sync images
          </div>
        </div>
      </div>
    </div>
  )
}

function LastRun({ status }: { status: ImagesStatus }) {
  const run = status.lastRun
  if (!run) return null
  return (
    <div className="mt-6 rounded-md border hairline bg-paper-card p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-3">
        <span className="eyebrow">
          {status.running ? 'Publishing' : 'Last run'}
        </span>
        <StatusPill status={run.status} />
        <span className="font-mono text-[11.5px] text-ink-500">
          {run.startedAt ? formatRelative(run.startedAt) : ''}
          {run.triggeredBy ? ` · ${run.triggeredBy}` : ' · scheduled'}
        </span>
      </div>
      <div className="mt-2.5 flex flex-wrap gap-2">
        <Chip tone="flat">{formatInt(run.assetsRead)} assets read</Chip>
        <Chip tone="ok">{formatInt(run.published)} published</Chip>
        {run.failed > 0 && <Chip tone="danger">{formatInt(run.failed)} failed</Chip>}
      </div>
      {run.error && (
        <p className="mt-2.5 max-w-prose font-mono text-[11.5px] text-danger-600">
          {run.error}
        </p>
      )}
      {status.running && (
        <p className="mt-2.5 text-[12px] text-ink-400">
          Runs on the server — closing this page will not stop it.
        </p>
      )}
    </div>
  )
}

function Chip({ tone, children }: { tone: 'ok' | 'warn' | 'danger' | 'flat'; children: React.ReactNode }) {
  const tones = {
    ok: 'bg-ok-100 text-ok-600',
    warn: 'bg-warn-100 text-warn-600',
    danger: 'bg-danger-100 text-danger-600',
    flat: 'bg-ink-100 text-ink-700',
  }
  return (
    <span
      className={cn(
        'inline-flex h-[22px] items-center rounded-[3px] px-2 font-mono text-[11px] font-semibold tabular-nums',
        tones[tone],
      )}
    >
      {children}
    </span>
  )
}

/** The deliverable. Above the tables, with its downloads on it. */
function Handoff({
  rows,
  canRun,
  onSent,
}: {
  rows: BundleRow[]
  canRun: boolean
  onSent: (id: number) => Promise<void>
}) {
  const [newest, ...older] = rows

  return (
    <>
      <div className="mt-8 flex flex-wrap items-baseline gap-3">
        <h2 className="font-serif text-[18px] font-medium text-ink-900">Handoff to Turkey</h2>
        <p className="text-[12.5px] text-ink-500">
          A ZIP of the photos and an Excel of the options in it. Turkey uploads these
          into Omnitron &mdash; we cannot.
        </p>
      </div>

      <div className="mt-3 overflow-hidden rounded-md border hairline bg-paper-card shadow-card">
        {!newest && (
          <div className="px-4 py-5 text-[13px] text-ink-500">
            No bundle yet. One is built after each publish, from every photo not
            already handed over.
          </div>
        )}

        {newest && (
          <div className="flex flex-wrap items-center gap-4 border-b hairline bg-brand-50 px-4 py-4">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[14px] font-semibold text-ink-900">
                  Bundle &mdash; {newest.builtAt ? formatDateTime(newest.builtAt) : ''}
                </span>
                <span
                  className={cn(
                    'inline-flex h-5 items-center gap-1 rounded-[3px] px-1.5 font-mono text-[10.5px] font-semibold',
                    newest.sentAt
                      ? 'bg-ink-100 text-ink-700'
                      : 'bg-ok-100 text-ok-600',
                  )}
                >
                  {newest.sentAt ? 'sent' : 'ready'}
                </span>
              </div>
              <div className="mt-1 font-mono text-[12px] tabular-nums text-ink-500">
                {formatInt(newest.options)} options · {formatInt(newest.files)} photos ·{' '}
                {formatBytes(newest.bytes)}
                {newest.sentAt ? ` · sent ${formatRelative(newest.sentAt)}` : ''}
              </div>
            </div>
            <div className="flex flex-none flex-wrap gap-2">
              {newest.hasZip && (
                <a
                  href={`/api/images/bundles/${newest.id}/zip`}
                  className="inline-flex h-[34px] items-center gap-1.5 rounded-[5px] bg-brand-700 px-3.5 text-[13px] font-medium text-white hover:bg-brand-800"
                >
                  <Download size={14} />
                  ZIP · {formatBytes(newest.bytes)}
                </a>
              )}
              {newest.hasXlsx && (
                <a
                  href={`/api/images/bundles/${newest.id}/xlsx`}
                  className="inline-flex h-[34px] items-center gap-1.5 rounded-[5px] border hairline bg-paper-card px-3 text-[13px] font-medium text-ink-700 hover:bg-paper"
                >
                  <FileSpreadsheet size={14} className="text-ok-600" />
                  Excel
                </a>
              )}
              {canRun && !newest.sentAt && (
                <button
                  type="button"
                  onClick={() => void onSent(newest.id)}
                  className="inline-flex h-[34px] items-center rounded-[5px] border hairline bg-paper-card px-3 font-mono text-[11px] text-ink-500 hover:text-ink-900"
                >
                  mark sent
                </button>
              )}
            </div>
          </div>
        )}

        {older.map((bundle) => (
          <div
            key={bundle.id}
            className="flex flex-wrap items-center gap-3 border-b border-ink-50 px-4 py-2.5"
          >
            <span className="w-[108px] flex-none font-mono text-[12px] text-ink-700">
              {bundle.builtAt ? formatDateTime(bundle.builtAt) : ''}
            </span>
            <span className="flex-1 font-mono text-[12px] tabular-nums text-ink-500">
              {formatInt(bundle.options)} options · {formatInt(bundle.files)} photos ·{' '}
              {formatBytes(bundle.bytes)}
            </span>
            {bundle.sentAt && (
              <span className="font-mono text-[11px] text-ink-400">
                sent {formatRelative(bundle.sentAt)}
              </span>
            )}
            <div className="flex flex-none gap-3">
              {bundle.hasZip ? (
                <a
                  href={`/api/images/bundles/${bundle.id}/zip`}
                  className="font-mono text-[11.5px] text-brand-700 hover:underline"
                >
                  ZIP
                </a>
              ) : (
                <span className="font-mono text-[11.5px] text-ink-300">cleared</span>
              )}
              {bundle.hasXlsx && (
                <a
                  href={`/api/images/bundles/${bundle.id}/xlsx`}
                  className="font-mono text-[11.5px] text-brand-700 hover:underline"
                >
                  Excel
                </a>
              )}
            </div>
          </div>
        ))}

        {rows.length > 0 && (
          <div className="px-4 py-2.5 font-mono text-[11px] text-ink-400">
            Older bundles are cleared from the server. The record of what was in them
            stays.
          </div>
        )}
      </div>
    </>
  )
}

function Tab({
  label,
  count,
  active,
  onClick,
}: {
  label: string
  count: number
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'px-3.5 pb-2.5 text-[13px] transition-colors',
        active
          ? 'font-semibold text-ink-900 shadow-[inset_0_-2px_0_0_#16513e]'
          : 'text-ink-500 hover:text-ink-700',
      )}
    >
      {label}{' '}
      <span className="font-mono text-[11.5px] tabular-nums text-ink-400">
        {formatInt(count)}
      </span>
    </button>
  )
}

function PublishedTable({ rows, total }: { rows: PublishedImageRow[]; total: number }) {
  return (
    <>
      <p className="mt-3 max-w-prose text-[12.5px] text-ink-500">
        Photos we have put on the photo server. They are not in Omnitron until Turkey
        uploads the bundle &mdash; that is what this list is waiting on.
      </p>
      <div className="mt-3 overflow-hidden rounded-md border hairline bg-paper-card shadow-card">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-paper">
                <Th>Option</Th>
                <Th>File</Th>
                <Th>Edition</Th>
                <Th>Published</Th>
                <Th>In Omnitron</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.file} className="border-b border-ink-50 hover:bg-paper">
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <span className="h-[34px] w-[26px] flex-none rounded-[3px] border border-ink-100 bg-ink-100" />
                      <span className="font-mono text-[12.5px] text-ink-900">
                        {row.modelColor}
                      </span>
                    </div>
                  </Td>
                  <Td>
                    <a
                      href={row.url}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-[12px] text-brand-700 hover:underline"
                    >
                      {row.file}
                    </a>
                  </Td>
                  <Td>
                    {row.edition === 'original' ? (
                      <span className="text-[13px] text-ink-300">&mdash;</span>
                    ) : (
                      <span className="inline-flex h-5 items-center rounded-[3px] bg-info-100 px-1.5 font-mono text-[10.5px] font-semibold text-info-600">
                        {row.edition}
                      </span>
                    )}
                  </Td>
                  <Td>
                    <span className="font-mono text-[12px] text-ink-500">
                      {row.publishedAt ? formatDateTime(row.publishedAt) : ''}
                    </span>
                  </Td>
                  <Td>
                    {row.landedAt ? (
                      <span className="font-mono text-[11px] text-ok-600">confirmed</span>
                    ) : (
                      <span className="font-mono text-[11px] text-ink-400">
                        not yet seen
                      </span>
                    )}
                  </Td>
                </tr>
              ))}
              {!rows.length && (
                <tr>
                  <td colSpan={5} className="px-3.5 py-6 text-[13px] text-ink-500">
                    Nothing published yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {rows.length > 0 && (
          <div className="border-t hairline bg-paper px-3.5 py-2.5 font-mono text-[11px] text-ink-400">
            Showing {formatInt(rows.length)} of {formatInt(total)}
          </div>
        )}
      </div>
    </>
  )
}

function AbsentTable({ rows, total }: { rows: AbsentRow[]; total: number }) {
  return (
    <>
      <p className="mt-3 max-w-prose text-[12.5px] text-ink-500">
        Searched in the brand library and not found. These need photos from the brand
        &mdash; no amount of re-running will produce them. Check the spelling column
        before asking: a footwear code we searched under only one name is a bug here,
        not a missing photo.
      </p>
      <div className="mt-3 overflow-hidden rounded-md border hairline bg-paper-card shadow-card">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-paper">
                <Th>Option</Th>
                <Th>Searched as</Th>
                <Th>Attempts</Th>
                <Th>First missed</Th>
                <Th>Last checked</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.modelColor} className="border-b border-ink-50 hover:bg-paper">
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <span className="ph-img h-[34px] w-[26px] flex-none rounded-[3px] border border-ink-100" />
                      <span className="font-mono text-[12.5px] text-ink-900">
                        {row.modelColor}
                      </span>
                    </div>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {(row.searchedAs ?? [row.modelColor]).map((spelling) => (
                        <span
                          key={spelling}
                          className="inline-flex h-5 items-center rounded-[3px] bg-ink-50 px-1.5 font-mono text-[10.5px] text-ink-700"
                        >
                          {spelling}
                        </span>
                      ))}
                      {(row.searchedAs?.length ?? 1) > 1 && (
                        <span
                          className="font-mono text-[10.5px] text-ink-400"
                          title="Our footwear codes carry a leading 7 that the brand library drops."
                        >
                          footwear
                        </span>
                      )}
                    </div>
                  </Td>
                  <Td>
                    <span className="font-mono text-[12px] tabular-nums text-ink-700">
                      searched {formatInt(row.attempts)}&times;
                    </span>
                  </Td>
                  <Td>
                    <span className="font-mono text-[12px] text-ink-500">
                      {row.firstMissedAt ? formatDateTime(row.firstMissedAt) : ''}
                    </span>
                  </Td>
                  <Td>
                    <span className="font-mono text-[12px] text-ink-500">
                      {row.lastCheckedAt ? formatRelative(row.lastCheckedAt) : ''}
                    </span>
                  </Td>
                </tr>
              ))}
              {!rows.length && (
                <tr>
                  <td colSpan={5} className="px-3.5 py-6 text-[13px] text-ink-500">
                    Every option searched for so far had a photo in the library.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {rows.length > 0 && (
          <div className="border-t hairline bg-paper px-3.5 py-2.5 font-mono text-[11px] text-ink-400">
            Showing {formatInt(rows.length)} of {formatInt(total)}
          </div>
        )}
      </div>
    </>
  )
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="border-b hairline px-3.5 py-2.5 text-left font-mono text-[10.5px] font-normal uppercase tracking-[0.14em] text-ink-500">
      {children}
    </th>
  )
}

function Td({ children }: { children: React.ReactNode }) {
  return <td className="px-3.5 py-2.5 align-middle">{children}</td>
}
