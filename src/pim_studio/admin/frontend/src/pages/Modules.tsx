/**
 * The module chooser — the landing view after sign-in.
 *
 * One account reaches both panels (05 - Decisions Log, 2026-08-18). They are
 * separate applications on separate ports, so this is a full-page view outside
 * the AppShell rather than a page inside it: you are choosing *which* shell to
 * enter, and wrapping it in one of them would beg the question.
 *
 * The cards link out with plain anchors, not the router. The other module is a
 * different origin and a different app — a client-side navigation would only
 * fail to find the route.
 */
import { ArrowRight, ShoppingBag, Tags } from 'lucide-react'
import { api, type ModuleCard } from '../lib/api'
import { useApi } from '../lib/useApi'
import { useAuth } from '../lib/auth'

const ICONS: Record<string, typeof ShoppingBag> = {
  'price-comparison': Tags,
  'product-management': ShoppingBag,
}

export default function Modules() {
  const { user } = useAuth()
  const { data, error, loading } = useApi(() => api.modules(), [])

  const firstName = (user?.name ?? user?.email ?? '').split(/[\s@]/)[0]

  return (
    <div className="min-h-screen bg-paper text-ink-900">
      <div className="mx-auto flex min-h-screen max-w-[760px] flex-col justify-center px-6 py-16">
        <header>
          <p className="font-mono text-[12px] uppercase tracking-wide text-ink-500">
            Northwind Poland
          </p>
          <h1 className="mt-2 font-serif text-[32px] font-medium tracking-tight text-ink-900">
            {firstName ? `Welcome back, ${firstName}` : 'Welcome back'}
          </h1>
          <p className="mt-2 text-[14px] text-ink-500">
            Pick a module. The same account signs you in to both.
          </p>
        </header>

        <div className="mt-10 grid gap-4 min-[640px]:grid-cols-2">
          {loading && (
            <>
              <div className="h-[168px] animate-pulse rounded-md border hairline bg-paper-card" />
              <div className="h-[168px] animate-pulse rounded-md border hairline bg-paper-card" />
            </>
          )}

          {error && (
            <div className="col-span-full rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
              Could not load the module list — {error.message}. Both panels are
              still reachable by their own address.
            </div>
          )}

          {data?.modules.map((m: ModuleCard) => {
            const Icon = ICONS[m.key] ?? ShoppingBag
            const here = m.key === data.current
            return (
              <a
                key={m.key}
                href={here ? '/' : m.url}
                className="group flex flex-col rounded-md border hairline bg-paper-card p-6 shadow-card transition hover:border-brand-700 hover:shadow-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-700"
              >
                <div className="flex items-center justify-between">
                  <Icon size={22} className="text-brand-700" />
                  {here && (
                    <span className="rounded bg-ink-300 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-ink-900">
                      You are here
                    </span>
                  )}
                </div>
                <h2 className="mt-4 font-serif text-[19px] font-medium tracking-tight">
                  {m.name}
                </h2>
                <p className="mt-1.5 flex-1 text-[13px] leading-relaxed text-ink-500">
                  {m.blurb}
                </p>
                <span className="mt-4 inline-flex items-center gap-1.5 text-[13px] font-medium text-brand-700">
                  {here ? 'Continue' : 'Open'}
                  <ArrowRight
                    size={15}
                    className="transition-transform group-hover:translate-x-0.5"
                  />
                </span>
              </a>
            )
          })}
        </div>
      </div>
    </div>
  )
}
