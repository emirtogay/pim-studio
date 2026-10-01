import { useState } from 'react'

import { api } from '../lib/api'

/** Rebuild the extract from the panel.
 *
 * The extract is generated, not queried, and it lives on the host rather than
 * in the image — so a release that changes what it must contain leaves this
 * page empty until the nightly sync rebuilds it. Telling the reader to run a
 * script is only useful to the few who can log into the server; this runs the
 * same script, on the server, for anyone who can already see the problem.
 *
 * Developer-only server-side. A non-developer gets 403 and is told to ask,
 * rather than shown a button that cannot work.
 */
function RebuildExtract() {
  const [state, setState] = useState<'idle' | 'running' | 'failed'>('idle')
  const [error, setError] = useState('')

  async function run() {
    setState('running')
    setError('')
    try {
      await api.rebuildExtract()
      // The loaders memoise per module instance, so a reload is the honest way
      // to show the new extract rather than reaching in to clear a cache.
      window.location.reload()
    } catch (e) {
      setState('failed')
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="mt-5">
      <button
        type="button"
        onClick={run}
        disabled={state === 'running'}
        className="rounded-[5px] bg-brand-700 px-3 py-1.5 text-[13px] font-medium text-white disabled:opacity-60"
      >
        {state === 'running' ? 'Rebuilding…' : 'Rebuild the extract'}
      </button>
      <p className="mt-2 text-[12px] text-ink-500">
        {state === 'running'
          ? 'Reading the whole catalogue from the database. This takes a few minutes.'
          : 'Runs on the server, from the database. Needs no Omnitron access.'}
      </p>
      {state === 'failed' && (
        <p className="mt-2 max-w-prose text-[12.5px] text-red-700">{error}</p>
      )}
    </div>
  )
}

export default RebuildExtract
