/**
 * Users — who can open this panel, and as what.
 *
 * Small on purpose. This is a handful of named people, so the page is a list
 * you can read at a glance rather than a table with a search box: if it ever
 * needs paging, something has gone wrong that paging would hide.
 *
 * Three things it has to say out loud, because getting them wrong is quiet:
 *
 * **These accounts are shared with Northwind Storefront.** One login, pick a module.
 * So a role change here changes it there, and deactivating somebody signs them
 * out of both. Said at the top rather than discovered afterwards.
 *
 * **Deactivate, not delete.** There is no delete — a removed row takes the
 * price-panel account with it and orphans every activity row pointing at the
 * id. Disabled accounts stay listed and marked, because an account nobody can
 * see is an account nobody reviews.
 *
 * **You cannot lock yourself out.** The controls on your own row that could
 * close the panel are disabled rather than hidden, with the reason on hover:
 * hiding them would read as the page being broken. The server refuses the same
 * two changes, so the page is explaining a rule, not enforcing one.
 */
import { useState } from 'react'
import { KeyRound, Plus, ShieldAlert, UserPlus, X } from 'lucide-react'
import { api, type AdminUser, type Role } from '../lib/api'
import { useApi } from '../lib/useApi'
import { useAuth } from '../lib/auth'
import { cn } from '../lib/cn'
import PageContainer from '../components/PageContainer'
import { formatDateTime, formatRelative } from '../lib/format'

/** Matches `accounts.MIN_PASSWORD_LENGTH`. Checked here so the form can say so
 *  before a round trip; the server is what enforces it. */
const MIN_PASSWORD = 10

const ROLE_TONE: Record<string, string> = {
  developer: 'bg-brand-700 text-white',
  planning: 'bg-info-600 text-white',
  manager: 'bg-ink-200 text-ink-900',
}

