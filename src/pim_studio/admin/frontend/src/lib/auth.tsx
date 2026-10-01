/**
 * Auth context + `useAuth()` hook.
 *
 * On mount, the provider calls `/api/auth/me`. If the server returns 401 the
 * cookie is absent or expired, so we route the user to `/login`. If it
 * returns a user, the app renders as usual.
 *
 * Role helpers (`canSeeAdmin`, `canSeeDeveloperOnly`) mirror the backend's
 * role tiers so the UI and the API agree on who can see what.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError, type AuthUser, type Role } from './api'

interface AuthContextValue {
  user: AuthUser | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

export function AuthProvider({
  children,
  initialUser,
}: {
  children: ReactNode
  /**
   * Skip the `/api/auth/me` round trip and start with this user.
   *
   * Only for the dev preview harness, which renders the real app against
   * fixtures with no backend to authenticate against. Production mounts the
   * provider without it, so the session cookie remains the only way to
   * become authenticated.
   */
  initialUser?: AuthUser
}) {
  const [user, setUser] = useState<AuthUser | null>(initialUser ?? null)
  const [loading, setLoading] = useState(initialUser === undefined)

  const refresh = useCallback(async () => {
    try {
      const res = await api.me()
      setUser(res.user)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setUser(null)
      } else {
        throw err
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (initialUser !== undefined) return
    refresh()
  }, [refresh, initialUser])

  const login = useCallback(async (email: string, password: string) => {
    const res = await api.login(email, password)
    setUser(res.user)
  }, [])

  const logout = useCallback(async () => {
    await api.logout()
    setUser(null)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading, login, logout, refresh }),
    [user, loading, login, logout, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (ctx === undefined) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return ctx
}

// ── Role helpers ─────────────────────────────────────────────────────────

/** Can this role see any /admin/* page? Managers cannot. */
export function canSeeAdmin(role: Role): boolean {
  return role === 'developer' || role === 'planning'
}

/** Can this role see developer-only pages (Scrape Runs + Schedules)? */
export function canSeeDeveloperOnly(role: Role): boolean {
  return role === 'developer'
}
