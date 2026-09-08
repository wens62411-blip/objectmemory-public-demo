import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { useVisiblePolling } from './useVisiblePolling'

export function useApi<T>(path: string | null, initial: T, pollInterval?: number) {
  const [data, setData] = useState<T>(initial)
  const [loading, setLoading] = useState(Boolean(path))
  const [error, setError] = useState('')
  const initialValue = useRef(initial)
  initialValue.current = initial
  const hasData = useRef(false)
  const polling = pollInterval !== undefined
  const pending = useRef<{ path: string; controller: AbortController; work: Promise<void> } | null>(null)

  const read = useCallback((signal?: AbortSignal): Promise<void> => {
    if (!path || signal?.aborted) return Promise.resolve()
    if (pending.current?.path === path && !pending.current.controller.signal.aborted) return pending.current.work
    pending.current?.controller.abort('path_changed')
    const controller = new AbortController()
    const abort = () => controller.abort(signal?.reason ?? 'poll_paused')
    signal?.addEventListener('abort', abort, { once: true })
    const timeout = window.setTimeout(() => controller.abort('read_timeout'), 10_000)
    // Background polls retain the readable card instead of flashing a spinner.
    setLoading(!hasData.current)
    const work = Promise.resolve().then(async () => {
      try {
        if (controller.signal.aborted) return
        const value = await api<T>(path, { signal: controller.signal })
        if (!controller.signal.aborted && pending.current?.controller === controller) {
          setData(value)
          hasData.current = true
          setError('')
        }
      } catch (value) {
        if (pending.current?.controller === controller) {
          if (controller.signal.reason === 'read_timeout' || controller.signal.reason === 'poll_timeout') {
            setError('读取超时，请检查服务状态后重试')
          } else if (!controller.signal.aborted) {
            setError(value instanceof Error ? value.message : '暂时无法读取数据')
          }
        }
      } finally {
        window.clearTimeout(timeout)
        signal?.removeEventListener('abort', abort)
        if (pending.current?.controller === controller) {
          pending.current = null
          setLoading(false)
        }
      }
    })
    pending.current = { path, controller, work }
    return work
  }, [path])

  // Keep the public no-argument callback suitable for retry buttons.
  const reload = useCallback(() => read(), [read])
  useEffect(() => {
    // A result from another board/item must never label the new target valid.
    hasData.current = false
    setData(initialValue.current)
    setError('')
    setLoading(Boolean(path))
    if (!polling) void read()
    return () => {
      pending.current?.controller.abort('path_changed')
      pending.current = null
    }
  }, [read, polling])
  useVisiblePolling(read, {
    enabled: Boolean(path) && polling,
    intervalMs: pollInterval ?? 5000, timeoutMs: 10_000, immediate: true, resetKey: path,
  })
  return { data, setData, loading, error, reload }
}

export function usePolling<T>(path: string | null, initial: T, interval = 5000) {
  return useApi<T>(path, initial, interval)
}
