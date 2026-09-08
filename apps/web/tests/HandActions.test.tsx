import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HandActionsCard, HandActionSummary } from '../src/components/HandActionsCard'
import { VisionFrame } from '../src/components/VisionFrame'
import { SettingsPage } from '../src/pages/SettingsPage'
import { ToastProvider } from '../src/components/Toast'
import type { HandActionSnapshot, HandInteraction } from '../src/types'

const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
const snapshot: HandActionSnapshot = {
  camera_id: 'cam', source_session_id: 'session', source_frame: 20, timestamp: '2026-09-06T02:00:00Z', fresh: true,
  hand_status: { enabled: true, available: true, status: 'ready' }, profile_count: 0,
  hands: [{ hand_id: 'hand-1', posture: 'open_palm', posture_label: '张手', motion: 'right', motion_label: '向右移动', stable: true, evidence_type: 'landmark_geometry_only', grasp_established: false }], interactions: [],
}
const interaction = (holding_status: string): HandInteraction => ({ item_id: 'phone', item_name: '登记手机', state: holding_status, holding_status, release_observed: holding_status === 'released', reason: 'server movement evidence' })
const settle = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

describe('Mock 动作缓存UI合同：不证明物理手势或触觉抓握', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('没有物品照片仍持续展示几何手势，仅读缓存GET不创建推理/物品事件', async () => {
    const fetchMock = vi.fn(async () => json(snapshot)); vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    expect(screen.getByText('张手')).toBeInTheDocument()
    expect(screen.getByText('向右移动')).toBeInTheDocument()
    expect(screen.getByText(/尚未注册可识别的物品照片，仍可查看手部反馈/)).toBeInTheDocument()
    expect(screen.getByText(/不等于物品拿起或放下/)).toBeInTheDocument()
    await settle(1000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenLastCalledWith('/api/cameras/cam/actions', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it.each(['disabled', 'failed', 'unavailable', 'loading'])('%s必须隐藏响应残留的手势和交互', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...snapshot, interactions: [interaction('co_moving')], hand_status: { ...snapshot.hand_status, status, enabled: status !== 'disabled' } })))
    render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    expect(screen.queryByRole('list', { name: '当前几何手动作' })).not.toBeInTheDocument()
    expect(screen.queryByRole('list', { name: '手与物品的交互线索' })).not.toBeInTheDocument()
  })

  it.each(['stale_frame', 'camera_not_running', 'camera_changed'])('服务端%s即使带旧动作也立刻清空', async (reason) => {
    let response = snapshot
    vi.stubGlobal('fetch', vi.fn(async () => json(response)))
    render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    expect(screen.getByText('张手')).toBeInTheDocument()
    response = { ...snapshot, fresh: false, reason }; await settle(1000)
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
  })

  it('网络失败撤下旧动作，恢复后只展示新帧', async () => {
    let offline = false
    vi.stubGlobal('fetch', vi.fn(async () => { if (offline) throw new Error('controlled disconnect'); return json(snapshot) }))
    render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    offline = true; await settle(1000)
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
    expect(screen.getByText('动作暂时不可用，旧动作已隐藏')).toBeInTheDocument()
  })

  it('重复返回同一帧不能延长旧动作寿命；新帧恢复显示', async () => {
    let response = snapshot
    vi.stubGlobal('fetch', vi.fn(async () => json(response)))
    render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    await settle(2501)
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
    expect(screen.getByText('动作帧已过期，等待新鲜画面')).toBeInTheDocument()
    await settle(500)
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
    response = { ...snapshot, source_frame: 21 }; await settle(1000)
    expect(screen.getByText('张手')).toBeInTheDocument()
  })

  it('关闭分析和卸载均中止轮询，不保留动作或继续发送请求', async () => {
    const fetchMock = vi.fn(async () => json(snapshot)); vi.stubGlobal('fetch', fetchMock)
    const view = render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    view.rerender(<MemoryRouter><HandActionsCard cameraId="cam" enabled={false} /></MemoryRouter>); await settle(3000)
    expect(screen.queryByText('张手')).not.toBeInTheDocument(); expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.getByText('手部分析已关闭')).toBeInTheDocument()
    view.unmount(); await settle(5000); expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('设置未知或摄像头已停止时不读动作缓存', async () => {
    const fetchMock = vi.fn(async () => json(snapshot)); vi.stubGlobal('fetch', fetchMock)
    const view = render(<MemoryRouter><HandActionsCard cameraId="cam" settingsKnown={false} /></MemoryRouter>); await settle(2000)
    expect(fetchMock).not.toHaveBeenCalled()
    view.rerender(<MemoryRouter><HandActionsCard cameraId="cam" cameraRunning={false} /></MemoryRouter>); await settle(2000)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.getByText('摄像头未运行，暂无当前动作')).toBeInTheDocument()
  })

  it('等待请求不重复并发；即使未收到下一响应旧动作也按时过期', async () => {
    let resolvePending: (value: Response) => void = () => undefined
    const pending = new Promise<Response>((resolve) => { resolvePending = resolve })
    const fetchMock = vi.fn().mockResolvedValueOnce(json(snapshot)).mockReturnValue(pending); vi.stubGlobal('fetch', fetchMock)
    const view = render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle(0); await settle(2501)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
    view.unmount(); resolvePending(json({ ...snapshot, source_frame: 21 })); await settle(3000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('跨相机响应或同会话倒退帧不能显示为当前动作', async () => {
    let response = snapshot
    vi.stubGlobal('fetch', vi.fn(async () => json(response)))
    render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    response = { ...snapshot, source_frame: 19 }; await settle(1000)
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
    response = { ...snapshot, camera_id: 'other', source_frame: 21 }; await settle(1000)
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
  })

  it('当前没有手时仍可显示有依据的持有未知状态，不把手丢失写成放下', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...snapshot, hands: [], interactions: [interaction('holding_uncertain')], hand_status: { ...snapshot.hand_status, status: 'no_hands' } })))
    render(<MemoryRouter><HandActionsCard cameraId="cam" /></MemoryRouter>); await settle()
    expect(screen.getByText('持有状态不确定 · 不能判为放下')).toBeInTheDocument()
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
  })

  it('候选手势保持候选，不执行后端未知手势标签中的成功文案', () => {
    render(<HandActionSummary hands={[{ ...snapshot.hands[0], posture: 'unknown-code', posture_label: '拿起成功', stable: false }]} interactions={[interaction('nearby'), interaction('co_moving'), interaction('release_candidate'), interaction('released')].map((entry, i) => ({ ...entry, item_id: `item-${i}` }))} />)
    expect(screen.getByText('待稳定：姿态未知')).toBeInTheDocument()
    expect(screen.getByText('手在物品附近 · 不代表拿起')).toBeInTheDocument()
    expect(screen.getByText('连续共同移动 · 可能携带')).toBeInTheDocument()
    expect(screen.getByText('已观测可见分离 · 仍未确认放下')).toBeInTheDocument()
    expect(screen.queryByText('拿起成功')).not.toBeInTheDocument()
  })

  it('手身份显示左右或简短序号，完整轨迹ID只留在诊断title；四指展开不冒充张手', () => {
    const fullId = 'hand-1234567890abcdef1234567890abcdef'
    render(<HandActionSummary hands={[{ ...snapshot.hands[0], hand_id: fullId, handedness: 'Left', posture: 'fingers_extended' }, { ...snapshot.hands[0], hand_id: 'b', handedness: 'Right' }, { ...snapshot.hands[0], hand_id: 'c' }]} interactions={[]} />)
    expect(screen.getByText('左手')).toHaveAttribute('title', `手部轨迹编号：${fullId}`)
    expect(screen.getByText('右手')).toBeInTheDocument()
    expect(screen.getByText('手 3')).toBeInTheDocument()
    expect(screen.queryByText(fullId, { exact: false })).not.toBeInTheDocument()
    expect(screen.getByText('四指展开')).toBeInTheDocument()
    expect(screen.getByText('四根手指伸展，拇指姿态尚未确认。')).toBeInTheDocument()
  })

  it('VisionFrame隐藏关键点不隐藏手动作，叠加仍来自同一帧并会过期', async () => {
    const frame = { runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, image_data_url: 'data:image/jpeg;base64,AA', width: 640, height: 480, source_frame: 20, source_timestamp: snapshot.timestamp,
      source_session_id: snapshot.source_session_id, candidates: [], hands: [{ landmarks: [[.1, .2], [.2, .3]] }], hand_status: snapshot.hand_status, hand_actions: snapshot.hands, interactions: [interaction('nearby')] }
    vi.stubGlobal('fetch', vi.fn(async () => json(frame)))
    const view = render(<MemoryRouter><VisionFrame cameraId="cam" showHands={false} /></MemoryRouter>); await settle()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    expect(screen.getByText('张手')).toBeInTheDocument()
    expect(view.container.querySelector('circle')).not.toBeInTheDocument()
    view.rerender(<MemoryRouter><VisionFrame cameraId="cam" showHands /></MemoryRouter>)
    expect(view.container.querySelectorAll('circle')).toHaveLength(2)
    expect(screen.getByText('手在物品附近 · 不代表拿起')).toBeInTheDocument()
    await settle(2501)
    expect(screen.queryByRole('img', { name: '同帧识别原图' })).not.toBeInTheDocument()
    expect(screen.queryByText('张手')).not.toBeInTheDocument()
  })
})

