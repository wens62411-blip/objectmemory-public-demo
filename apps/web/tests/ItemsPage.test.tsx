import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ItemsPage } from '../src/pages/ItemsPage'
import { ToastProvider } from '../src/components/Toast'
import { RuntimeProvider } from '../src/contexts/RuntimeContext'
import type { EventRecord, SearchResult } from '../src/types'

const phone = { id: 'phone-zero', name: '我的手机', type: '手机', aliases: [], aruco_id: 0, ring_enabled: false }
const keys = { id: 'keys-one', name: '钥匙', type: '钥匙', aliases: [], aruco_id: 1, ring_enabled: false }
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })

function setup() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/items' && !init?.method) return json([phone, keys])
    if (path.endsWith('/last-location')) return json({ item: phone, evidence: null })
    if (path === '/api/items/phone-zero' && init?.method === 'PATCH') return json({ ...phone, ...JSON.parse(String(init.body)) })
    throw new Error(`unexpected request: ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<MemoryRouter><ToastProvider><ItemsPage /></ToastProvider></MemoryRouter>)
  return fetchMock
}

describe('Mock 组件单测：物品标签绑定', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('Marker 0 正常显示，编辑名称保存仍提交数字 0', async () => {
    const fetchMock = setup()
    expect(await screen.findByText('标签 000')).toBeInTheDocument()
    fireEvent.click(within(screen.getByText('我的手机').closest('article')!).getByTitle('编辑'))
    fireEvent.click(screen.getByText('高级诊断 · 绑定标签'))
    expect(screen.getByRole('combobox', { name: 'ArUco 标签编号' })).toHaveValue('000')
    fireEvent.change(screen.getByRole('textbox', { name: '物品名称' }), { target: { value: '我的主力手机' } })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/items/phone-zero', expect.objectContaining({ method: 'PATCH' })))
    const body = JSON.parse(String(fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')?.[1]?.body))
    expect(body.name).toBe('我的主力手机')
    expect(body.aruco_id).toBe(0)
  })

  it('完整显示 0 至 49，当前自己的编号可保留，其他物品已占用编号不可选', async () => {
    setup()
    await screen.findByText('标签 000')
    fireEvent.click(within(screen.getByText('我的手机').closest('article')!).getByTitle('编辑'))
    fireEvent.click(screen.getByText('高级诊断 · 绑定标签'))
    const choices = within(screen.getByRole('combobox', { name: 'ArUco 标签编号' }))
    expect(choices.getAllByRole('option')).toHaveLength(51)
    expect(choices.getByRole('option', { name: '000' })).toBeEnabled()
    expect(choices.getByRole('option', { name: '001（已被其他物品使用）' })).toBeDisabled()
    expect(choices.getByRole('option', { name: '049' })).toBeEnabled()
  })

  it('即使绕过禁用选项提交已占用编号，页面也不发送保存请求', async () => {
    const fetchMock = setup()
    await screen.findByText('标签 000')
    fireEvent.click(within(screen.getByText('我的手机').closest('article')!).getByTitle('编辑'))
    fireEvent.click(screen.getByText('高级诊断 · 绑定标签'))
    fireEvent.change(screen.getByRole('combobox', { name: 'ArUco 标签编号' }), { target: { value: '001' } })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    expect(await screen.findByText('这个标签编号已被其他物品占用，请选择可用编号。')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
  })
})

describe('Mock 组件合同：物品卡最后可靠观察', () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
  const observed: EventRecord = {
    id: null, event_id: null, item_id: phone.id, item_name: phone.name, camera_id: 'cam', camera_name: '相机', room_name: '书房', zone_name: '桌面右侧',
    event_type: 'last_seen', timestamp_start: '2026-09-05T12:00:00Z', timestamp_end: '2026-09-05T12:00:00Z', confidence: .92,
    evidence_status: 'observation_only', detection_mode: 'experimental', runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false,
    identity_evidence: { accepted: true, best_score: .92, score_kind: 'cosine_similarity_not_probability' }, clip_path: null,
  }
  function setupLocation(overrides: Partial<SearchResult>) {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/runtime-config') return json({ runtime_mode: 'REAL' })
      if (path === '/api/items') return json([phone])
      if (path.endsWith('/last-location')) return json({ item: phone, last_confirmed: null, last_seen: null, last_picked_up: null, last_occluded: null, last_exited: null, status: 'last_seen', answer: '不可直接使用的旧文案', ...overrides })
      throw new Error(`unexpected request ${path}`)
    }))
    render(<MemoryRouter><ToastProvider><RuntimeProvider><ItemsPage /></RuntimeProvider></ToastProvider></MemoryRouter>)
  }

  it('没有完整移动历史也显示真实last_observed及非正确率外观分数', async () => {
    setupLocation({ last_observed: observed })
    expect(await screen.findByText('书房 · 桌面右侧')).toBeInTheDocument()
    const card = within(screen.getByText('我的手机').closest('article')!)
    expect(card.getAllByText('最后看到').length).toBeGreaterThan(0)
    expect(card.getByText(/外观分数 0.920（非正确率）/)).toBeInTheDocument()
    expect(card.queryByText(/可信度 92%/)).not.toBeInTheDocument()
    expect(card.getByRole('link', { name: '查看观察截图与历史确认' })).toHaveAttribute('href', `/search?q=${encodeURIComponent(phone.name)}`)
  })

  it.each(['lost', 'offline', 'occluded', 'exited_view'] as const)('等时间%s不让旧放置冒充当前所在位置', async (event_type) => {
    const confirmed = { ...observed, id: 'event-old', event_id: 'event-old', event_type: 'placed' as const, evidence_status: 'confirmed', final_status: 'confirmed_placed', zone_name: '旧沙发' }
    setupLocation({ last_observed: { ...observed, event_type }, last_confirmed: confirmed, evidence: confirmed })
    expect(await screen.findByText('书房 · 桌面右侧')).toBeInTheDocument()
    const card = within(screen.getByText('我的手机').closest('article')!)
    expect(card.getByText(/上方仅为最后可见区域/)).toBeInTheDocument()
    expect(card.queryByText('书房 · 旧沙发')).not.toBeInTheDocument()
  })

  it('REAL物品卡不显示视频回放或未确认身份的观察', async () => {
    setupLocation({ last_observed: { ...observed, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true }, last_seen: { ...observed, identity_evidence: { accepted: false } } })
    await screen.findByText('暂时没有真实摄像头产生的位置记录。')
    expect(screen.queryByText('书房 · 桌面右侧')).not.toBeInTheDocument()
  })

  it('停留页面时1.5秒读到新位置，读取失败保留旧证据并明确刷新失败', async () => {
    vi.useFakeTimers()
    const state: Partial<SearchResult> = { last_observed: observed }
    await act(async () => { setupLocation(state); await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('书房 · 桌面右侧')).toBeInTheDocument()
    state.last_observed = { ...observed, zone_name: '书柜前', timestamp_end: '2026-09-05T12:01:00Z' }
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.getByText('书房 · 书柜前')).toBeInTheDocument()
    vi.mocked(fetch).mockRejectedValue(new Error('offline'))
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.getByText('书房 · 书柜前')).toBeInTheDocument()
    expect(screen.getByText(/位置暂时无法刷新，保留上次可靠记录/)).toBeInTheDocument()
  })

  it('空位置显示服务端具体观察提示，不把候选说明当位置证据', async () => {
    setupLocation({ last_observed: null, observation_hint: { code: 'not_observed', message: '当前帧没有手机候选，请检查物品是否清晰进入视野。' } })
    expect(await screen.findByText('当前观察提示（不是位置记录）：')).toBeInTheDocument()
    expect(screen.getByText(/当前帧没有手机候选/)).toBeInTheDocument()
    expect(screen.getByText('暂时没有真实摄像头产生的位置记录。')).toBeInTheDocument()
  })
})
