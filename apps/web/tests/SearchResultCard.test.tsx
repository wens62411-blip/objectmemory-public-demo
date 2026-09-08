import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SearchResultCard } from '../src/components/SearchResultCard'
import type { EventRecord, SearchResult } from '../src/types'
import { formatTime } from '../src/lib/format'
import { SearchPage } from '../src/pages/SearchPage'
import { ToastProvider } from '../src/components/Toast'

const result: SearchResult = {
  item: { id: 'phone', name: '我的手机', type: '手机', aliases: ['手机'], aruco_id: 1, ring_enabled: true },
  status: 'placed',
  answer: '手机最后一次确认放在客厅 · 沙发右侧。',
  last_confirmed: { id: 'e1', item_id: 'phone', item_name: '我的手机', event_type: 'placed', camera_id: 'cam', camera_name: '客厅演示回放', room_name: '客厅', zone_name: '沙发右侧', timestamp_start: '2026-09-03T08:32:00Z', confidence: .95, evidence_type: 'test_replay', detection_mode: 'aruco', screenshot_path: '/media/event-images/e1.jpg', clip_path: '/media/event-clips/e1.mp4', runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true, evidence_status: 'confirmed', final_status: 'confirmed_placed' },
  last_seen: null, last_picked_up: null, last_occluded: null, last_exited: null,
}