describe('Mock设置合同：分析与关键点显示分离', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
  it('旧设置缺少分析开关默认启用，关闭显示仍提交分析true', async () => {
    const settings = { detection_mode: 'aruco', inference_fps: 5, static_seconds: 2, pre_seconds: 5, post_seconds: 5, retention_days: 7, save_clips: true, show_hands: true, privacy_mode: true, record_events: true,
      runtime_mode: 'REAL', source_type: 'runtime_settings', is_simulated: false, hand_action_min_stable_frames: 7 }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => json(String(input) === '/api/settings' ? init?.method === 'PATCH' ? JSON.parse(String(init.body)) : settings : { local: false }))
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><ToastProvider><SettingsPage /></ToastProvider></MemoryRouter>)
    expect(await screen.findByRole('checkbox', { name: '启用本地手部分析' })).toBeChecked()
    fireEvent.click(screen.getByRole('checkbox', { name: '显示手部关键点' }))
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(true))
    const body = JSON.parse(String(fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')?.[1]?.body))
    expect(body).toMatchObject({ show_hands: false, hand_detection_enabled: true })
    for (const key of ['runtime_mode', 'source_type', 'is_simulated', 'hand_action_min_stable_frames']) expect(body).not.toHaveProperty(key)
    expect(screen.getByText(/关闭后仍可继续分析手部动作/)).toBeInTheDocument()
  })
})
