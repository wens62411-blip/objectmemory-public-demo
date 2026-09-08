import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useApi, usePolling } from '../src/hooks/useApi'

function deferred() {
  let resolve!: (value: Response) => void
  const promise = new Promise<Response>((done) => { resolve = done })
  return { promise, resolve }
}
const response = (value: string) => new Response(JSON.stringify({ value }), { status: 200 })

describe('页面 GET 生命周期（受控网络单元测试，非硬件证明）', () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('慢请求和重复刷新只保留一次在途读取', async () => {
    vi.useFakeTimers()
    const request = deferred()
    const fetcher = vi.fn(() => request.promise)
    vi.stubGlobal('fetch', fetcher)
    const { result } = renderHook(() => usePolling('/api/firmware/status', {}, 1000))
    await act(async () => { await vi.advanceTimersByTimeAsync(3500); void result.current.reload(); void result.current.reload() })
    expect(fetcher).toHaveBeenCalledTimes(1)
    await act(async () => { request.resolve(response('fresh')); await request.promise })
    expect(result.current.data).toEqual({ value: 'fresh' })
  })

  it('切换资料后旧响应不得覆盖新资料，并取消旧请求', async () => {
    const first = deferred(), second = deferred()
    const fetcher = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    vi.stubGlobal('fetch', fetcher)
    const { result, rerender } = renderHook(({ path }) => useApi(path, {}), { initialProps: { path: '/api/items/old' } })
    await act(async () => { await Promise.resolve() })
    const oldSignal = fetcher.mock.calls[0][1].signal as AbortSignal
    rerender({ path: '/api/items/new' })
    expect(oldSignal.aborted).toBe(true)
    await act(async () => { second.resolve(response('new')); await second.promise })
    await act(async () => { first.resolve(response('old')); await first.promise })
    expect(result.current.data).toEqual({ value: 'new' })
  })

  it('隐藏页面暂停周期读取，重新可见后恢复；卸载取消请求', async () => {
    vi.useFakeTimers()
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const pending = deferred()
    const fetcher = vi.fn().mockResolvedValueOnce(response('first')).mockReturnValue(pending.promise)
    vi.stubGlobal('fetch', fetcher)
    const { unmount } = renderHook(() => usePolling('/api/cameras', {}, 1000))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    visibility.mockReturnValue('hidden')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
    expect(fetcher).toHaveBeenCalledTimes(1)
    visibility.mockReturnValue('visible')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(fetcher).toHaveBeenCalledTimes(2)
    const signal = fetcher.mock.calls[1][1].signal as AbortSignal
    unmount()
    expect(signal.aborted).toBe(true)
  })

  it('读取超时显示错误，不伪造成功或无限叠加请求', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn((_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    }))
    vi.stubGlobal('fetch', fetcher)
    const { result } = renderHook(() => useApi('/api/firmware/status', { value: 'previous' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(result.current.loading).toBe(false)
    expect(result.current.error).toContain('超时')
    expect(result.current.data).toEqual({ value: 'previous' })
  })

  it('切换固件目标立即移除旧板的通过标识', async () => {
    const second = deferred()
    const fetcher = vi.fn().mockResolvedValueOnce(response('AI verified')).mockReturnValueOnce(second.promise)
    vi.stubGlobal('fetch', fetcher)
    const { result, rerender } = renderHook(({ path }) => useApi(path, {}), { initialProps: { path: '/api/firmware/status' } })
    await act(async () => { await Promise.resolve() })
    expect(result.current.data).toEqual({ value: 'AI verified' })
    rerender({ path: '/api/firmware/status?board_model=xiao_esp32s3_sense' })
    expect(result.current.data).toEqual({})
    expect(result.current.loading).toBe(true)
    await act(async () => { second.resolve(response('S3 verified')); await second.promise })
    expect(result.current.data).toEqual({ value: 'S3 verified' })
  })

  it('只改变轮询间隔时保留已读数据，不重新闪烁加载状态', async () => {
    vi.useFakeTimers()
    const second = deferred()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response('ready')).mockReturnValue(second.promise))
    const { result, rerender } = renderHook(({ interval }) => usePolling('/api/health', {}, interval), { initialProps: { interval: 5000 } })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    rerender({ interval: 10000 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(result.current.data).toEqual({ value: 'ready' })
    expect(result.current.loading).toBe(false)
    await act(async () => { second.resolve(response('new')); await second.promise })
    expect(result.current.data).toEqual({ value: 'new' })
  })
})
