/**
 * What's new — release notes, and the target of the topbar bell.
 *
 * Entries come from CHANGELOG.md, parsed by `pim_studio/changelog.py` and
 * emitted to `public/demo/changelog.json` by `python -m pim_studio.changelog`.
 * That indirection exists because the panel has no API layer yet; when
 * `/api/changelog` lands it calls the same parser and returns the same shape,
 * so only the loader in `demoData.ts` changes.
 *
 * Acknowledgement — clearing the bell — needs a logged-in user and a write,
 * so it waits for the API. Until then the dot is driven by the build version
 * alone. Being honest about that beats faking a cleared state locally, which
 * would make the bell lie the moment a second person opened the panel.
 *
 * The markdown accepted here is deliberately small — paragraphs, **bold**, and
 * `## headings` — so a one-page feature doesn't pull in a markdown library.
 * Anything else falls through as plain text, which is fine for content we
 * write ourselves.
 */

import { Fragment, useEffect, useState } from 'react'
import { Sparkles } from 'lucide-react'
import type { ChangelogEntry, ChangelogPayload } from '../lib/api'
import { loadChangelog } from '../lib/demoData'

export default function WhatsNew() {
  const [data, setData] = useState<ChangelogPayload | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    void loadChangelog().then((payload) => {
      if (cancelled) return
      setData(payload)
      setLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const entries = data?.entries ?? []

  return (
    <div className="px-7 py-6">
      <div className="mb-1 flex items-center gap-2">
        <Sparkles size={18} className="text-brand-700" />
        <h1 className="text-[22px] font-semibold tracking-tight">What&rsquo;s new</h1>
      </div>
      <p className="mb-6 font-mono text-[12.5px] text-ink-500">
        {loading
          ? 'Loading…'
          : data?.current_version
            ? `Currently running version ${data.current_version}`
            : 'No release notes available.'}
      </p>

      {!loading && entries.length === 0 && (
        <div className="rounded-[7px] border hairline bg-paper-card px-4 py-6 text-center text-[13px] text-ink-500">
          No release notes have been written yet.
        </div>
      )}

      <div className="space-y-6">
        {entries.map((entry, i) => (
          <Entry key={entry.version} entry={entry} highlighted={i === 0} />
        ))}
      </div>
    </div>
  )
}

function Entry({ entry, highlighted }: { entry: ChangelogEntry; highlighted: boolean }) {
  return (
    <article
      className={`overflow-hidden rounded-[7px] border bg-paper-card shadow-card ${
        highlighted ? 'border-brand-300' : 'hairline'
      }`}
    >
      <header
        className={`flex items-baseline gap-3 border-b px-5 py-3 ${
          highlighted ? 'border-brand-200 bg-brand-50' : 'hairline bg-paper-50'
        }`}
      >
        <h2 className="text-[16px] font-semibold tracking-tight text-ink-800">
          Version {entry.version}
        </h2>
        <span className="font-mono text-[12px] text-ink-500">{entry.date}</span>
        {highlighted && (
          <span className="ml-auto rounded-xs bg-brand-700 px-1.5 py-0.5 font-mono text-[10.5px] uppercase tracking-wider text-white">
            Latest
          </span>
        )}
      </header>
      <div className="space-y-3 px-5 py-4 text-[13px] leading-[1.6] text-ink-700">
        {renderBody(entry.body)}
      </div>
    </article>
  )
}

/** Split a body into blocks. `### Known gaps` becomes a small subheading; the
 *  rest are paragraphs. */
function renderBody(md: string) {
  const blocks = md
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean)

  return blocks.map((block, i) => {
    const heading = block.match(/^#{2,4}\s+(.*)$/)
    if (heading) {
      return (
        <h3
          key={i}
          className="pt-1 font-mono text-[11px] uppercase tracking-wider text-ink-500"
        >
          {heading[1]}
        </h3>
      )
    }
    return <p key={i}>{renderInline(block)}</p>
  })
}

function renderInline(text: string) {
  // A very small subset: **bold** spans, `code` spans and *emphasis*. The
  // split keeps delimiter matches at odd indices so they can be wrapped on
  // render. Backticks earn their place because the entries name tables and
  // commands constantly, and raw backticks in prose read as a typo.
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return (
        <strong key={i} className="font-semibold text-ink-900">
          {part.slice(2, -2)}
        </strong>
      )
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code
          key={i}
          className="rounded-xs bg-paper-sunk px-1 py-0.5 font-mono text-[12px] text-ink-800"
        >
          {part.slice(1, -1)}
        </code>
      )
    }
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
      return (
        <em key={i} className="italic">
          {part.slice(1, -1)}
        </em>
      )
    }
    // A single newline inside a paragraph is a wrap in the source file, not a
    // line break — join with a space so reflowed prose reads correctly.
    return (
      <Fragment key={i}>
        {part.split('\n').map((line, j, arr) => (
          <Fragment key={j}>
            {line}
            {j < arr.length - 1 && ' '}
          </Fragment>
        ))}
      </Fragment>
    )
  })
}