describe('Mock 组件单测：寻找结果证据卡', () => {
  afterEach(cleanup)
  it('搜索引导以最新可靠观察为先，不再要求历史确认放置优先', () => {
    render(<MemoryRouter><ToastProvider><SearchPage /></ToastProvider></MemoryRouter>)
    expect(screen.getByText('最新可靠观察优先')).toBeInTheDocument()
    expect(screen.getByText('历史确认放置独立展示')).toBeInTheDocument()
    expect(screen.getByText(/不替代更新的观察或未知状态/)).toBeInTheDocument()
    expect(screen.queryByText('最后确认放置优先')).not.toBeInTheDocument()
  })
  it('没有位置证据时独立显示后台观察提示，不把提示升级成识别成功或位置', () => {
    const hintOnly = { ...result, last_confirmed: null, observation_hint: { code: 'identity_rejected', message: '当前画面中的候选物品尚未通过身份确认，请检查参考照片。' } }
    render(<MemoryRouter><SearchResultCard result={hintOnly} runtimeMode="REAL" /></MemoryRouter>)
    expect(screen.getByRole('region', { name: '观察提示' })).toHaveTextContent('观察提示（不是位置记录）')
    expect(screen.getByRole('region', { name: '观察提示' })).toHaveTextContent(hintOnly.observation_hint.message)
    expect(screen.getByText('暂时没有真实摄像头产生的位置记录。')).toBeInTheDocument()
    expect(screen.queryByText('客厅 · 沙发右侧')).not.toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByText('真实确认')).not.toBeInTheDocument()
  })
  it.each([undefined, null, { code: 'empty', message: '' }, { code: 'blank', message: '   \n ' }])('空观察提示%j不生成提示区域或位置记录', (observation_hint) => {
    render(<MemoryRouter><SearchResultCard result={{ ...result, last_confirmed: null, observation_hint }} runtimeMode="REAL" /></MemoryRouter>)
    expect(screen.queryByRole('region', { name: '观察提示' })).not.toBeInTheDocument()
    expect(screen.getByText('暂时没有真实摄像头产生的位置记录。')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })
  it('展示位置、可信度、回放标识和可执行操作', () => {
    const ring = vi.fn()
    render(<MemoryRouter><SearchResultCard result={result} runtimeMode="DEMO" onRing={ring} /></MemoryRouter>)
    expect(screen.getByText('客厅 · 沙发右侧')).toBeInTheDocument()
    expect(screen.getByText(/可信度 95%/)).toBeInTheDocument()
    expect(screen.getAllByText('测试视频').length).toBeGreaterThan(0)
    screen.getByRole('button', { name: /让手机响铃/ }).click()
    expect(ring).toHaveBeenCalledOnce()
  })
  it('被遮挡时明确说明不确定', () => {
    const occluded = { ...result, last_confirmed: null, evidence: { ...result.last_confirmed!, id: null, event_type: 'occluded' as const, evidence_status: 'observation_only', final_status: null, clip_path: null } }
    render(<MemoryRouter><SearchResultCard result={occluded} runtimeMode="DEMO" /></MemoryRouter>)
    expect(screen.getByText(/具体位置尚未完全确认/)).toBeInTheDocument()
  })
  it('运行模式未确认时不回退信任事件自身的 REAL 或 DEMO 标记', () => {
    const view = render(<MemoryRouter><SearchResultCard result={result} runtimeMode="UNKNOWN" /></MemoryRouter>)
    const card = within(view.container)
    expect(card.getByText('运行模式未确认，无法展示位置记录。')).toBeInTheDocument()
    expect(card.getByText('运行模式未确认，位置证据已隐藏')).toBeInTheDocument()
    expect(card.queryByText('客厅 · 沙发右侧')).not.toBeInTheDocument()
    expect(card.queryByRole('link', { name: /播放事件录像/ })).not.toBeInTheDocument()
  })
  it('REAL 中未确认或无效证据不会展示位置和媒体', () => {
    const candidate = { ...result, answer: '手机最后一次确认放在客厅 · 沙发右侧。', last_confirmed: { ...result.last_confirmed!, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, evidence_status: 'candidate' } }
    const view = render(<MemoryRouter><SearchResultCard result={candidate} runtimeMode="REAL" /></MemoryRouter>)
    const card = within(view.container)
    expect(card.getByText('暂时没有真实摄像头产生的位置记录。')).toBeInTheDocument()
    expect(card.getByText('位置尚未由真实摄像头确认')).toBeInTheDocument()
    expect(card.queryByText('客厅 · 沙发右侧')).not.toBeInTheDocument()
    expect(card.queryByRole('link', { name: /播放事件录像/ })).not.toBeInTheDocument()
  })
  it('REAL 浏览器 observation 只显示最后看到，伪造 confirmed 则隐藏位置', () => {
    const observation = { ...result, answer: '不应直接信任的后端答案', last_confirmed: null, last_seen: { ...result.last_confirmed!, runtime_mode: 'REAL', source_type: 'browser_camera', is_simulated: false, event_type: 'last_seen' as const, evidence_status: 'observation_only', screenshot_path: null, clip_path: null } }
    const view = render(<MemoryRouter><SearchResultCard result={observation} runtimeMode="REAL" /></MemoryRouter>)
    let card = within(view.container)
    expect(card.getByText('客厅 · 沙发右侧')).toBeInTheDocument()
    expect(card.getByText(/最后在客厅 · 沙发右侧被看到，尚未有新的确认放置证据/)).toBeInTheDocument()
    expect(card.getByText('浏览器帧（来源未验证）')).toBeInTheDocument()
    expect(card.getAllByText('最后看到').length).toBeGreaterThan(0)
    expect(card.queryByText('真实确认')).not.toBeInTheDocument()

    const forged = { ...observation, last_seen: { ...observation.last_seen!, event_type: 'placed' as const, evidence_status: 'confirmed' } }
    view.rerender(<MemoryRouter><SearchResultCard result={forged} runtimeMode="REAL" /></MemoryRouter>)
    card = within(view.container)
    expect(card.getByText('暂时没有真实摄像头产生的位置记录。')).toBeInTheDocument()
    expect(card.queryByText('客厅 · 沙发右侧')).not.toBeInTheDocument()
    expect(card.queryByText('真实确认')).not.toBeInTheDocument()
  })

  it('新的 REAL 观察优先于旧的确认证据，回答、位置和来源保持一致', () => {
    const confirmed = { ...result.last_confirmed!, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, room_name: '客厅', zone_name: '桌面', timestamp_start: '2026-09-03T08:30:00Z', timestamp_end: '2026-09-03T08:30:03Z' }
    const observation = { ...confirmed, id: 'observation', event_type: 'last_seen' as const, source_type: 'browser_camera', evidence_status: 'observation_only', final_status: null, room_name: '卧室', zone_name: '床边', timestamp_start: '2026-09-03T08:40:00Z', timestamp_end: '2026-09-03T08:40:00Z', screenshot_path: null, clip_path: null }
    const combined = { ...result, answer: '旧回答不应被展示', evidence: confirmed, last_confirmed: confirmed, last_seen: observation }
    const view = render(<MemoryRouter><SearchResultCard result={combined} runtimeMode="REAL" /></MemoryRouter>)
    const card = within(view.container)
    expect(card.getByText('卧室 · 床边')).toBeInTheDocument()
    expect(card.getByText(/最后在卧室 · 床边被看到/)).toBeInTheDocument()
    expect(card.getByText('浏览器帧（来源未验证）')).toBeInTheDocument()
    expect(card.getByLabelText('最后看到的区域')).toHaveTextContent('卧室 · 床边')
    expect(card.getByLabelText('最后看到的区域')).not.toHaveTextContent('客厅 · 桌面')
    expect(within(card.getByRole('region', { name: '历史确认放置' })).getByText('客厅 · 桌面')).toBeInTheDocument()
    expect(card.queryByRole('link', { name: /播放事件录像/ })).not.toBeInTheDocument()
  })

  it('DEMO 的更新观察保持最后看到为主，同时明确显示上一次模拟确认', () => {
    const observation = { ...result.last_confirmed!, id: 'observation', event_type: 'last_seen' as const, evidence_status: 'observation_only', final_status: null, timestamp_start: '2026-09-03T08:40:00Z', timestamp_end: '2026-09-03T08:40:00Z', screenshot_path: null, clip_path: null }
    const combined = { ...result, evidence: observation, last_seen: observation }
    render(<MemoryRouter><SearchResultCard result={combined} runtimeMode="DEMO" /></MemoryRouter>)
    expect(screen.getAllByText('最后看到').length).toBeGreaterThan(0)
    expect(screen.getAllByText('模拟确认').length).toBeGreaterThan(0)
  })

  it('REAL 中缺失或拒绝的 final_status 不展示确切位置与媒体', () => {
    for (const finalStatus of [null, 'rejected']) {
      const invalid = { ...result, last_confirmed: { ...result.last_confirmed!, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, final_status: finalStatus } }
      const view = render(<MemoryRouter><SearchResultCard result={invalid} runtimeMode="REAL" /></MemoryRouter>)
      const card = within(view.container)
      expect(card.getByText('暂时没有真实摄像头产生的位置记录。')).toBeInTheDocument()
      expect(card.queryByText('客厅 · 沙发右侧')).not.toBeInTheDocument()
      expect(card.queryByRole('link', { name: /播放事件录像/ })).not.toBeInTheDocument()
      view.unmount()
    }
  })

  const photoObservation = (overrides: Partial<EventRecord> = {}): EventRecord => ({
    ...result.last_confirmed!, id: null, event_id: null, event_type: 'last_seen', evidence_status: 'observation_only', final_status: null,
    runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, detection_mode: 'experimental',
    camera_name: '桌面相机', room_name: '书房', zone_name: '桌子右侧', timestamp_start: '2026-09-05T12:00:20Z', timestamp_end: '2026-09-05T12:00:20Z',
    source_session_id: 'observed-session', source_frame: 42, confidence: .92, screenshot_path: '/media/last-observed/one.jpg', clip_path: null,
    screenshot_source_session_id: 'observed-session', screenshot_source_frame: 10, screenshot_observed_at: '2026-09-05T12:00:00Z',
    identity_evidence: { accepted: true, best_score: .92, score_kind: 'cosine_similarity_not_probability', model_id: 'dinov2-small' }, ...overrides,
  })

  it('开机首次可靠观察无需动作、历史event id或短片，外观分数不能显示为正确率', () => {
    const observation = photoObservation()
    render(<MemoryRouter><SearchResultCard result={{ ...result, evidence: observation, last_confirmed: null, last_observed: observation }} runtimeMode="REAL" onCorrect={vi.fn()} /></MemoryRouter>)
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('最后在书房 · 桌子右侧被看到')
    expect(screen.getByText('外观分数 0.920（非正确率）')).toBeInTheDocument()
    expect(screen.queryByText(/可信度 92%/)).not.toBeInTheDocument()
    expect(screen.getByText(/没有拿放录像不影响这条可靠观察/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '纠正位置' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '播放事件录像' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '有依据的候选位置' })).not.toBeInTheDocument()
  })

  it.each(['lost', 'offline', 'occluded', 'exited_view'] as const)('等时间的新%s状态覆盖旧confirmed主答案，历史仍单列', (eventType) => {
    const observation = photoObservation({ event_type: eventType, room_name: '门厅', zone_name: '门口', timestamp_start: result.last_confirmed!.timestamp_start, timestamp_end: result.last_confirmed!.timestamp_start })
    const confirmed = { ...result.last_confirmed!, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false }
    render(<MemoryRouter><SearchResultCard result={{ ...result, evidence: confirmed, last_confirmed: confirmed, last_confirmed_placement: confirmed, last_observed: observation, status: eventType }} runtimeMode="REAL" onCorrect={vi.fn()} /></MemoryRouter>)
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('最后在门厅 · 门口被看到')
    expect(screen.getByRole('heading', { level: 2 })).not.toHaveTextContent('确认放在')
    expect(screen.getByText(/这是最后可见区域，不表示物品现在仍在那里/)).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '历史确认放置' })).getByText('客厅 · 沙发右侧')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '纠正位置' })).not.toBeInTheDocument()
  })

  it('较早截图时间/帧与最新last_seen分别标明，不会把旧截图标成最新帧', () => {
    const observation = photoObservation()
    render(<MemoryRouter><SearchResultCard result={{ ...result, evidence: observation, last_confirmed: null, last_observed: observation }} runtimeMode="REAL" /></MemoryRouter>)
    const explanation = screen.getByLabelText('截图与观察时间说明')
    expect(explanation).toHaveTextContent(`最后观察帧：42 · 最后看到时间：${formatTime(observation.timestamp_start)}`)
    expect(explanation).toHaveTextContent(`截图拍摄时间：${formatTime(observation.screenshot_observed_at)}`)
    expect(explanation).toHaveTextContent('截图来源帧：10')
    expect(explanation).toHaveTextContent('较早的同来源截图，不是最后一次观察的同一帧')
  })

  it('错误会话截图或读取失败不清空可靠观察，也不显示错配图片', () => {
    const observation = photoObservation({ screenshot_source_session_id: 'another-session' })
    const view = render(<MemoryRouter><SearchResultCard result={{ ...result, evidence: observation, last_confirmed: null }} runtimeMode="REAL" /></MemoryRouter>)
    expect(screen.getByText('截图来源会话不一致，已隐藏')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('最后在书房')
    view.rerender(<MemoryRouter><SearchResultCard result={{ ...result, evidence: photoObservation(), last_confirmed: null }} runtimeMode="REAL" /></MemoryRouter>)
    fireEvent.error(screen.getByAltText('我的手机最后可见截图'))
    expect(screen.getByText('截图无法读取')).toBeInTheDocument()
    expect(screen.getByText(/图像证据当前不完整，可靠观察与最后看到时间仍保留/)).toBeInTheDocument()
  })

  it('候选必须有原因、标签及同观察会话，单独显示推测且不影响主答案', () => {
    const observation = photoObservation({ event_type: 'occluded' })
    const basis = { evidence_type: 'inferred' as const, source_session_id: 'observed-session', source_frame: 42, observed_at: observation.timestamp_start }
    const hypotheses = [
      { ...basis, label: '桌边附近', reason: '最后观察位于桌边，随后遮挡' },
      { ...basis, label: '无依据的抽屉', reason: '' },
      { ...basis, label: '其他会话的沙发', reason: '旧轨迹', source_session_id: 'another-session' },
    ]
    const view = render(<MemoryRouter><SearchResultCard result={{ ...result, evidence: observation, last_confirmed: null, location_hypotheses: hypotheses }} runtimeMode="REAL" /></MemoryRouter>)
    const candidates = screen.getByRole('region', { name: '有依据的候选位置' })
    expect(candidates).toHaveTextContent('可能位置 · 推测，尚未确认')
    expect(candidates).toHaveTextContent('依据：最后观察位于桌边，随后遮挡')
    expect(candidates).not.toHaveTextContent('无依据的抽屉')
    expect(candidates).not.toHaveTextContent('其他会话的沙发')
    expect(screen.getByRole('heading', { level: 2 })).not.toHaveTextContent('桌边附近')
    view.rerender(<MemoryRouter><SearchResultCard result={{ ...result, evidence: observation, last_confirmed: null, location_hypotheses: [] }} runtimeMode="REAL" /></MemoryRouter>)
    expect(screen.queryByRole('region', { name: '有依据的候选位置' })).not.toBeInTheDocument()
  })

  it('事件纠正仅用于有效历史ID，未确认或无ID观察不能发起事件纠正', () => {
    const callback = vi.fn()
    const view = render(<MemoryRouter><SearchResultCard result={result} runtimeMode="DEMO" onCorrect={callback} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: '纠正位置' }))
    expect(callback).toHaveBeenCalledWith(result.last_confirmed)
    view.rerender(<MemoryRouter><SearchResultCard result={{ ...result, evidence: photoObservation({ identity_evidence: { accepted: false } }), last_confirmed: null }} runtimeMode="REAL" onCorrect={callback} /></MemoryRouter>)
    expect(screen.queryByRole('button', { name: '纠正位置' })).not.toBeInTheDocument()
    expect(screen.queryByText('书房 · 桌子右侧')).not.toBeInTheDocument()
  })
})
