/**
 * Login page.
 *
 * Minimal email + password form. On success the auth context is populated
 * and we navigate to wherever the user was trying to reach (or `/`).
 */

import { FormEvent, useState } from 'react'
import { useLocation, useNavigate, Navigate } from 'react-router-dom'
import { useAuth } from '../lib/auth'
import { ApiError } from '../lib/api'

interface LocationState {
  from?: string
}

export default function Login() {
  const { user, loading, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  // Target path comes from either React Router state (set by RequireAuth on a
  // client-side redirect) or a `?next=` query param (set by the hard redirect
  // in api.ts when a fetch 401s mid-session).
  const stateFrom = (location.state as LocationState | null)?.from
  const queryNext = new URLSearchParams(location.search).get('next')
  // Falls back to the module chooser, not the dashboard: a plain sign-in is
  // the "pick a module" moment. An explicit target still wins, so someone
  // bounced off a deep link lands back on it rather than at the chooser.
  const from = stateFrom || queryNext || '/modules'

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Already signed in? Bounce straight to the target page so /login can't
  // be used as a sign-out backdoor.
  if (!loading && user) return <Navigate to={from} replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await login(email, password)
      navigate(from, { replace: true })
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message || 'Invalid email or password')
      } else {
        setError('Unexpected error — try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-lg border hairline bg-paper-card p-7 shadow-card"
      >
        <div className="mb-5 flex items-center gap-3">
          <img
            src="/logo.png"
            alt="PIM Studio"
            className="h-10 w-10 object-contain"
          />
          <div>
            <h1 className="font-serif text-[18px] font-medium text-ink-900">
              PIM Studio
            </h1>
            <p className="font-mono text-[11.5px] text-ink-400">
              Sign in to continue
            </p>
          </div>
        </div>

        <label className="mb-3 block">
          <span className="eyebrow mb-1 block">Email or username</span>
          {/* `type="text"`, not `email` — accounts can log in with a plain
              username (e.g. "admin"), not just an email address. The backend
              matches the identifier verbatim, so HTML5 email validation would
              wrongly block valid non-email logins. */}
          <input
            type="text"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded-xs border hairline bg-white px-2.5 py-1.5 text-[13px] text-ink-900 focus:outline-none focus:ring-1 focus:ring-brand-700"
          />
        </label>

        <label className="mb-4 block">
          <span className="eyebrow mb-1 block">Password</span>
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-xs border hairline bg-white px-2.5 py-1.5 text-[13px] text-ink-900 focus:outline-none focus:ring-1 focus:ring-brand-700"
          />
        </label>

        {error && (
          <p className="mb-3 rounded-xs bg-danger-100 px-2.5 py-1.5 text-[12px] text-danger-600">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting || !email || !password}
          className="w-full rounded-xs bg-brand-700 px-3 py-2 text-[13px] font-medium text-white hover:bg-brand-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>

        <p className="mt-5 text-center font-mono text-[10.5px] text-ink-400">
          Built by Emir Togay
        </p>
      </form>
    </div>
  )
}
