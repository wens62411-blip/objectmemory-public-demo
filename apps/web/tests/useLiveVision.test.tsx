import { act, cleanup, renderHook } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { parseVisionPacket, useLiveVision } from '../src/hooks/useLiveVision'

// Mock WebSocket contract only: no actual JPEG decode, camera or measured 30 FPS.
const metadata = { type: 'frame', camera_id: 'camera-a', source_session_id: 'session-a', source_frame: 10,
  source_timestamp: '2026-09-06T10:00:00Z', width: 640, height: 480, coordinate_space: 'source_normalized',
  display_only: true, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, candidates: [], hands: [] }
function packet(patch: Record<string, unknown> = {}, bytes = [255, 216, 255, 219, 1, 2, 255, 217]) {
  const header = new TextEncoder().encode(JSON.stringify({ ...metadata, ...patch }))
  const buffer = new ArrayBuffer(4 + header.length + bytes.length)
  new DataView(buffer).setUint32(0, header.length, false)
  new Uint8Array(buffer, 4, header.length).set(header)
  new Uint8Array(buffer, 4 + header.length).set(bytes)
  return buffer
}

class MockSocket {
  static OPEN = 1
  static instances: MockSocket[] = []
  readyState = 0; binaryType = ''
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { if (this.readyState === 3) return; this.readyState = 3; this.onclose?.() })
  constructor(public url: string) { MockSocket.instances.push(this) }
  open() { this.readyState = 1; this.onopen?.() }
  message(data: unknown) { this.onmessage?.({ data }) }
}
let activeUrls: Set<string>, maximumUrls: number, created: number
let visibility: DocumentVisibilityState
const advance = async (milliseconds: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds) }) }
const start = async () => {
  const hook = renderHook(({ id, active }) => useLiveVision(id, active), { initialProps: { id: 'camera-a', active: true } })
  await act(async () => { await Promise.resolve() })
  const socket = MockSocket.instances[0]
  act(() => socket.open())
  return { ...hook, socket }
}

describe('Mock 二进制帧头合同：不是实际视频解码或相机验收', () => {
  it('读取大端长度的JSON来源头与JPEG Blob，保持帧会话不变', () => {
    const result = parseVisionPacket(packet())
    expect(result.metadata).toMatchObject(metadata)
    expect(result.jpeg.size).toBe(8); expect(result.jpeg.type).toBe('image/jpeg')
  })
  it.each([0, 1, 131073, 9999999])('拒绝越界帧头长度 %s', length => {
    const value = packet(); new DataView(value).setUint32(0, length, false)
    expect(() => parseVisionPacket(value)).toThrow()
  })
  it('拒绝过小/超上限包、损坏JSON和非JPEG头', () => {
    expect(() => parseVisionPacket(new ArrayBuffer(7))).toThrow()
    expect(() => parseVisionPacket(new ArrayBuffer(8 * 1024 * 1024 + 131077))).toThrow()
    const badJson = packet(); new Uint8Array(badJson)[4] = 0
    expect(() => parseVisionPacket(badJson)).toThrow()
    expect(() => parseVisionPacket(packet({}, [137, 80, 78, 71]))).toThrow()
  })
  it.each([{ width: 0 }, { width: 100000, height: 100000 }, { height: .5 }, { source_frame: -1 }, { source_frame: 1.5 }, { coordinate_space: 'pixels' }, { display_only: false }, { hands: null }, { candidates: {} }, { source_session_id: '' }, { runtime_mode: 'UNKNOWN' }])('拒绝无效来源或尺寸 %j', patch => {
    expect(() => parseVisionPacket(packet(patch))).toThrow()
  })
  it.each([{ source_timestamp: 'invalid-date' }, { source_timestamp: null }, { source_session_id: 123 }, { camera_id: '' }, { source_type: 'demo_seed', is_simulated: false }, { runtime_mode: 'REAL', is_simulated: true }])('来源字段不可信时不生成可显示帧 %j', patch => {
    expect(() => parseVisionPacket(packet(patch))).toThrow()
  })
})

