import { useEffect, useRef } from 'react'

/** Read-only polling: one request cycle at a time, only while the page is visible. */
export function useVisiblePolling(task: (signal: AbortSignal) => Promise<void>, {
  enabled = true, intervalMs = 1500, timeoutMs = 8000, immediate = false, resetKey,
}: { enabled?: boolean; intervalMs?: number; timeoutMs?: number; immediate?: boolean; resetKey?: unknown } = {}) {
  const latest = useRef(task)
  latest.current = task
  const pending = useRef<Promise<void> | null>(null)
  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | null = null
    const schedule = (delay: number) => {
      window.clearTimeout(timer)
      if (!stopped && document.visibilityState !== 'hidden') timer = window.setTimeout(() => { void tick() }, delay)
    }
    const tick = async () => {
      if (stopped || document.visibilityState === 'hidden') return
      if (pending.current) { schedule(50); return }
      controller = new AbortController()
      const signal = controller.signal
      const timeout = window.setTimeout(() => controller?.abort('poll_timeout'), timeoutMs)
      const work = Promise.resolve().then(() => { if (!signal.aborted) return latest.current(signal) })
      pending.current = work
      try { await work } catch { /* The page owns error feedback; never leave an unhandled rejection. */ }
      finally {
        window.clearTimeout(timeout)
        // A Promise.all task can fail before its sibling GET settles.
        controller?.abort('poll_completed')
        if (pending.current === work) pending.current = null
        controller = null
        schedule(intervalMs)
      }
    }
    const visibility = () => {
      window.clearTimeout(timer)
      if (document.visibilityState === 'hidden') controller?.abort('poll_paused')
      else schedule(0)
    }
    document.addEventListener('visibilitychange', visibility)
    schedule(immediate ? 0 : intervalMs)
    return () => {
      stopped = true
      window.clearTimeout(timer)
      controller?.abort('poll_paused')
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [enabled, intervalMs, timeoutMs, immediate, resetKey])
}

export function reportPollingError(signal: AbortSignal) {
  return !signal.aborted || signal.reason === 'poll_timeout'
}
