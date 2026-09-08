import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VisionFrame } from '../src/components/VisionFrame'
import type { Camera, Zone } from '../src/types'

// Controlled transport/DOM lifecycle contracts, not JPEG decode or hardware FPS proof.
const camera: Camera = { id: 'cam', name: '合同相机', room_name: '测试房间', source_type: 'webcam', source: '0', enabled: true, inference_fps: 5, save_clips: true, runtime_mode: 'REAL', is_simulated: false, source_type_provenance: 'opencv_camera', health: { status: 'ready' } }
const zone = { id: 'desk', camera_id: 'cam', name: '桌面', enabled: true, points: [[.1, .2], [.6, .2], [.6, .8]], priority: 1 } as Zone
const frame = (sequence = 1) => ({ type: 'frame', camera_id: 'cam', source_session_id: 'unit-session', source_frame: sequence,
  source_timestamp: '2026-09-06T00:00:00Z', width: 1280, height: 720, coordinate_space: 'source_normalized', display_only: true,
  runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, hands: [],
  candidates: [{ category: 'cell phone', bbox: [.2, .2, .2, .3], accepted: false }] })
const packet = (sequence = 1) => {
  const metadata = new TextEncoder().encode(JSON.stringify(frame(sequence)))
  const bytes = new Uint8Array(4 + metadata.length + 5)
  new DataView(bytes.buffer).setUint32(0, metadata.length)
  bytes.set(metadata, 4); bytes.set([255, 216, 255, 217, 0], 4 + metadata.length)
  return bytes.buffer
}
class Socket {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
  static instances: Socket[] = []
  readyState = Socket.CONNECTING; binaryType = ''; sent: string[] = []
  onopen: (() => void) | null = null; onclose: (() => void) | null = null
  onerror: (() => void) | null = null; onmessage: ((event: { data: unknown }) => void) | null = null
  constructor(public url: string) { Socket.instances.push(this) }
  open() { this.readyState = Socket.OPEN; this.onopen?.() }
  message(data: unknown) { this.onmessage?.({ data }) }
  send(data: string) { this.sent.push(data) }
  close() { if (this.readyState === Socket.CLOSED) return; this.readyState = Socket.CLOSED; this.onclose?.() }
}
const settle = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
async function showMain(props: Partial<React.ComponentProps<typeof VisionFrame>> = {}) {
  const view = render(<VisionFrame cameraId="cam" camera={camera} primary mode="aruco" zones={[zone]} {...props} />)
  await settle()
  await act(async () => { Socket.instances[0].open(); Socket.instances[0].message(packet()) })
  const image = screen.getByAltText('同帧识别原图')
  expect(image).toHaveStyle({ visibility: 'hidden' })
  fireEvent.load(image)
  return view
}

