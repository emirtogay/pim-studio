/**
 * Route guards.
 *
 * `<RequireAuth>` redirects unauthenticated users to /login, remembering the
 * target path so they land back on it after signing in.
 *
 * `<RequireRole>` further narrows access to specific roles; mismatched users
 * bounce to `/`, which forwards to Coverage — the one page every signed-in
 * user can see. (It was Overview until that page was switched off; the
 * redirect means this guard did not have to change with it.)
 */

import { ReactNode } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../lib/auth'
import type { Role } from '../lib/api'

export function RequireAuth({ children }: { children?: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-paper">
        <p className="font-mono text-[12px] text-ink-400">Loading…</p>
      </div>
    )
  }
  if (!user) {
    return (
      <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
    )
  }
  return <>{children ?? <Outlet />}</>
}

export function RequireRole({
  allowed,
  children,
}: {
  allowed: Role[]
  children?: ReactNode
}) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) return null
  if (!user) {
    return (
      <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
    )
  }
  if (!allowed.includes(user.role)) {
    return <Navigate to="/" replace />
  }
  return <>{children ?? <Outlet />}</>
}
