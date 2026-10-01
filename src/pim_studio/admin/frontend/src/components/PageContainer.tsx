import type { ReactNode } from 'react'

/**
 * Default content wrapper for the common page layout: centered column with a
 * max-width and generous padding. Overview, Products, Channel Stores, and
 * Updates all use this. Scrape Runs and Schedules opt out and manage their
 * own layout (dense tables with a right sidebar).
 *
 * AppShell intentionally doesn't impose this container — pushing it into
 * each page keeps the shell agnostic to content shape.
 */
export default function PageContainer({ children }: { children: ReactNode }) {
  return <div className="mx-auto max-w-content px-10 py-12">{children}</div>
}
