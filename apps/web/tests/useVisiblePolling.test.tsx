import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useVisiblePolling } from '../src/hooks/useVisiblePolling'

function Reader({ read }: { read: (signal: AbortSignal) => Promise<void> }) {
  useVisiblePolling(read, { intervalMs: 1500, timeoutMs: 5000 })
  return null
}

describe('只读轮询生命周期（Mock契约，不证明摄像头识别）', () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
  it('慢请求不重叠；超时中止；卸载不再读取', async () => {
    vi.useFakeTimers()
    const signals: AbortSignal[] = []
    const read = vi.fn((signal: AbortSignal) => new Promise<void>((resolve) => {
      signals.push(signal); signal.addEventListener('abort', () => resolve(), { once: true })
    }))
    const { unmount } = render(<Reader read={read} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(read).toHaveBeenCalledTimes(1)
    expect(signals[0].aborted).toBe(false)
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(signals[0].reason).toBe('poll_timeout')
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(read).toHaveBeenCalledTimes(2)
    unmount()
    expect(signals[1].aborted).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
    expect(read).toHaveBeenCalledTimes(2)
  })
  it('隐藏页面中止且不读取，恢复可见后立即恢复GET周期', async () => {
    vi.useFakeTimers()
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const read = vi.fn(async (_signal: AbortSignal) => undefined)
    render(<Reader read={read} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(read).toHaveBeenCalledTimes(1)
    visibility.mockReturnValue('hidden')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(read).toHaveBeenCalledTimes(1)
    visibility.mockReturnValue('visible')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(read).toHaveBeenCalledTimes(2)
  })
})
