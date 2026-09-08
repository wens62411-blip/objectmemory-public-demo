import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VisionFrame } from '../src/components/VisionFrame'

// Controlled hook/HTTP contracts only. No physical camera, JPEG decode or FPS proof.
const controlled = vi.hoisted(() => ({ state: { connected: true, frame: null as unknown, error: '', fps: 24, acknowledge: vi.fn() } }))
vi.mock('../src/hooks/useLiveVision', () => ({ useLiveVision: () => controlled.state }))
const candidate = { bbox: [.2, .2, .2, .4], category: 'cell phone', accepted: true, best_item_id: 'fixture-phone', best_item_name: '测试手机', best_score: .9 }
const fixture = (sourceFrame = 30) => ({
  camera_id: 'fixture-camera', source_session_id: 'fixture-session', source_frame: sourceFrame,
  source_timestamp: '2026-09-06T10:00:00Z', width: 640, height: 480, image_data_url: `blob:unit-frame-${sourceFrame}`,
  runtime_mode: 'TEST', source_type: 'video_file', is_simulated: true, display_only: true,
  candidates: [candidate], hands: [],
})
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
let pendingFetch: ReturnType<typeof vi.fn>
let visibility: DocumentVisibilityState

function showDecodedStream() {
  const view = render(<VisionFrame cameraId="fixture-camera" />)
  controlled.state = { ...controlled.state, frame: fixture() }
  view.rerender(<VisionFrame cameraId="fixture-camera" />)
  const image = screen.getByAltText('同帧识别原图')
  expect(image).toHaveStyle({ visibility: 'hidden' })
  expect(screen.queryByLabelText('真实关键点叠加')).not.toBeInTheDocument()
  fireEvent.load(image)
  expect(image).toHaveStyle({ visibility: 'visible' })
  expect(screen.getByLabelText('真实关键点叠加').querySelectorAll('rect')).toHaveLength(1)
  return view
}

describe('Mock 连接关闭/挂起边界：旧跟踪图与框不得等待 HTTP 超时才撤下', () => {
  beforeEach(() => {
    vi.useFakeTimers(); visibility = 'visible'
    controlled.state = { connected: true, frame: null, error: '', fps: 24, acknowledge: vi.fn() }
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
    pendingFetch = vi.fn((_url: unknown, options?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      const abort = () => reject(new DOMException('Aborted', 'AbortError'))
      if (options?.signal?.aborted) abort()
      else options?.signal?.addEventListener('abort', abort, { once: true })
    }))
    vi.stubGlobal('fetch', pendingFetch)
  })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('已解码流断开后立即清除图和框，降级 HTTP 挂起也不能保留旧框', async () => {
    const view = showDecodedStream()
    expect(controlled.state.acknowledge).toHaveBeenCalledExactlyOnceWith(30, 'fixture-session')
    controlled.state = { ...controlled.state, connected: false, frame: null, error: '连接已关闭', fps: 0 }
    view.rerender(<VisionFrame cameraId="fixture-camera" />)
    expect(screen.queryByAltText('同帧识别原图')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('真实关键点叠加')).not.toBeInTheDocument()
    await advance(1000)
    expect(pendingFetch).toHaveBeenCalledTimes(1)
    expect(screen.queryByAltText('同帧识别原图')).not.toBeInTheDocument()
    expect(screen.queryByText('已匹配 测试手机')).not.toBeInTheDocument()
    await advance(1501)
    expect(screen.getByRole('alert')).toHaveTextContent('旧框已撤下')
  })

  it('断连后的新模型诊断帧必须重新解码，不继承已撤下流的可见状态或 ACK', async () => {
    const view = showDecodedStream()
    const fallback = { ...fixture(31), display_only: false, image_data_url: 'data:image/jpeg;base64,/9j/2Q==' }
    pendingFetch.mockResolvedValueOnce(new Response(JSON.stringify(fallback), { status: 200 }))
    controlled.state = { ...controlled.state, connected: false, frame: null, error: '连接已关闭', fps: 0 }
    view.rerender(<VisionFrame cameraId="fixture-camera" />)
    await advance(1)
    const image = screen.getByAltText('同帧识别原图')
    expect(image).toHaveAttribute('data-source-frame', '31')
    expect(image).toHaveStyle({ visibility: 'hidden' })
    expect(screen.queryByLabelText('真实关键点叠加')).not.toBeInTheDocument()
    fireEvent.load(image)
    expect(image).toHaveStyle({ visibility: 'visible' })
    expect(screen.getByLabelText('真实关键点叠加').querySelectorAll('rect')).toHaveLength(1)
    expect(controlled.state.acknowledge).toHaveBeenCalledTimes(1)
  })

  it('连接仍在但帧已过期时清空本地图和框，不继续显示上次解码的图', () => {
    const view = showDecodedStream()
    controlled.state = { ...controlled.state, frame: null, error: '新鲜帧已超时' }
    view.rerender(<VisionFrame cameraId="fixture-camera" />)
    expect(screen.queryByAltText('同帧识别原图')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('真实关键点叠加')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('新鲜帧已超时')
    expect(pendingFetch).not.toHaveBeenCalled()
  })

  it('页面挂起立即隐藏本地帧；断连后再恢复也不恢复旧帧', async () => {
    const view = showDecodedStream()
    visibility = 'hidden'; fireEvent(document, new Event('visibilitychange'))
    expect(screen.queryByAltText('同帧识别原图')).not.toBeInTheDocument()
    controlled.state = { ...controlled.state, connected: false, frame: null, error: '', fps: 0 }
    view.rerender(<VisionFrame cameraId="fixture-camera" />)
    await advance(500)
    expect(pendingFetch).not.toHaveBeenCalled()
    visibility = 'visible'; fireEvent(document, new Event('visibilitychange'))
    await advance(500)
    expect(pendingFetch).toHaveBeenCalledTimes(1)
    expect(screen.queryByAltText('同帧识别原图')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('真实关键点叠加')).not.toBeInTheDocument()
  })
})
