/**
 * Activity — what has been done to this module, and by whom.
 *
 * Two audiences arrive with different questions and the page answers both
 * without making either wade through the other:
 *
 * - **Who has been getting in?** Sign-ins, and especially the ones that
 *   failed. Failed attempts are pulled to the top as a count, because they
 *   are the only rows here that might mean something is wrong right now.
 * - **Who changed that?** Every mutating request, with what it moved.
 *
 * **The page says what it does not cover.** It holds mutations, sign-ins and
 * the account CLI's writes — not the nightly sync, which has its own page with
 * row counts and a staleness verdict no audit row could carry, and not reads.
 * A page that looks like a record of everything, while being a record of one
 * kind of thing, is how this codebase has been wrong four times already.
 *
 * **And where the record starts.** Nothing here is backfillable, so a filter
 * that matches nothing and a log that began on Tuesday produce the same empty
 * table. The footer says which one you are looking at.
 */
import { useState } from 'react'
import { ScrollText, ShieldAlert } from 'lucide-react'
import { api, type AuditEntry } from '../lib/api'
import { useApi } from '../lib/useApi'
import { cn } from '../lib/cn'
import PageContainer from '../components/PageContainer'
import { formatDateTime, formatInt, formatRelative } from '../lib/format'

const PAGE_SIZE = 100

/** 0 is "the whole record", and it is the default. This log is young — a
 *  default window would hide most of it on a page whose job is showing what
 *  happened. */
const WINDOWS: { label: string; days: number }[] = [
  { label: 'All', days: 0 },
  { label: '24h', days: 1 },
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
]

