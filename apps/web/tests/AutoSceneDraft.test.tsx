import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AutoSceneDraft, hasFreshSceneSource, acceptsSceneResponse } from '../src/components/AutoSceneDraft'
import type { Camera, SceneResponse } from '../src/types'

const camera: Camera = { id: 'auto-cam', name: '书房相机', room_name: '书房', source_type: 'opencv_camera', source: '0', enabled: true, inference_fps: 5, save_clips: true, runtime_mode: 'REAL', is_simulated: false, health: { status: 'running', preview_source_session_id: 'session', preview_frame_sequence: 50, preview_age_ms: 10 } }
const empty = { scene: null, items: [], runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false }
const saved: SceneResponse = { ...empty, scene: { camera_id: camera.id, proposals: [], surfaces: [], runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false,
  source_session_id: 'scene-session', source_frame: 50, source_timestamp: '2026-09-07T10:00:00Z', snapshot_id: 'scene-shot', scene_version: 1,
  screenshot_path: '/media/scene-images/one.jpg', screenshot_sha256: 'a'.repeat(64), calibration_status: 'needs_confirmation', geometry: { coordinate_space: 'source_normalized', source_width: 640, source_height: 480 } } }
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
const props = { cameraId: camera.id, mode: 'REAL' as const, hasScene: false, blocked: false }
const toggle = () => screen.getByRole('checkbox', { name: '首次在线画面自动生成一次近似场景草稿' })
async function tick(ms = 1) { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

describe('自动场景草稿Mock契约，不证明真实家具识别', () => {
  beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); localStorage.setItem('objectmemory:auto-scene:REAL:auto-cam', '0') })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear() })
  const setup = (options: { camera?: Camera; preflight?: unknown; failure?: boolean; proposed?: unknown } = {}) => {
    const onCreated = vi.fn()
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/cameras') return json([options.camera || camera])
      if (String(input).endsWith('/scene/propose?only_if_empty=true')) return options.failure ? json({ detail: '场景已经存在，已拒绝覆盖' }, 409) : json(options.proposed || saved)
      if (String(input).endsWith('/scene') && !init?.method) return json(options.preflight || empty)
      throw new Error(`Unexpected mock request ${input}`)
    })
    vi.stubGlobal('fetch', fetch)
    const view = render(<AutoSceneDraft {...props} onCreated={onCreated} />)
    return { ...view, fetch, onCreated }
  }
  it('默认首次自动等待有效画面，但从不自行启动相机；用户关闭选择可持久化', async () => {
    localStorage.removeItem('objectmemory:auto-scene:REAL:auto-cam')
    const { fetch } = setup({ camera: { ...camera, health: { status: 'offline' } } })
    expect(toggle()).toBeChecked(); await tick()
    expect(fetch.mock.calls.map(([input]) => input)).toEqual(['/api/cameras'])
    fireEvent.click(toggle()); await tick(3000)
    expect(fetch).toHaveBeenCalledOnce()
    expect(localStorage.getItem('objectmemory:auto-scene:REAL:auto-cam')).toBe('0')
  })
  it('开启后只提交一次空场景条件POST，没有PUT确认，刷新不会重复', async () => {
    const first = setup(); fireEvent.click(toggle()); await tick()
    expect(first.fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(first.fetch.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false)
    expect(first.onCreated).toHaveBeenCalledExactlyOnceWith(saved)
    await tick(5000)
    expect(first.fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    first.unmount()
    const second = setup(); await tick(5000)
    expect(second.fetch).not.toHaveBeenCalled()
    expect(screen.getByText(/刷新不会重复运行/)).toBeInTheDocument()
  })
  it('已保存/正在人工编辑的场景不自动扫描或覆盖', async () => {
    const value = setup(); fireEvent.click(toggle())
    value.rerender(<AutoSceneDraft {...props} hasScene onCreated={value.onCreated} />)
    await tick(5000); expect(value.fetch).not.toHaveBeenCalled()
    value.rerender(<AutoSceneDraft {...props} blocked onCreated={value.onCreated} />)
    await tick(5000); expect(value.fetch).not.toHaveBeenCalled()
  })
  it('提交前发现已有场景则采用已有记录，不再POST', async () => {
    const value = setup({ preflight: saved }); fireEvent.click(toggle()); await tick()
    expect(value.onCreated).toHaveBeenCalledWith(saved)
    expect(value.fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })
  it('已有场景若来源不匹配也不得被采用，更不能据此自动重建', async () => {
    const value = setup({ preflight: { ...saved, scene: { ...saved.scene, runtime_mode: 'DEMO', is_simulated: true } } }); fireEvent.click(toggle()); await tick()
    expect(value.onCreated).not.toHaveBeenCalled()
    expect(value.fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    expect(screen.getByText(/场景来源信息未通过校验/)).toBeInTheDocument()
  })
  it('模式切换时晚到的旧GET不能触发写入或覆盖回调', async () => {
    let finish!: (value: Response) => void
    const onCreated = vi.fn()
    const fetch = vi.fn(() => new Promise<Response>(resolve => { finish = resolve }))
    vi.stubGlobal('fetch', fetch)
    const view = render(<AutoSceneDraft {...props} onCreated={onCreated} />)
    fireEvent.click(toggle()); await tick()
    view.rerender(<AutoSceneDraft {...props} mode="UNKNOWN" onCreated={onCreated} />)
    await act(async () => finish(json([camera])))
    expect(onCreated).not.toHaveBeenCalled(); expect(fetch).toHaveBeenCalledOnce()
  })
  it('服务器409防竞态时不会重试、确认或谎称成功', async () => {
    const value = setup({ failure: true }); fireEvent.click(toggle()); await tick(5000)
    expect(value.fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(value.onCreated).not.toHaveBeenCalled()
    expect(screen.getByText(/不会自动重试或覆盖已有场景/)).toBeInTheDocument()
  })
  it('自动创建响应不能冒充人工已校准模型', async () => {
    const value = setup({ proposed: { ...saved, scene: { ...saved.scene, calibration_status: 'calibrated' } } }); fireEvent.click(toggle()); await tick()
    expect(value.onCreated).not.toHaveBeenCalled()
    expect(screen.getByText(/自动生成的场景不是未确认草稿/)).toBeInTheDocument()
  })
  it('没有家具候选明确显示没有编造家具', async () => {
    setup(); fireEvent.click(toggle()); await tick()
    expect(screen.getByText(/没有编造家具或房间/)).toBeInTheDocument()
  })
  it('没有有效源一分钟后停止检查，不无限积压', async () => {
    const value = setup({ camera: { ...camera, health: { status: 'offline' } } }); fireEvent.click(toggle())
    await tick(63_000)
    expect(value.fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    expect(toggle()).not.toBeChecked()
    const count = value.fetch.mock.calls.length; await tick(10_000); expect(value.fetch).toHaveBeenCalledTimes(count)
  })
  it('开启后页面卸载不会继续获取画面', async () => {
    const value = setup({ camera: { ...camera, health: { status: 'offline' } } }); fireEvent.click(toggle()); await tick()
    value.unmount(); await tick(10_000); expect(value.fetch).toHaveBeenCalledOnce()
  })
  it.each([
    undefined,
    { ...camera, runtime_mode: 'DEMO', is_simulated: true },
    { ...camera, source_type: 'video_file' as const },
    { ...camera, health: { ...camera.health!, preview_age_ms: 1100 } },
    { ...camera, health: { ...camera.health!, preview_source_session_id: null } },
    { ...camera, health: { ...camera.health!, health_snapshot_stale: true } },
    { ...camera, health: { ...camera.health!, status: 'offline' } },
  ])('无效/过期/模拟源不会通过真实场景门 %#', value => expect(hasFreshSceneSource(value, 'REAL')).toBe(false))
  it('只有连续同模式新鲜帧元数据可以触发', () => expect(hasFreshSceneSource(camera, 'REAL')).toBe(true))
  it.each([
    { camera_id: 'other' }, { source_session_id: '' }, { source_frame: -1 }, { source_timestamp: 'invalid' },
    { source_type: 'video_file' }, { screenshot_sha256: 'unverified' }, { geometry: { coordinate_space: 'pixels', source_width: 640, source_height: 480 } },
    { screenshot_path: 'https://example.com/private-scene.jpg' }, { screenshot_path: '/media/scene-images/../outside.jpg' },
  ])('拒绝历史记录中损坏/错配的来源信息%j', patch => expect(acceptsSceneResponse({ ...saved, scene: { ...saved.scene!, ...patch } }, camera.id, 'REAL')).toBe(false))
})
