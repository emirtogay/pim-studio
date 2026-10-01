/**
 * Minimal data-fetching hook — enough to cover the admin panel's needs
 * without pulling in react-query or SWR yet.
 *
 * Two hooks:
 *
 * - `useApi(fn, deps, { pollMs? })` — runs an async `fn` that returns data,
 *   re-runs whenever any entry in `deps` changes, and (optionally) polls on
 *   an interval. Returns `{ data, error, loading, refetch }`. The fetch is
 *   cancellation-safe: if the component unmounts or deps change mid-flight,
 *   the stale response is discarded.
 *
 * - `useMutation(fn)` — for POST/PATCH flows. Returns `{ mutate, loading,
 *   error }`. Caller awaits `mutate()` and decides what to do after — this
 *   stays deliberately dumb so the caller can refetch the right queries.
 *
 * Swap in react-query later if we outgrow this. The shape (`loading /
 * error / data` triples) is chosen to match react-query so the migration
 * is mechanical.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface QueryResult<T> {
  data: T | undefined
  error: Error | undefined
  loading: boolean
  refetch: () => void
}

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
  options: { pollMs?: number } = {},
): QueryResult<T> {
  const [data, setData] = useState<T | undefined>(undefined)
  const [error, setError] = useState<Error | undefined>(undefined)
  const [loading, setLoading] = useState(true)
  // Bumping this triggers a re-fetch via the effect below.
  const [nonce, setNonce] = useState(0)
  // Track the latest fetch so an older in-flight request can't overwrite
  // a newer one's data when it resolves late.
  const generationRef = useRef(0)

  const refetch = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    const myGen = ++generationRef.current
    // Only show the loading state on the cold fetch (no data yet). For
    // refetches — whether triggered by deps changes or by the polling
    // interval — keep the previous data on screen so the grid doesn't flash
    // a skeleton every 15s. Mirrors react-query's keepPreviousData pattern.
    setData((prev) => {
      if (prev === undefined) setLoading(true)
      return prev
    })
    let cancelled = false

    fetcher()
      .then((result) => {
        if (cancelled || myGen !== generationRef.current) return
        setData(result)
        setError(undefined)
      })
      .catch((err: unknown) => {
        if (cancelled || myGen !== generationRef.current) return
        setError(err instanceof Error ? err : new Error(String(err)))
      })
      .finally(() => {
        if (cancelled || myGen !== generationRef.current) return
        setLoading(false)
      })

    let interval: ReturnType<typeof setInterval> | undefined
    if (options.pollMs && options.pollMs > 0) {
      interval = setInterval(() => setNonce((n) => n + 1), options.pollMs)
    }

    return () => {
      cancelled = true
      if (interval) clearInterval(interval)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, options.pollMs])

  return { data, error, loading, refetch }
}

export interface MutationResult<TArgs, TResult> {
  mutate: (args: TArgs) => Promise<TResult>
  loading: boolean
  error: Error | undefined
}

export function useMutation<TArgs, TResult>(
  fn: (args: TArgs) => Promise<TResult>,
): MutationResult<TArgs, TResult> {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | undefined>(undefined)

  const mutate = useCallback(
    async (args: TArgs) => {
      setLoading(true)
      setError(undefined)
      try {
        return await fn(args)
      } catch (err) {
        const e = err instanceof Error ? err : new Error(String(err))
        setError(e)
        throw e
      } finally {
        setLoading(false)
      }
    },
    [fn],
  )

  return { mutate, loading, error }
}