export default function Activity() {
  const [action, setAction] = useState('')
  const [userEmail, setUserEmail] = useState('')
  const [days, setDays] = useState(0)
  const [offset, setOffset] = useState(0)

  const { data, error, loading } = useApi(
    () =>
      api.audit({
        action: action || undefined,
        user_email: userEmail || undefined,
        days,
        limit: PAGE_SIZE,
        offset,
      }),
    [action, userEmail, days, offset],
  )

  // Any filter change starts a new list; keeping the offset would land you on
  // page 3 of a result with one page.
  function refine(fn: () => void) {
    fn()
    setOffset(0)
  }

  const failedSignIns =
    data?.actions.find((a) => a.action === 'sign-in failed')?.count ?? 0
  const filtered = Boolean(action || userEmail)

  return (
    <PageContainer>
      <h1 className="font-serif text-[28px] font-medium tracking-tight text-ink-900">
        Activity log
      </h1>
      <p className="mt-2 font-mono text-[12px] text-ink-500">
        What has been done to this module, and by whom
      </p>

      {/* Failed sign-ins are the only rows here that can mean something is
          wrong right now, so they get a line of their own rather than a chip
          in a row of chips. */}
      {failedSignIns > 0 && (
        <button
          type="button"
          onClick={() => refine(() => setAction('sign-in failed'))}
          className="mt-6 flex w-full gap-2.5 rounded-md border hairline bg-warn-100 p-4 text-left shadow-card hover:bg-warn-100/70"
        >
          <ShieldAlert size={15} className="mt-0.5 shrink-0 text-warn-600" />
          <p className="text-[13px] leading-relaxed text-ink-900">
            <span className="font-medium">
              {formatInt(failedSignIns)} failed sign-in
              {failedSignIns === 1 ? '' : 's'}
            </span>{' '}
            <span className="text-ink-700">
              {days === 0 ? 'on record' : `in the last ${days} days`}. Each row
              carries the address exactly as it was typed, not normalised, so a
              near-miss of a real account is still readable as one.
            </span>
          </p>
        </button>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-3">
        <ChipRow label="Window">
          {WINDOWS.map((w) => (
            <Chip
              key={w.days}
              active={days === w.days}
              onClick={() => refine(() => setDays(w.days))}
            >
              {w.label}
            </Chip>
          ))}
        </ChipRow>

        {data && data.users.length > 0 && (
          <ChipRow label="Who">
            <Chip active={!userEmail} onClick={() => refine(() => setUserEmail(''))}>
              everyone
            </Chip>
            {data.users.map((u) => (
              <Chip
                key={u.email}
                active={userEmail === u.email}
                onClick={() =>
                  refine(() => setUserEmail(userEmail === u.email ? '' : u.email))
                }
              >
                {u.email} · {formatInt(u.count)}
              </Chip>
            ))}
          </ChipRow>
        )}
      </div>

      {data && data.actions.length > 0 && (
        <div className="mt-3">
          <ChipRow label="What">
            <Chip active={!action} onClick={() => refine(() => setAction(''))}>
              everything
            </Chip>
            {data.actions.map((a) => (
              <Chip
                key={a.action}
                active={action === a.action}
                tone={data.accessActions.includes(a.action) ? 'access' : 'plain'}
                onClick={() =>
                  refine(() => setAction(action === a.action ? '' : a.action))
                }
              >
                {a.action} · {formatInt(a.count)}
              </Chip>
            ))}
          </ChipRow>
        </div>
      )}

      {loading && !data && (
        <div className="mt-6 h-40 animate-pulse rounded-md border hairline bg-paper-card" />
      )}
      {error && (
        <div className="mt-6 rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
          Could not load the activity log — {error.message}
        </div>
      )}

      {data && (
        <>
          <div className="mt-4 overflow-x-auto rounded-md border hairline bg-paper-card shadow-card">
            <table className="w-full min-w-[880px] border-collapse text-[13px]">
              <thead>
                <tr className="border-b hairline">
                  {['When', 'Who', 'What', 'Detail', 'Where'].map((h) => (
                    <th
                      key={h}
                      className="px-3 py-2 text-left font-mono text-[10.5px] font-semibold uppercase tracking-[0.1em] text-ink-500"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <Row key={row.id} row={row} access={data.accessActions} />
                ))}
              </tbody>
            </table>

            {data.items.length === 0 && (
              <p className="px-3 py-6 text-center text-[13px] text-ink-500">
                {filtered
                  ? 'Nothing matches those filters.'
                  : data.firstAt
                    ? 'Nothing in this window.'
                    : 'Nothing recorded yet. The log fills as people sign in and change things.'}
              </p>
            )}
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <p className="font-mono text-[11px] text-ink-500">
              {data.total > PAGE_SIZE
                ? `${formatInt(offset + 1)}–${formatInt(
                    Math.min(offset + PAGE_SIZE, data.total),
                  )} of ${formatInt(data.total)}`
                : `${formatInt(data.total)} ${data.total === 1 ? 'entry' : 'entries'}`}
            </p>
            {data.total > PAGE_SIZE && (
              <div className="flex gap-1.5">
                <Pager
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Newer
                </Pager>
                <Pager
                  disabled={offset + PAGE_SIZE >= data.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Older
                </Pager>
              </div>
            )}
          </div>

          {/* What the page covers, and where its record begins. Both are
              things an empty table cannot say for itself. */}
          <div className="mt-6 flex gap-2.5 rounded-md border hairline bg-paper-card p-4 text-[12.5px] leading-relaxed text-ink-500 shadow-card">
            <ScrollText size={14} className="mt-0.5 shrink-0 text-ink-400" />
            <div>
              <p>
                This log holds requests that changed something, sign-ins, and
                account changes made on the server with the CLI — marked{' '}
                <code className="font-mono text-[11.5px]">CLI</code>. It does
                not hold reads, and it does not hold the nightly sync, which is
                on <span className="text-ink-700">Sync Runs</span> with the row
                counts and staleness an audit row could not carry.
              </p>
              <p className="mt-1.5">
                {data.firstAt ? (
                  <>
                    The record starts {formatRelative(data.firstAt)} —{' '}
                    <span className="font-mono text-[11.5px]">
                      {formatDateTime(data.firstAt)}
                    </span>
                    . Nothing before that was kept, and none of it can be
                    backfilled.
                  </>
                ) : (
                  'Nothing has been recorded yet, so there is no record to start from.'
                )}
              </p>
            </div>
          </div>
        </>
      )}
    </PageContainer>
  )
}

function Row({ row, access }: { row: AuditEntry; access: string[] }) {
  const failed = row.status_code !== null && row.status_code >= 400
  const isAccess = access.includes(row.action)

  return (
    <tr className="border-b hairline align-top last:border-0">
      <td className="whitespace-nowrap px-3 py-2 font-mono text-[12px] text-ink-700">
        <span title={formatRelative(row.created_at)}>
          {formatDateTime(row.created_at)}
        </span>
      </td>
      <td className="px-3 py-2 font-mono text-[12px] text-ink-900">
        {row.user_email ?? (
          // A mutating request nobody was signed in for. Rare, and worth
          // seeing rather than blanking.
          <span className="text-ink-400" title="No authenticated session">
            not signed in
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        <span
          className={cn(
            'rounded-[3px] px-1.5 py-0.5 font-mono text-[11px]',
            failed
              ? 'bg-danger-100 text-danger-600'
              : isAccess
                ? 'bg-info-100 text-info-600'
                : 'bg-paper-sunk text-ink-700',
          )}
        >
          {row.action}
        </span>
      </td>
      <td className="px-3 py-2 text-[12.5px] leading-snug text-ink-700">
        {row.detail ?? <span className="text-ink-300">—</span>}
      </td>
      <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] text-ink-500">
        <div className="flex items-center gap-1.5">
          {row.method && (
            <span
              className={cn(
                'rounded-[3px] px-1 py-0.5',
                row.method === 'CLI'
                  ? 'bg-ink-100 text-ink-600'
                  : 'text-ink-400',
              )}
              title={row.method === 'CLI' ? 'Run on the server, not in the panel' : undefined}
            >
              {row.method}
            </span>
          )}
          {row.status_code !== null && (
            <span className={cn(failed && 'font-semibold text-danger-600')}>
              {row.status_code}
            </span>
          )}
          {row.ip && <span className="text-ink-400">{row.ip}</span>}
        </div>
      </td>
    </tr>
  )
}

function ChipRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mr-0.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-ink-500">
        {label}
      </span>
      {children}
    </div>
  )
}

function Chip({
  active,
  tone = 'plain',
  onClick,
  children,
}: {
  active: boolean
  tone?: 'plain' | 'access'
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'rounded-[3px] border hairline px-2 py-1 font-mono text-[11px] transition-colors',
        active
          ? 'border-brand-700 bg-brand-50 text-brand-900'
          : tone === 'access'
            ? 'bg-paper-card text-info-600 hover:bg-paper'
            : 'bg-paper-card text-ink-700 hover:bg-paper',
      )}
    >
      {children}
    </button>
  )
}

function Pager({
  disabled,
  onClick,
  children,
}: {
  disabled: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="rounded-[3px] border hairline bg-paper-card px-2.5 py-1 font-mono text-[11px] text-ink-700 hover:bg-paper disabled:opacity-40"
    >
      {children}
    </button>
  )
}
