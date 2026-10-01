import { useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  ShoppingBag,
  ClipboardCheck,
  BellRing,
  ChevronDown,
  LogOut,
  Menu,
  X,
  Activity,
  Clock,
  Upload,
  Image,
  Users as UsersIcon,
  ScrollText,
  Grid2x2,
  GitCompareArrows,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { api } from '../lib/api'
import { useApi } from '../lib/useApi'
import { canSeeAdmin, canSeeDeveloperOnly, useAuth } from '../lib/auth'

/**
 * AppShell matches `docs/design/App Shell.html` from the design handoff —
 * the deliberately-quiet variant: 52px top bar with logo + user menu only,
 * 220px sidebar with three primary nav items plus a collapsible Admin group.
 *
 * Sidebar Admin items are role-gated: managers see no Admin section at all;
 * planning users see Imports but not Sync Runs / Schedules; developers see
 * everything.
 */
export default function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const { user, logout } = useAuth()

  const showAdmin = user ? canSeeAdmin(user.role) : false
  const showDevOnly = user ? canSeeDeveloperOnly(user.role) : false

  const adminPaths = ['/admin/imports', '/admin/conflicts', '/admin/runs', '/admin/schedules', '/admin/users', '/admin/activity']
  const [adminOpen, setAdminOpen] = useState(() =>
    adminPaths.some((p) => location.pathname.startsWith(p))
  )

  // Build version, surfaced in the sidebar footer. Fetched once on mount;
  // changes only when the server is redeployed, no polling needed.
  const { data: versionInfo } = useApi(() => api.version(), [])

  async function handleSignOut() {
    setUserMenuOpen(false)
    await logout()
    navigate('/login', { replace: true })
  }

  // Initials for the avatar — take the first letters of the first two words.
  const initials = (user?.name ?? user?.email ?? '?')
    .split(/[\s@]/)
    .filter(Boolean)
    .map((w) => w[0]?.toUpperCase() ?? '')
    .slice(0, 2)
    .join('') || '?'

  const roleBadgeTone: Record<string, string> = {
    developer: 'bg-brand-700 text-white',
    planning: 'bg-info-600 text-white',
    manager: 'bg-ink-300 text-ink-900',
  }

  return (
    <div className="min-h-screen bg-paper text-ink-900">
      {/* Top bar */}
      <header className="fixed inset-x-0 top-0 z-30 flex h-[52px] items-center justify-between border-b hairline bg-paper/90 backdrop-blur">
        <div className="flex w-[220px] shrink-0 items-center gap-2.5 px-4">
          <button
            aria-label="Open menu"
            className="mr-1 hidden h-8 w-8 items-center justify-center rounded hover:bg-paper-sunk max-[900px]:flex"
            onClick={() => setMobileOpen((v) => !v)}
          >
            {mobileOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
          <img
            src="/logo.png"
            alt="PIM Studio"
            className="h-7 w-7 shrink-0 object-contain"
          />
          <span className="text-[14px] font-semibold tracking-tight">PIM Studio</span>
        </div>

        <div className="flex items-center gap-1 pr-5">
          {/* Back to the module chooser. A link rather than a dropdown of the
              other panels: the chooser already lists them and knows which are
              configured, so duplicating that here would be a second place to
              keep correct. */}
          <NavLink
            to="/modules"
            aria-label="Switch module"
            title="Switch module"
            className="grid h-9 w-9 place-items-center rounded-md text-ink-500 hover:bg-paper-sunk hover:text-ink-700"
          >
            <Grid2x2 size={16} />
          </NavLink>

          {/* What's-new bell. Pulses with a small dot when the running build's
              version is newer than this user's last_seen_changelog_version. */}
          <NavLink
            to="/whats-new"
            aria-label="What's new"
            title="What's new"
            className="relative grid h-9 w-9 place-items-center rounded-md text-ink-500 hover:bg-paper-sunk hover:text-ink-700"
          >
            <BellRing size={16} />
            {user?.changelog_unread && (
              <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-brand-700 ring-2 ring-paper" />
            )}
          </NavLink>

        <div className="relative">
          <button
            onClick={() => setUserMenuOpen((v) => !v)}
            className="flex items-center gap-2 rounded-md p-1 pr-2 hover:bg-paper-sunk"
            aria-haspopup="menu"
            aria-expanded={userMenuOpen}
          >
            <div className="grid h-7 w-7 place-items-center rounded-full bg-ink-900 text-[11px] font-semibold tracking-wider text-white">
              {initials}
            </div>
            <span className="text-[13px] font-medium">{user?.name ?? user?.email ?? '—'}</span>
            {user && (
              <span
                className={cn(
                  'ml-0.5 rounded-xs px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide',
                  roleBadgeTone[user.role] ?? 'bg-ink-200 text-ink-900',
                )}
              >
                {user.role}
              </span>
            )}
            <ChevronDown
              size={14}
              className={cn(
                'text-ink-400 transition-transform',
                userMenuOpen && 'rotate-180'
              )}
            />
          </button>
          {userMenuOpen && (
            <div
              role="menu"
              className="absolute right-0 top-[calc(100%+4px)] z-40 min-w-[220px] rounded-md border hairline bg-paper-card p-1 shadow-pop"
            >
              <div className="cursor-default truncate px-2.5 py-1.5 text-[12px] text-ink-500">
                {user?.email ?? '—'}
              </div>
              <div className="mx-1 my-1 h-px bg-hairline" />
              <button
                type="button"
                role="menuitem"
                onClick={handleSignOut}
                className="flex h-8 w-full items-center gap-2 rounded px-2.5 text-[13px] text-ink-700 hover:bg-paper-sunk"
              >
                <LogOut size={14} />
                Sign out
              </button>
            </div>
          )}
        </div>
        </div>
      </header>

      {/* Mobile overlay */}
      {mobileOpen && (
        <button
          aria-label="Close menu"
          onClick={() => setMobileOpen(false)}
          className="fixed inset-0 z-10 hidden bg-ink-900/20 max-[900px]:block"
        />
      )}

      {/* Sidebar */}
      <aside
        className={cn(
          'fixed bottom-0 left-0 top-[52px] z-20 flex w-[220px] flex-col overflow-y-auto border-r hairline bg-paper-card',
          'max-[900px]:-translate-x-full max-[900px]:transition-transform max-[900px]:duration-200',
          mobileOpen && 'max-[900px]:translate-x-0'
        )}
      >
        <nav className="flex-1 p-2.5">
          {/* No Overview entry while `/` is a redirect to Coverage — a nav
              item that bounces you somewhere else is worse than no item. */}
          <NavItem to="/products" icon={<ShoppingBag size={16} />} label="Products" />
          <NavItem to="/coverage" icon={<ClipboardCheck size={16} />} label="Coverage" />
          {/* A module, not an admin setting. It answers a standing question —
              which options have no photo — the same way Coverage answers what
              is missing, and it is opened on a cadence rather than when
              something needs configuring. Under Admin it read as plumbing. */}
          <NavItem to="/images" icon={<Image size={16} />} label="Images" />

          {showAdmin && (
            <>
              <div className="h-5" />
              <button
                onClick={() => setAdminOpen((v) => !v)}
                className="flex h-[34px] w-full items-center gap-2.5 rounded px-2.5 text-[13px] font-medium text-ink-500 hover:bg-paper-sunk"
              >
                <Activity size={16} className="text-ink-300" />
                <span>Admin</span>
                <ChevronDown
                  size={13}
                  className={cn(
                    'ml-auto text-ink-300 transition-transform',
                    adminOpen && 'rotate-180'
                  )}
                />
              </button>
              <div
                className={cn(
                  'overflow-hidden transition-[max-height] duration-200',
                  adminOpen ? 'max-h-[340px]' : 'max-h-0'
                )}
              >
                <SubItem to="/admin/imports" icon={<Upload size={14} />} label="Imports" />
                <SubItem
                  to="/admin/conflicts"
                  icon={<GitCompareArrows size={14} />}
                  label="Conflicts"
                />
                {showDevOnly && (
                  <SubItem to="/admin/runs" icon={<Activity size={14} />} label="Sync Runs" />
                )}
                {showDevOnly && (
                  <SubItem to="/admin/schedules" icon={<Clock size={14} />} label="Schedules" />
                )}
                {showDevOnly && (
                  <SubItem to="/admin/users" icon={<UsersIcon size={14} />} label="Users" />
                )}
                {showDevOnly && (
                  <SubItem to="/admin/activity" icon={<ScrollText size={14} />} label="Activity Log" />
                )}
              </div>
            </>
          )}
        </nav>

        <div className="border-t hairline p-3 text-[11px] text-ink-400">
          <div>PIM Studio · by Emir Togay</div>
          {versionInfo && (
            <NavLink
              to="/whats-new"
              className="mt-0.5 block font-mono text-ink-300 hover:text-brand-700"
            >
              v{versionInfo.version}
            </NavLink>
          )}
        </div>
      </aside>

      {/* Main */}
      <main className="ml-[220px] min-h-[calc(100vh-52px)] pt-[52px] max-[900px]:ml-0">
        <Outlet />
      </main>
    </div>
  )
}

/** Primary nav item. The active state is the design's 2px left-edge green
 *  accent against a brand-50 background. */
function NavItem({
  to,
  icon,
  label,
  end,
}: {
  to: string
  icon: React.ReactNode
  label: string
  end?: boolean
}) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          'relative mb-0.5 flex h-[34px] items-center gap-2.5 rounded px-2.5 text-[13px] font-medium transition-colors',
          isActive
            ? 'bg-brand-50 text-brand-900'
            : 'text-ink-700 hover:bg-paper-sunk'
        )
      }
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <span className="absolute inset-y-[6px] -left-[10px] w-0.5 rounded bg-brand-700" />
          )}
          <span className={cn(isActive ? 'text-brand-700' : 'text-ink-500')}>{icon}</span>
          <span>{label}</span>
        </>
      )}
    </NavLink>
  )
}

/** Nested Admin sub-item — indented and smaller than NavItem. */
function SubItem({
  to,
  icon,
  label,
}: {
  to: string
  icon: React.ReactNode
  label: string
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        cn(
          'mb-0.5 flex h-[30px] items-center gap-2 rounded pl-9 pr-2.5 text-[12.5px] transition-colors',
          isActive
            ? 'bg-brand-50 text-brand-900[&_svg]:text-brand-700'
            : 'text-ink-500 hover:bg-paper-sunk hover:text-ink-700'
        )
      }
    >
      <span className="text-ink-400">{icon}</span>
      <span>{label}</span>
    </NavLink>
  )
}
