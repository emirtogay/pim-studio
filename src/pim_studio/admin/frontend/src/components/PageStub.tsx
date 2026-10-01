import PageContainer from './PageContainer'

/**
 * Shared placeholder for pages whose design is final but whose data layer
 * isn't wired yet. Keeps the AppShell verifiable end-to-end without fake
 * data that would drift from real shapes. Wraps itself in `PageContainer`
 * so existing stubs render unchanged after AppShell stopped imposing one.
 */
export default function PageStub({
  title,
  subtitle,
  blurb,
}: {
  title: string
  subtitle?: string
  blurb?: string
}) {
  return (
    <PageContainer>
      <h1 className="font-serif text-[28px] font-medium tracking-tight text-ink-900">
        {title}
      </h1>
      {subtitle && (
        <p className="mt-2 font-mono text-[12px] text-ink-500">{subtitle}</p>
      )}
      <div className="mt-8 rounded-md border hairline bg-paper-card p-6 text-[13px] text-ink-500 shadow-card">
        {blurb ?? 'Not yet implemented.'}
      </div>
    </PageContainer>
  )
}
