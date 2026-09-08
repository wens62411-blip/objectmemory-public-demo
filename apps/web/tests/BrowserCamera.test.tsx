import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BrowserCamera } from '../src/components/BrowserCamera'

class DelayedCloseSocket {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
  static instances: DelayedCloseSocket[] = []
  readyState = DelayedCloseSocket.OPEN
  bufferedAmount = 0
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  constructor(public url: string) { DelayedCloseSocket.instances.push(this) }
  send(value: unknown) { this.sent.push(value) }
  close() { this.readyState = DelayedCloseSocket.CLOSED }
  finishClose() { this.onclose?.() }
  message(value: unknown) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(value) })) }
}

function uploadHarness() {
  vi.useFakeTimers()
  DelayedCloseSocket.instances = []
  vi.stubGlobal('WebSocket', DelayedCloseSocket)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['frame'], { type: 'image/jpeg' })))
}
async function tick(ms = 0) { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

describe('浏览器直连摄像头', () => {
  const stop = vi.fn()
  const track = {
    stop,
    getSettings: () => ({ deviceId: 'camera-built-in', width: 640, height: 480 }),
    readyState: 'live' as MediaStreamTrackState,
    onended: null as null | (() => void),
  }
  const stream = { active: true, getTracks: () => [track], getVideoTracks: () => [track] }

  beforeEach(() => {
    stop.mockClear()
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue(stream),
      enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'videoinput', deviceId: 'camera-built-in', label: 'ASUS 内置摄像头', groupId: '', toJSON: () => ({}) }]),
    } })
    Object.defineProperty(navigator, 'permissions', { configurable: true, value: { query: vi.fn().mockResolvedValue({ state: 'prompt', onchange: null }) } })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    Object.defineProperty(HTMLMediaElement.prototype, 'readyState', { configurable: true, get: () => HTMLMediaElement.HAVE_ENOUGH_DATA })
    Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', { configurable: true, get: () => 640 })
    Object.defineProperty(HTMLVideoElement.prototype, 'videoHeight', { configurable: true, get: () => 480 })
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
  })

  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('手机HTTP仅能管理和上传文件，不申请持续摄像头或建议手机访问localhost', () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false })
    render(<BrowserCamera cameraId="phone" />)
    expect(screen.getByRole('button', { name: /授权并选择摄像头/ })).toBeDisabled()
    expect(screen.getByRole('note')).toHaveTextContent('localhost 仅指当前设备')
    expect(screen.getByRole('note')).toHaveTextContent('手机信任的 HTTPS')
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  })

  it.each(['cancel', 'unmount', 'switch', 'hidden'])('授权迟到时%s不能重启采集或上传', async action => {
    let resolve!: (value: MediaStream) => void
    vi.mocked(navigator.mediaDevices.getUserMedia).mockReturnValue(new Promise(done => { resolve = done }))
    const socket = vi.fn(); vi.stubGlobal('WebSocket', socket)
    const view = render(<BrowserCamera cameraId="old-camera" />)
    fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ }))
    if (action === 'cancel') fireEvent.click(screen.getByRole('button', { name: '取消摄像头请求' }))
    if (action === 'unmount') view.unmount()
    if (action === 'switch') view.rerender(<BrowserCamera cameraId="new-camera" />)
    if (action === 'hidden') {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
      fireEvent(document, new Event('visibilitychange'))
    }
    await act(async () => { resolve(stream as unknown as MediaStream); await Promise.resolve() })
    expect(stop).toHaveBeenCalled()
    expect(socket).not.toHaveBeenCalled()
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled()
  })

  it('播放失败释放已申请轨道，不能只把UI改成未预览', async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValue(new Error('play failed'))
    render(<BrowserCamera />)
    fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ }))
    await waitFor(() => expect(stop).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: /授权并选择摄像头/ })).toBeEnabled()
  })

  it('旧socket迟到close/ready不清掉新通道，重复或未知ACK不虚增接收帧数', async () => {
    uploadHarness()
    const observed = vi.fn()
    const view = render(<BrowserCamera cameraId="old" targetFps={8} onState={observed} />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ })) })
    const old = DelayedCloseSocket.instances[0]
    act(() => old.message({ type: 'ready', camera_id: 'old' }))
    await tick(125)
    act(() => old.message({ type: 'ack', sequence: 1 }))
    expect(observed.mock.calls.at(-1)![0].uploadedFrames).toBe(1)
    view.rerender(<BrowserCamera cameraId="new" targetFps={8} onState={observed} />)
    expect(observed.mock.calls.at(-1)![0].uploadedFrames).toBe(0)
    const current = DelayedCloseSocket.instances[1]
    act(() => { current.message({ type: 'ready', camera_id: 'new' }); old.finishClose(); old.message({ type: 'ready', camera_id: 'old' }) })
    await tick(125)
    expect(current.sent).toHaveLength(1)
    expect(old.sent).toHaveLength(1)
    expect(observed.mock.calls.at(-1)![0].uploadedFrames).toBe(0)
    act(() => { current.message({ type: 'ack', sequence: 9 }); old.message({ type: 'ack', sequence: 1 }) })
    expect(observed.mock.calls.at(-1)![0].uploadedFrames).toBe(0)
    act(() => { current.message({ type: 'ack', sequence: 1 }); current.message({ type: 'ack', sequence: 1 }) })
    expect(observed.mock.calls.at(-1)![0].uploadedFrames).toBe(1)
    await tick(125)
    expect(current.sent).toHaveLength(2)
  })

  it('编码迟到不能把停止前旧帧传给更换后的摄像头', async () => {
    uploadHarness()
    let encoded!: BlobCallback
    vi.mocked(HTMLCanvasElement.prototype.toBlob).mockImplementation(callback => { encoded = callback })
    const view = render(<BrowserCamera cameraId="old" targetFps={8} />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ })) })
    const old = DelayedCloseSocket.instances[0]
    act(() => old.message({ type: 'ready', camera_id: 'old' }))
    await tick(125)
    view.rerender(<BrowserCamera cameraId="new" targetFps={8} />)
    act(() => encoded(new Blob(['old-frame'], { type: 'image/jpeg' })))
    expect(old.sent).toHaveLength(0)
    expect(DelayedCloseSocket.instances[1].sent).toHaveLength(0)
  })

  it('握手身份不一致或没有ready时不发送视频并给出可重连入口', async () => {
    uploadHarness()
    render(<BrowserCamera cameraId="camera" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ })) })
    const first = DelayedCloseSocket.instances[0]
    act(() => first.message({ type: 'ready', camera_id: 'another' }))
    expect(first.sent).toHaveLength(0)
    expect(screen.getByText(/摄像头身份不一致/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新连接上传' }))
    await tick(5000)
    expect(DelayedCloseSocket.instances[1].sent).toHaveLength(0)
    expect(screen.getByText(/5 秒内没有确认采集通道/)).toBeInTheDocument()
  })

  it('后台页面立即停止轨道，回到前台不擅自重启', async () => {
    uploadHarness()
    const visible = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    render(<BrowserCamera cameraId="camera" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ })) })
    act(() => DelayedCloseSocket.instances[0].message({ type: 'ready', camera_id: 'camera' }))
    visible.mockReturnValue('hidden'); fireEvent(document, new Event('visibilitychange'))
    await tick(1000)
    expect(stop).toHaveBeenCalled()
    expect(DelayedCloseSocket.instances[0].sent).toHaveLength(0)
    visible.mockReturnValue('visible'); fireEvent(document, new Event('visibilitychange'))
    await tick(1000)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /授权并选择摄像头/ })).toBeEnabled()
  })

  it('只有用户点击后才请求权限，并显示真实设备名', async () => {
    const media = navigator.mediaDevices as MediaDevices
    const view = render(<BrowserCamera />)
    expect(media.getUserMedia).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ }))
    await waitFor(() => expect(media.getUserMedia).toHaveBeenCalledTimes(1))
    expect(await screen.findByRole('option', { name: 'ASUS 内置摄像头' })).toBeInTheDocument()
    expect(screen.getByText('权限已允许')).toBeInTheDocument()
    view.unmount()
    expect(stop).toHaveBeenCalled()
  })

  it('服务端 ready 之前不发送数据，首个客户端消息是二进制 JPEG', async () => {
    const instances: FakeSocket[] = []
    class FakeSocket {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
      readyState = FakeSocket.CONNECTING
      bufferedAmount = 0
      sent: unknown[] = []
      onopen: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null
      constructor(public url: string) { instances.push(this) }
      send(value: unknown) { this.sent.push(value) }
      close() { this.readyState = FakeSocket.CLOSED; this.onclose?.() }
      open() { this.readyState = FakeSocket.OPEN; this.onopen?.() }
      message(value: unknown) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(value) })) }
    }
    vi.stubGlobal('WebSocket', FakeSocket)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) => callback(new Blob([new Uint8Array(256)], { type: 'image/jpeg' })))

    const view = render(<BrowserCamera cameraId="browser-camera-one" targetFps={8} showPreview={false} />)
    expect(screen.queryByLabelText('浏览器摄像头实时预览', { selector: 'video' })).toBeInTheDocument()
    expect(view.container.querySelector('.browser-camera-stage')).toHaveAttribute('aria-hidden', 'true')
    fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ }))
    await waitFor(() => expect(instances).toHaveLength(1))
    instances[0].open()
    await new Promise((resolve) => setTimeout(resolve, 180))
    expect(instances[0].sent).toHaveLength(0)
    instances[0].message({ type: 'ready', camera_id: 'browser-camera-one' })
    await waitFor(() => expect(instances[0].sent.length).toBeGreaterThan(0), { timeout: 1000 })
    expect(instances[0].sent[0]).toBeInstanceOf(Blob)
    expect(view.container.querySelector('video')?.srcObject).toBe(stream)
    view.unmount()
  })

  it('设备轨道断开后立即关闭上传并释放所有轨道', async () => {
    class FakeSocket {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
      readyState = FakeSocket.OPEN
      bufferedAmount = 0
      onopen: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null
      send() {}
      close() { this.readyState = FakeSocket.CLOSED; this.onclose?.() }
    }
    vi.stubGlobal('WebSocket', FakeSocket)
    render(<BrowserCamera cameraId="browser-camera-disconnect" />)
    fireEvent.click(screen.getByRole('button', { name: /授权并选择摄像头/ }))
    await waitFor(() => expect(track.onended).toBeTypeOf('function'))
    track.onended?.()
    await waitFor(() => expect(screen.getByText(/上传通道与设备轨道已释放/)).toBeInTheDocument())
    expect(stop).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /授权并选择摄像头/ })).toBeInTheDocument()
  })
})