describe('Mock WebSocket 背压、URL生命周期和页面可见性合同（不是30FPS实测）', () => {
  beforeEach(() => {
    vi.useFakeTimers(); MockSocket.instances = []; activeUrls = new Set(); maximumUrls = 0; created = 0; visibility = 'visible'
    vi.stubGlobal('WebSocket', MockSocket)
    const NativeURL = URL
    vi.stubGlobal('URL', class extends NativeURL {
      static createObjectURL = vi.fn(() => { const url = `blob:mock-frame-${++created}`; activeUrls.add(url); maximumUrls = Math.max(maximumUrls, activeUrls.size); return url })
      static revokeObjectURL = vi.fn((url: string) => { activeUrls.delete(url) })
    })
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
  })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('StrictMode 首轮检查不得创建随后立即取消的 CONNECTING socket', async () => {
    const { unmount } = renderHook(() => useLiveVision('camera-a', true), { wrapper: StrictMode })
    expect(MockSocket.instances).toHaveLength(0)
    await act(async () => { await Promise.resolve() })
    expect(MockSocket.instances).toHaveLength(1)
    expect(MockSocket.instances[0].close).not.toHaveBeenCalled()
    unmount()
    expect(MockSocket.instances[0].close).toHaveBeenCalledOnce()
    await advance(10000)
    expect(MockSocket.instances).toHaveLength(1)
  })

  it('连接微任务执行前已卸载则完全不创建网络连接', async () => {
    const { unmount } = renderHook(() => useLiveVision('camera-a', true))
    unmount()
    await act(async () => { await Promise.resolve() })
    await advance(10000)
    expect(MockSocket.instances).toHaveLength(0)
    expect(created).toBe(0)
  })

  it('真实握手中导航仍立即关闭已创建连接，不积累待连接资源或重试', async () => {
    const { unmount } = renderHook(() => useLiveVision('camera-a', true))
    await act(async () => { await Promise.resolve() })
    const socket = MockSocket.instances[0]
    expect(socket.readyState).toBe(0)
    unmount()
    expect(socket.close).toHaveBeenCalledOnce()
    await advance(10000)
    expect(MockSocket.instances).toHaveLength(1)
    expect(activeUrls.size).toBe(0)
  })

  it('实际img确认前没有ACK，错误帧/会话ACK不解锁，正确ACK只发一次', async () => {
    const { result, socket } = await start()
    expect(socket.binaryType).toBe('arraybuffer')
    expect(socket.url).toMatch(/\/ws\/cameras\/camera-a\/vision$/)
    act(() => socket.message(packet()))
    expect(result.current.frame?.source_frame).toBe(10)
    expect(socket.send).not.toHaveBeenCalled()
    act(() => { result.current.acknowledge(9, 'session-a'); result.current.acknowledge(10, 'wrong-session') })
    expect(socket.send).not.toHaveBeenCalled()
    act(() => { result.current.acknowledge(10, 'session-a'); result.current.acknowledge(10, 'session-a') })
    expect(socket.send).toHaveBeenCalledExactlyOnceWith(JSON.stringify({ ack: 10, source_session_id: 'session-a' }))
  })
  it('一个待解码帧期间收到多余帧立即断开，不积压队列', async () => {
    const { result, socket } = await start()
    act(() => { socket.message(packet()); socket.message(packet({ source_frame: 11 })) })
    expect(socket.close).toHaveBeenCalledOnce(); expect(result.current.frame).toBeNull()
    expect(created).toBe(1); expect(activeUrls.size).toBe(0)
  })
  it.each([{ camera_id: 'wrong-camera' }, { source_frame: 9 }, { source_frame: 10 }])('错相机或旧帧清空图像并断开 %j', async patch => {
    const { result, socket } = await start()
    act(() => socket.message(packet()))
    act(() => result.current.acknowledge(10, 'session-a'))
    act(() => socket.message(packet(patch)))
    expect(socket.close).toHaveBeenCalledOnce(); expect(result.current.frame).toBeNull(); expect(activeUrls.size).toBe(0)
  })
  it('100次模拟收帧始终最多两个Blob URL，ACK/卸载释放所有资源', async () => {
    const { result, socket, unmount } = await start()
    for (let frame = 10; frame < 110; frame++) {
      act(() => socket.message(packet({ source_frame: frame })))
      expect(activeUrls.size).toBeLessThanOrEqual(2)
      act(() => result.current.acknowledge(frame, 'session-a'))
      expect(activeUrls.size).toBe(1)
      expect(Number.isFinite(result.current.fps)).toBe(true)
    }
    expect(maximumUrls).toBe(2); expect(socket.send).toHaveBeenCalledTimes(100)
    unmount(); expect(activeUrls.size).toBe(0); expect(socket.close).toHaveBeenCalledOnce()
  })
  it('启动突发帧不足一秒不公布FPS，只计算至少一秒的解码ACK窗口', async () => {
    let clock = 0
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const { result, socket } = await start()
    for (const [index, time] of [0, 13.6, 999, 1000].entries()) {
      clock = time
      act(() => socket.message(packet({ source_frame: index + 10 })))
      act(() => result.current.acknowledge(index + 10, 'session-a'))
      expect(result.current.fps).toBe(index < 3 ? 0 : 3)
    }
  })
  it.each(['session', 'empty', 'hidden'] as const)('%s 后清空旧测量窗口，不能继承旧FPS', async reset => {
    let clock = 0
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const hook = await start()
    const { result } = hook
    let socket = hook.socket
    for (const [index, time] of [0, 500, 1000].entries()) {
      clock = time
      act(() => socket.message(packet({ source_frame: index + 10 })))
      act(() => result.current.acknowledge(index + 10, 'session-a'))
    }
    expect(result.current.fps).toBe(2)
    if (reset === 'empty') act(() => socket.message('{"type":"waiting"}'))
    if (reset === 'hidden') {
      act(() => { visibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')) })
      expect(result.current.fps).toBe(0)
      act(() => { visibility = 'visible'; document.dispatchEvent(new Event('visibilitychange')) })
      socket = MockSocket.instances[1]
      act(() => socket.open())
    }
    const session = reset === 'session' ? 'new-session' : 'session-a'
    for (const [index, time] of [1100, 2099, 2100].entries()) {
      clock = time
      act(() => socket.message(packet({ source_frame: index + 13, source_session_id: session })))
      if (index === 0) expect(result.current.fps).toBe(0)
      act(() => result.current.acknowledge(index + 13, session))
      expect(result.current.fps).toBe(index < 2 ? 0 : 2)
    }
  })
  it('未完成解码也在1.2秒过期清框，断流后有界重连', async () => {
    const { result, socket, unmount } = await start()
    act(() => socket.message(packet()))
    await advance(1200)
    expect(result.current.frame).toBeNull(); expect(activeUrls.size).toBe(0); expect(socket.close).toHaveBeenCalledOnce()
    expect(MockSocket.instances).toHaveLength(1)
    await advance(500); expect(MockSocket.instances).toHaveLength(2)
    unmount(); await advance(20000); expect(MockSocket.instances).toHaveLength(2)
  })
  it('页面隐藏关闭socket与释放URL，回到页面只创建一个连接', async () => {
    const { result, socket } = await start()
    act(() => socket.message(packet()))
    act(() => { visibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')) })
    expect(result.current.frame).toBeNull(); expect(socket.close).toHaveBeenCalledOnce(); expect(activeUrls.size).toBe(0)
    await advance(10000); expect(MockSocket.instances).toHaveLength(1)
    act(() => { visibility = 'visible'; document.dispatchEvent(new Event('visibilitychange')); document.dispatchEvent(new Event('visibilitychange')) })
    expect(MockSocket.instances).toHaveLength(2)
  })
  it('切换相机拒绝旧socket迟到消息，停用后不连接', async () => {
    const { result, socket, rerender } = await start()
    rerender({ id: 'camera-b', active: true })
    await act(async () => { await Promise.resolve() })
    expect(socket.close).toHaveBeenCalledOnce()
    act(() => socket.message(packet()))
    expect(result.current.frame).toBeNull(); expect(created).toBe(0)
    expect(MockSocket.instances[1].url).toMatch(/camera-b\/vision$/)
    rerender({ id: 'camera-b', active: false })
    expect(MockSocket.instances[1].close).toHaveBeenCalledOnce(); expect(MockSocket.instances).toHaveLength(2)
  })
  it('文本无新帧状态撤下旧图，不发送虚构ACK', async () => {
    const { result, socket } = await start()
    act(() => { socket.message(packet()); socket.message(JSON.stringify({ type: 'waiting' })) })
    expect(result.current.frame).toBeNull(); expect(activeUrls.size).toBe(0); expect(socket.send).not.toHaveBeenCalled()
  })
  it('WebSocket构造失败有错误反馈，并按退避时间重试而非永久静默', async () => {
    let attempts = 0
    vi.stubGlobal('WebSocket', class extends MockSocket {
      constructor(url: string) { if (++attempts < 3) throw new Error('constructor blocked by test'); super(url) }
    })
    const { result, unmount } = renderHook(() => useLiveVision('camera-a', true))
    await act(async () => { await Promise.resolve() })
    expect(result.current.connected).toBe(false); expect(result.current.error).not.toBe('')
    expect(attempts).toBe(1)
    await advance(499); expect(attempts).toBe(1)
    await advance(1); expect(attempts).toBe(2)
    await advance(999); expect(attempts).toBe(2)
    await advance(1); expect(attempts).toBe(3); expect(MockSocket.instances).toHaveLength(1)
    act(() => MockSocket.instances[0].open())
    expect(result.current.connected).toBe(true); expect(result.current.error).toBe('')
    unmount(); await advance(10000); expect(attempts).toBe(3)
  })
})