export default function Users() {
  const { user: me } = useAuth()
  const { data: users, error, loading, refetch } = useApi(() => api.listUsers(), [])
  const { data: roleInfo } = useApi(() => api.roles(), [])

  const [adding, setAdding] = useState(false)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [resetting, setResetting] = useState<AdminUser | null>(null)

  const roles = roleInfo?.roles ?? []

  async function run(id: number, work: () => Promise<unknown>) {
    setBusyId(id)
    setFailure(null)
    try {
      await work()
      await refetch()
    } catch (e) {
      setFailure(e instanceof Error ? e.message : 'That change did not go through.')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <PageContainer>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-[28px] font-medium tracking-tight text-ink-900">
            Users
          </h1>
          <p className="mt-2 font-mono text-[12px] text-ink-500">
            Who can open this panel, and as what
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setAdding((v) => !v)
            setFailure(null)
          }}
          className="inline-flex items-center gap-1.5 rounded-[3px] border hairline bg-paper-card px-2.5 py-1.5 font-mono text-[11px] text-ink-700 hover:bg-paper"
        >
          {adding ? <X size={12} /> : <Plus size={12} />}
          {adding ? 'Cancel' : 'Add someone'}
        </button>
      </div>

      {/* The single most surprising fact about this page, so it is the first
          thing on it rather than a footnote under the table. */}
      <div className="mt-6 flex gap-2.5 rounded-md border hairline bg-paper-card p-4 shadow-card">
        <ShieldAlert size={15} className="mt-0.5 shrink-0 text-info-600" />
        <p className="text-[13px] leading-relaxed text-ink-700">
          <span className="font-medium text-ink-900">
            These accounts are shared with Northwind Storefront.
          </span>{' '}
          One login, either module. A role change here changes it there too, and
          deactivating somebody signs them out of both. There is no delete for
          the same reason — deactivating is reversible and keeps their history
          readable.
        </p>
      </div>

      {adding && (
        <AddForm
          roles={roles}
          onCancel={() => setAdding(false)}
          onCreated={async () => {
            setAdding(false)
            await refetch()
          }}
        />
      )}

      {failure && (
        <div className="mt-4 rounded-md border hairline bg-danger-100 p-3 text-[13px] text-ink-900">
          {failure}
        </div>
      )}

      {loading && !users && (
        <div className="mt-6 h-32 animate-pulse rounded-md border hairline bg-paper-card" />
      )}
      {error && (
        <div className="mt-6 rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
          Could not load users — {error.message}
        </div>
      )}

      {users && (
        <div className="mt-6 overflow-x-auto rounded-md border hairline bg-paper-card shadow-card">
          <table className="w-full min-w-[820px] border-collapse text-[13px]">
            <thead>
              <tr className="border-b hairline">
                {['Person', 'Role', 'Added', 'Last seen', ''].map((h) => (
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
              {users.map((u) => {
                const isMe = u.id === me?.id
                const busy = busyId === u.id
                return (
                  <tr
                    key={u.id}
                    className={cn(
                      'border-b hairline align-top last:border-0',
                      !u.is_active && 'bg-paper-sunk/60',
                      busy && 'opacity-50',
                    )}
                  >
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            'font-medium',
                            u.is_active ? 'text-ink-900' : 'text-ink-500',
                          )}
                        >
                          {u.name || u.email.split('@')[0]}
                        </span>
                        {isMe && (
                          <span className="rounded-[3px] bg-brand-50 px-1.5 py-0.5 font-mono text-[10px] text-brand-700">
                            you
                          </span>
                        )}
                        {!u.is_active && (
                          <span className="rounded-[3px] bg-ink-100 px-1.5 py-0.5 font-mono text-[10px] text-ink-600">
                            deactivated
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 font-mono text-[11.5px] text-ink-500">
                        {u.email}
                      </div>
                    </td>

                    <td className="px-3 py-2.5">
                      <RolePicker
                        value={u.role}
                        roles={roles}
                        // Demoting yourself is how the panel gets closed by
                        // the one person able to reopen it. Disabled, not
                        // hidden — a missing control reads as a bug.
                        disabled={isMe || busy}
                        title={
                          isMe
                            ? 'You cannot change your own role. Ask another developer, or use the CLI on the server.'
                            : undefined
                        }
                        onChange={(role) =>
                          run(u.id, () => api.updateUser(u.id, { role }))
                        }
                      />
                    </td>

                    <td className="whitespace-nowrap px-3 py-2.5 font-mono text-[12px] text-ink-500">
                      {formatDateTime(u.created_at)}
                    </td>

                    {/* Never is a state, not a blank: this account exists and
                        has not been used. */}
                    <td className="whitespace-nowrap px-3 py-2.5 font-mono text-[12px] text-ink-500">
                      {u.last_login_at ? (
                        <span title={formatDateTime(u.last_login_at)}>
                          {formatRelative(u.last_login_at)}
                        </span>
                      ) : (
                        <span className="text-ink-400">never</span>
                      )}
                    </td>

                    <td className="px-3 py-2.5">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          onClick={() => setResetting(u)}
                          disabled={busy}
                          className="inline-flex items-center gap-1 rounded-[3px] border hairline bg-paper-card px-2 py-1 font-mono text-[11px] text-ink-700 hover:bg-paper disabled:opacity-40"
                        >
                          <KeyRound size={11} />
                          Password
                        </button>
                        <button
                          type="button"
                          disabled={isMe || busy}
                          title={
                            isMe
                              ? 'You cannot deactivate your own account.'
                              : u.is_active
                                ? 'Signs them out of both modules. Reversible.'
                                : undefined
                          }
                          onClick={() =>
                            run(u.id, () =>
                              api.updateUser(u.id, { is_active: !u.is_active }),
                            )
                          }
                          className={cn(
                            'rounded-[3px] border hairline px-2 py-1 font-mono text-[11px] disabled:opacity-40',
                            u.is_active
                              ? 'bg-paper-card text-danger-600 hover:bg-danger-100'
                              : 'bg-paper-card text-brand-700 hover:bg-brand-50',
                          )}
                        >
                          {u.is_active ? 'Deactivate' : 'Reactivate'}
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {users.length === 0 && (
            <p className="px-3 py-6 text-center text-[13px] text-ink-500">
              No accounts. The first one is made on the server with{' '}
              <code className="font-mono text-[12px]">
                python -m pim_studio.admin.users add
              </code>{' '}
              — you would not be reading this if there were none, so this is a
              database nobody has signed into yet.
            </p>
          )}
        </div>
      )}

      {/* Hidden while the form is open — it shows the blurb for whichever role
          is selected, right where the choice is being made, and two copies of
          the same three sentences is one too many. */}
      {roles.length > 0 && !adding && (
        <dl className="mt-4 space-y-1">
          {roles.map((r) => (
            <div key={r.key} className="flex gap-2 text-[12px] leading-relaxed">
              <dt className="w-[74px] shrink-0 font-mono text-[11px] text-ink-500">
                {r.key}
              </dt>
              <dd className="text-ink-500">{r.blurb}</dd>
            </div>
          ))}
        </dl>
      )}

      {resetting && (
        <PasswordDialog
          user={resetting}
          isMe={resetting.id === me?.id}
          onClose={() => setResetting(null)}
        />
      )}
    </PageContainer>
  )
}

/** The role, as one control rather than a badge beside a dropdown saying the
 *  same word. The tint carries the tier — a developer should be visible at a
 *  glance down the column — and the select carries the change. */
function RolePicker({
  value,
  roles,
  disabled,
  title,
  onChange,
}: {
  value: Role
  roles: { key: Role; blurb: string }[]
  disabled?: boolean
  title?: string
  onChange: (role: Role) => void
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      title={title ?? roles.find((r) => r.key === value)?.blurb}
      onChange={(e) => onChange(e.target.value as Role)}
      className={cn(
        'rounded-[3px] border-0 py-1 pl-2 pr-1 font-mono text-[11px] uppercase tracking-wide',
        'disabled:cursor-not-allowed disabled:opacity-50',
        ROLE_TONE[value] ?? 'bg-ink-200 text-ink-900',
      )}
    >
      {(roles.length ? roles.map((r) => r.key) : [value]).map((r) => (
        <option key={r} value={r} className="bg-paper-card text-ink-900">
          {r}
        </option>
      ))}
    </select>
  )
}

function AddForm({
  roles,
  onCancel,
  onCreated,
}: {
  roles: { key: Role; blurb: string }[]
  onCancel: () => void
  onCreated: () => Promise<void>
}) {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState<Role>('planning')
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)

  const tooShort = password.length > 0 && password.length < MIN_PASSWORD

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setFailure(null)
    try {
      await api.createUser({ email, name: name || undefined, role, password })
      await onCreated()
    } catch (err) {
      setFailure(err instanceof Error ? err.message : 'Could not create that account.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 rounded-md border hairline bg-paper-card p-4 shadow-card"
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Email">
          <input
            required
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@example.com"
            className="w-full rounded-[3px] border hairline bg-paper px-2 py-1.5 font-mono text-[12px]"
          />
        </Field>
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="optional"
            className="w-full rounded-[3px] border hairline bg-paper px-2 py-1.5 text-[13px]"
          />
        </Field>
        <Field label="Role">
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            className="w-full rounded-[3px] border hairline bg-paper px-2 py-1.5 font-mono text-[12px]"
          >
            {(roles.length ? roles : [{ key: 'planning' as Role, blurb: '' }]).map(
              (r) => (
                <option key={r.key} value={r.key}>
                  {r.key}
                </option>
              ),
            )}
          </select>
          <p className="mt-1 text-[11.5px] leading-snug text-ink-500">
            {roles.find((r) => r.key === role)?.blurb}
          </p>
        </Field>
        <Field label="First password">
          <input
            required
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-[3px] border hairline bg-paper px-2 py-1.5 font-mono text-[12px]"
          />
          <p
            className={cn(
              'mt-1 text-[11.5px]',
              tooShort ? 'text-danger-600' : 'text-ink-500',
            )}
          >
            At least {MIN_PASSWORD} characters. They can change it themselves
            once they are in.
          </p>
        </Field>
      </div>

      {failure && (
        <p className="mt-3 rounded-[3px] bg-danger-100 px-2.5 py-2 text-[12.5px] text-ink-900">
          {failure}
        </p>
      )}

      <div className="mt-4 flex items-center gap-2">
        <button
          type="submit"
          disabled={saving || tooShort}
          className="inline-flex items-center gap-1.5 rounded-[3px] bg-brand-700 px-3 py-1.5 font-mono text-[11px] text-white hover:bg-brand-600 disabled:opacity-40"
        >
          <UserPlus size={12} />
          {saving ? 'Creating…' : 'Create account'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-[3px] border hairline bg-paper-card px-3 py-1.5 font-mono text-[11px] text-ink-700 hover:bg-paper"
        >
          Cancel
        </button>
      </div>
    </form>
  )
}

/** Password reset. A dialog rather than an inline field so it cannot be
 *  submitted by tabbing past it on the way to something else. */
function PasswordDialog({
  user,
  isMe,
  onClose,
}: {
  user: AdminUser
  isMe: boolean
  onClose: () => void
}) {
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const tooShort = password.length > 0 && password.length < MIN_PASSWORD

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setFailure(null)
    try {
      await api.resetUserPassword(user.id, password)
      setDone(true)
    } catch (err) {
      setFailure(err instanceof Error ? err.message : 'Could not set that password.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-ink-900/25 p-4">
      <div className="w-full max-w-[420px] rounded-md border hairline bg-paper-card p-5 shadow-pop">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-serif text-[18px] font-medium text-ink-900">
              Set a password
            </h2>
            <p className="mt-1 font-mono text-[11.5px] text-ink-500">{user.email}</p>
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="grid h-7 w-7 place-items-center rounded hover:bg-paper-sunk"
          >
            <X size={15} />
          </button>
        </div>

        {done ? (
          <>
            <p className="mt-4 text-[13px] leading-relaxed text-ink-700">
              Done. Tell them the new password directly — it is not stored
              anywhere it could be read back, here or in the activity log.
            </p>
            {!isMe && (
              <p className="mt-2 text-[12.5px] leading-relaxed text-ink-500">
                Their current session is still open. If the reason for this was
                a leaked password, deactivate and reactivate the account to
                close it.
              </p>
            )}
            <button
              type="button"
              onClick={onClose}
              className="mt-4 rounded-[3px] bg-brand-700 px-3 py-1.5 font-mono text-[11px] text-white hover:bg-brand-600"
            >
              Close
            </button>
          </>
        ) : (
          <form onSubmit={submit} className="mt-4">
            <input
              autoFocus
              required
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-[3px] border hairline bg-paper px-2 py-1.5 font-mono text-[12px]"
            />
            <p
              className={cn(
                'mt-1 text-[11.5px]',
                tooShort ? 'text-danger-600' : 'text-ink-500',
              )}
            >
              At least {MIN_PASSWORD} characters.
            </p>

            {failure && (
              <p className="mt-3 rounded-[3px] bg-danger-100 px-2.5 py-2 text-[12.5px] text-ink-900">
                {failure}
              </p>
            )}

            <div className="mt-4 flex items-center gap-2">
              <button
                type="submit"
                disabled={saving || tooShort || !password}
                className="rounded-[3px] bg-brand-700 px-3 py-1.5 font-mono text-[11px] text-white hover:bg-brand-600 disabled:opacity-40"
              >
                {saving ? 'Saving…' : 'Set password'}
              </button>
              <button
                type="button"
                onClick={onClose}
                className="rounded-[3px] border hairline bg-paper-card px-3 py-1.5 font-mono text-[11px] text-ink-700 hover:bg-paper"
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1 block font-mono text-[10.5px] uppercase tracking-[0.1em] text-ink-500">
        {label}
      </span>
      {children}
    </label>
  )
}