describe('单主画面与按需模型原帧（Mock 传输合同）', () => {
  beforeEach(() => {
    vi.useFakeTimers(); Socket.instances = []; vi.stubGlobal('WebSocket', Socket)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    let url = 0
    class FixtureURL extends URL {
      static createObjectURL = vi.fn(() => `blob:unit-${++url}`)
      static revokeObjectURL = vi.fn()
    }
    vi.stubGlobal('URL', FixtureURL)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...frame(10), display_only: false, image_data_url: 'data:image/jpeg;base64,/9j/2Q==', hand_status: { status: 'no_hands' } }), { headers: { 'Content-Type': 'application/json' } })))
  })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('唯一图像使用实际流来源/模式/区域，并在解码后才画框和发送 ACK', async () => {
    const view = await showMain()
    expect(view.container.querySelectorAll('img')).toHaveLength(1)
    expect(view.container.querySelector('img[src*="/stream"]')).not.toBeInTheDocument()
    expect(screen.getByText('本机 OpenCV 来源')).toBeInTheDocument()
    expect(screen.getByText('稳定标签模式')).toBeInTheDocument()
    expect(screen.getByLabelText('当前摄像头区域')).toHaveAttribute('viewBox', '0 0 1280 720')
    expect(view.container.querySelector('polygon')).toHaveAttribute('points', '128,144 768,144 768,576')
    expect(view.container.querySelector('.vision-frame-image')?.getAttribute('style')).not.toContain('max-width')
    expect(Socket.instances).toHaveLength(1)
    expect(Socket.instances[0].sent.map(value => JSON.parse(value))).toEqual([{ ack: 1, source_session_id: 'unit-session' }])
    expect(fetch).not.toHaveBeenCalled()
  })

  it('全屏仅作用于主画面容器；后台状态刷新不创建第二条流', async () => {
    const view = await showMain()
    const stage = view.container.querySelector('.vision-frame-stage')!
    const fullscreen = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(stage, 'requestFullscreen', { value: fullscreen })
    fireEvent.click(screen.getByRole('button', { name: '全屏' })); await settle()
    expect(fullscreen).toHaveBeenCalledOnce()
    view.rerender(<VisionFrame cameraId="cam" camera={{ ...camera, health: { status: 'ready', preview_fps: 18 } }} primary zones={[zone]} showZones={false} />)
    await settle()
    expect(Socket.instances).toHaveLength(1)
    expect(screen.queryByLabelText('当前摄像头区域')).not.toBeInTheDocument()
    expect(view.container.querySelectorAll('img')).toHaveLength(1)
  })

  it('模型原帧仅展开才读取，关闭即卸载，不建第二条视频 WS', async () => {
    const view = await showMain({ showHands: false })
    const details = screen.getByText('查看模型原帧与手部关键点').closest('details')!
    expect(fetch).not.toHaveBeenCalled()
    details.open = true; fireEvent(details, new Event('toggle')); await settle()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(view.container.querySelectorAll('img')).toHaveLength(2)
    expect(within(details).getByAltText('同帧识别原图')).toHaveAttribute('src', expect.stringContaining('data:image'))
    expect(Socket.instances).toHaveLength(1)
    details.open = false; fireEvent(details, new Event('toggle')); await settle()
    const calls = vi.mocked(fetch).mock.calls.length
    await settle(1000)
    expect(fetch).toHaveBeenCalledTimes(calls)
    expect(view.container.querySelectorAll('img')).toHaveLength(1)
  })

  it('断流清除图框，不在主画面自动伪装低频诊断；重连需解码新帧', async () => {
    const view = await showMain()
    await act(async () => Socket.instances[0].close())
    expect(view.container.querySelector('img')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('真实关键点叠加')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('连接中断')
    expect(fetch).not.toHaveBeenCalled()
    await settle(500)
    await act(async () => { Socket.instances[1].open(); Socket.instances[1].message(packet(2)) })
    const image = screen.getByAltText('同帧识别原图')
    expect(image).toHaveStyle({ visibility: 'hidden' })
    fireEvent.load(image)
    expect(image).toHaveStyle({ visibility: 'visible' })
    view.rerender(<VisionFrame cameraId="cam" camera={camera} primary active={false} />); await settle(1000)
    expect(view.container.querySelector('img')).not.toBeInTheDocument()
    expect(Socket.instances).toHaveLength(2)
    expect(Socket.instances.every(socket => socket.readyState === Socket.CLOSED)).toBe(true)
  })

  it('全屏失败只提示，不丢失正在播放的图像', async () => {
    const view = await showMain()
    Object.defineProperty(view.container.querySelector('.vision-frame-stage'), 'requestFullscreen', { value: vi.fn().mockRejectedValue(new Error('denied')) })
    fireEvent.click(screen.getByRole('button', { name: '全屏' })); await settle()
    expect(screen.getByRole('alert')).toHaveTextContent('未能进入全屏')
    expect(screen.getByAltText('同帧识别原图')).toBeInTheDocument()
  })
})
