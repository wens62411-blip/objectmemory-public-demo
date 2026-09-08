import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BrowserCamera } from '../src/components/BrowserCamera'

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

  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

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
