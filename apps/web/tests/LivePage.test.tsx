import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LivePage } from '../src/pages/LivePage'

const polling = vi.hoisted(() => ({ loading: false, initial: false }))
const live = vi.hoisted(() => ({ state: { connected: true, frame: null as unknown, error: '', fps: 24, acknowledge: vi.fn() } }))
vi.mock('../src/hooks/useLiveVision', () => ({ useLiveVision: () => live.state }))
vi.mock('../src/hooks/useApi', () => ({
  usePolling: (path: string) => ({
    data: path === '/api/cameras' && !polling.initial ? [{ id: 'camera-one', name: '组件测试摄像头', source_type: 'webcam', source_type_provenance: 'opencv_camera', runtime_mode: 'REAL', is_simulated: false, health: { status: 'ready', preview_fps: 29.7, fps: 5 } }] : [],
    loading: path === '/api/cameras' && polling.loading, error: null, reload: vi.fn(),
  }),
  useApi: (_path: string, defaults: unknown) => ({ data: defaults, loading: false, error: null, setData: vi.fn() }),
}))
vi.mock('../src/contexts/RuntimeContext', () => ({ useRuntime: () => ({ mode: 'REAL' }) }))
vi.mock('../src/components/Toast', () => ({ useToast: () => ({ notify: vi.fn() }) }))

describe('实时页轮询生命周期（Mock 状态合同，不证明真实帧率）', () => {
  beforeEach(() => {
    polling.loading = false; polling.initial = false
    live.state = { connected: true, frame: null, error: '', fps: 24, acknowledge: vi.fn() }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).endsWith('/vision-snapshot') ? {
      source_session_id: 'session', source_frame: 20, source_timestamp: '2026-09-06T00:00:00Z', runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false,
      image_data_url: 'data:image/jpeg;base64,AA', width: 640, height: 480, hands: [], candidates: [{ category: 'cell phone', bbox: [.1, .2, .2, .3], accepted: false, best_item_id: null }],
    } : { camera_id: 'camera-one', source_session_id: 'session', source_frame: 20, timestamp: '2026-09-06T00:00:00Z', fresh: true, hand_status: { enabled: true, available: true, status: 'ready' }, profile_count: 0, hands: [{ hand_id: 'h1', posture: 'closed_fist', posture_label: '握拳', motion: 'still', motion_label: '静止', stable: true }], interactions: [] }), { headers: { 'Content-Type': 'application/json' } })))
  })
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
  it('主画面旁常驻手动作卡，不要求先展开诊断或注册照片；Live开关只改show_hands', async () => {
    render(<MemoryRouter><LivePage /></MemoryRouter>)
    expect(await screen.findByText('握拳')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '当前手部动作' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '同帧物品和手部关键点' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '手部关键点' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/settings', expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ show_hands: false }) })))
  })
  it('后台轮询不会卸载已解码的唯一主画面，不挂载 MJPEG', async () => {
    const view = render(<MemoryRouter><LivePage /></MemoryRouter>)
    await act(async () => {
      live.state = { ...live.state, frame: { source_session_id: 'session', source_frame: 20, source_timestamp: '2026-09-06T00:00:00Z', runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, display_only: true, image_data_url: 'blob:unit-frame', width: 640, height: 480, hands: [], candidates: [] } }
      view.rerender(<MemoryRouter><LivePage /></MemoryRouter>)
    })
    const frame = screen.getByAltText('同帧识别原图')
    fireEvent.load(frame)
    const source = frame.getAttribute('src')
    polling.loading = true
    view.rerender(<MemoryRouter><LivePage /></MemoryRouter>)
    expect(screen.getByAltText('同帧识别原图')).toBe(frame)
    expect(frame.getAttribute('src')).toBe(source)
    polling.loading = false
    view.rerender(<MemoryRouter><LivePage /></MemoryRouter>)
    expect(screen.getByAltText('同帧识别原图')).toBe(frame)
    expect(view.container.querySelectorAll('img')).toHaveLength(1)
    expect(view.container.querySelector('img[src*="/stream"]')).not.toBeInTheDocument()
  })
  it('首次没有摄像头快照时才显示加载提示', () => {
    polling.loading = true; polling.initial = true
    render(<MemoryRouter><LivePage /></MemoryRouter>)
    expect(screen.getByText('正在读取摄像头…')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })
  it('默认只显示连续主画面，不自动请求模型诊断或原始视频', async () => {
    const errors = vi.spyOn(console, 'error')
    const view = render(<MemoryRouter><LivePage /></MemoryRouter>)
    await act(async () => {
      live.state = { ...live.state, frame: { source_session_id: 'session', source_frame: 21, source_timestamp: '2026-09-06T00:00:01Z', runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, display_only: true, image_data_url: 'blob:unit-frame-21', width: 640, height: 480, hands: [], candidates: [{ category: 'cell phone', bbox: [.1, .2, .2, .3], accepted: false, best_item_id: null }] } }
      view.rerender(<MemoryRouter><LivePage /></MemoryRouter>)
    })
    const recognition = await screen.findByAltText('同帧识别原图')
    fireEvent.load(recognition)
    expect(screen.getByRole('region', { name: '同帧物品和手部关键点' })).toBeInTheDocument()
    expect(screen.getByLabelText('候选手机（身份未确认）')).toBeInTheDocument()
    expect(view.container.querySelectorAll('img')).toHaveLength(1)
    expect(view.container.querySelector('img[src*="/stream"]')).not.toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/vision-snapshot'))).toBe(false)
    expect(screen.queryByRole('button', { name: '识别帧叠加' })).not.toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
    expect(errors.mock.calls.some(values => values.some(value => String(value).includes('same key')))).toBe(false)
  })
  it('窄屏锚点直接指向唯一主画面，技术状态放在画面之后', async () => {
    render(<MemoryRouter><LivePage /></MemoryRouter>)
    const recognition = screen.getByRole('region', { name: '同帧物品和手部关键点' }).closest('#live-recognition')!
    expect(recognition.parentElement).toHaveClass('live-view-single')
    expect(document.querySelector('.live-preview-pane')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看物品识别' })).toHaveAttribute('href', '#live-recognition')
    expect(recognition).toHaveAttribute('tabindex', '-1')
    expect(recognition.compareDocumentPosition(document.querySelector('.live-inspector')!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
    await screen.findByText('握拳')
  })
})
