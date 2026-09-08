import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ACCEPTANCE_SCENARIOS, normalizedRegion, RealMovementAcceptancePage, regionsOverlap, runMatchesScenarioContract } from '../src/pages/RealMovementAcceptancePage'
import type { AcceptanceRun, AcceptanceSuite } from '../src/types'

function json(value: unknown) {
  return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
}
const camera = { id: 'cam-real', name: '本机摄像头', room_name: '书房', source_type: 'webcam', source_type_provenance: 'opencv_camera', source: '0', enabled: true, inference_fps: 5, save_clips: true, runtime_mode: 'REAL', is_simulated: false, health: { status: 'running', source_frame_sequence: 42 } }
const phone = { id: 'phone-one', name: '我的手机', type: 'phone', aliases: [], aruco_id: 12, ring_enabled: false }
const zoneA = { id: 'zone-a', camera_id: 'cam-real', name: '桌面左侧', points: [[0.07, 0.14], [0.44, 0.14], [0.44, 0.86], [0.07, 0.86]], priority: 100, enabled: true }
const zoneB = { id: 'zone-b', camera_id: 'cam-real', name: '桌面右侧', points: [[0.56, 0.14], [0.93, 0.14], [0.93, 0.86], [0.56, 0.86]], priority: 90, enabled: true }
const preflight = { ready: true, runtime_mode: 'REAL', camera_id: 'cam-real', source_type: 'opencv_camera', is_simulated: false, health_status: 'running' }

// Explicit component-contract mocks; none are physical-camera acceptance evidence.
function run(index: number, override: Partial<AcceptanceRun> = {}): AcceptanceRun {
  const scenario = ACCEPTANCE_SCENARIOS[index - 1]
  const zone = (key?: string) => key === 'B' ? 'zone-b' : 'zone-a'
  return {
    id: `run-${index}`, validation_run_id: `run-${index}`, suite_id: 'suite-one', contract_version: 'p0-real-movement-v1',
    scenario_index: index, trial_kind: scenario.kind, minimum_duration_seconds: scenario.minimumDurationSeconds || 0,
    item_id: 'phone-one', camera_id: 'cam-real', runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false,
    origin_zone_id: zone(scenario.expectedFrom), destination_zone_id: zone(scenario.expectedTo || scenario.expectedFrom),
    status: 'PASSED', outcome: 'PASSED', score_eligible: true, event_id: scenario.crossZone ? `event-${index}` : null,
    result: { movement_event_count: scenario.crossZone ? 1 : 0 }, ...override,
  }
}
function suite(runs: AcceptanceRun[] = [], override: Partial<AcceptanceSuite> = {}): AcceptanceSuite {
  const movements = runs.filter((entry) => entry.trial_kind === 'movement')
  const negatives = runs.filter((entry) => entry.trial_kind !== 'movement')
  const passed = (entry: AcceptanceRun) => entry.status === 'PASSED' && entry.score_eligible !== false
  const complete = runs.length === 14 && runs.every((entry) => ['PASSED', 'FAILED', 'CANCELLED', 'CAMERA_INTERRUPTED'].includes(entry.status || ''))
  return {
    id: 'suite-one', suite_id: 'suite-one', contract_version: 'p0-real-movement-v1',
    contract: ACCEPTANCE_SCENARIOS.map((entry, index) => ({ scenario_index: index + 1, trial_kind: entry.kind, from_zone: entry.expectedFrom!, to_zone: entry.expectedTo || entry.expectedFrom!, minimum_duration_seconds: entry.minimumDurationSeconds || 0 })),
    runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, suite_status: 'IN_PROGRESS', overall_passed: false,
    item_id: 'phone-one', camera_id: 'cam-real', aruco_id: 12, zone_a_id: 'zone-a', zone_b_id: 'zone-b', zone_a: zoneA, zone_b: zoneB, thresholds: {},
    summary: { contract_complete: complete, created_runs: runs.length, terminal_runs: runs.filter((entry) => !['ACTIVE', 'CREATED'].includes(entry.status || '')).length, movement_total: movements.length, movement_passed: movements.filter(passed).length, movement_required: 8, negative_total: negatives.length, negative_passed: negatives.filter(passed).length, negative_required: 4, next_scenario_index: runs.length < 14 ? runs.length + 1 : null }, runs, ...override,
  }
}
function commonRequest(path: string, init?: RequestInit): Response | undefined {
  if (init?.method && init.method !== 'GET') return undefined
  if (path === '/api/cameras') return json([camera])
  if (path === '/api/items') return json([phone])
  if (path === '/api/cameras/cam-real/zones') return json([zoneA, zoneB])
  if (path === '/api/acceptance/preflight?camera_id=cam-real') return json(preflight)
  if (path.startsWith('/api/acceptance/marker-status?')) {
    const query = new URL(path, 'http://localhost').searchParams
    return json({ item_id: 'phone-one', camera_id: 'cam-real', validation_run_id: query.get('validation_run_id'), recognized: true, server_observed: true, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, frame: 99, stability: { state: 'VISIBLE_STATIC', diagnostics: { state: 'VISIBLE_STATIC', baseline: { source_frame: 90 } } } })
  }
}
function LocationProbe() { return <output data-testid="location">{useLocation().search}</output> }
function renderPage(query: string) { return render(<MemoryRouter initialEntries={[`/acceptance/real-movement${query}`]}><RealMovementAcceptancePage /><LocationProbe /></MemoryRouter>) }

describe('Mock 组件单测：真实移动验收套件合同', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('用预检的候选局域网链接生成真正二维码，但不把二维码存在当成手机可达', async () => {
    const fetchMock=vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path=String(input)
      if(path==='/api/acceptance/preflight?camera_id=cam-real') return json({...preflight,companion_url:'http://192.168.1.20:8018/companion/validation-marker'})
      if(path==='/api/acceptance/suites/suite-one') return json(suite([run(1,{status:'CREATED',outcome:'PENDING'})]))
      return commonRequest(path,init)!
    })
    vi.stubGlobal('fetch',fetchMock)
    renderPage('?suite=suite-one')
    const address=await screen.findByRole('link',{name:/http:\/\/192\.168\.1\.20:8018/})
    expect(address).toHaveAttribute('href','http://192.168.1.20:8018/companion/validation-marker?item=phone-one&camera=cam-real&run=run-1')
    expect(screen.getByRole('img', { name: '用手机打开 Marker 页的二维码' })).toBeInTheDocument()
    expect(screen.getByText(/是否可达仍取决于手机网络/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '复制链接' })).toBeEnabled()
    expect(fetchMock.mock.calls.every(([,init])=>!init?.method||init.method==='GET')).toBe(true)
  })

  it('固定四类负样本和十次交替跨区，拒绝其他套件、场景、物品与时长', () => {
    expect(ACCEPTANCE_SCENARIOS).toHaveLength(14)
    expect(ACCEPTANCE_SCENARIOS.filter((entry) => entry.kind !== 'movement').map((entry) => entry.kind)).toEqual(['stationary', 'minor_adjustment', 'occlusion', 'disconnect_reconnect'])
    expect(ACCEPTANCE_SCENARIOS.filter((entry) => entry.kind === 'movement').map((entry) => `${entry.expectedFrom}-${entry.expectedTo}`)).toEqual(['A-B', 'B-A', 'A-B', 'B-A', 'A-B', 'B-A', 'A-B', 'B-A', 'A-B', 'B-A'])
    expect(normalizedRegion('A', 'A', -1, 0.2, 1.4, 0.8)).toEqual({ key: 'A', name: 'A', x: 0, y: 0.2, width: 1, height: 0.6000000000000001 })
    expect(regionsOverlap({ key: 'A', name: 'A', x: 0, y: 0, width: 0.4, height: 1 }, { key: 'B', name: 'B', x: 0.6, y: 0, width: 0.4, height: 1 })).toBe(false)
    const matches = (entry: AcceptanceRun) => runMatchesScenarioContract(entry, ACCEPTANCE_SCENARIOS[2], 2, 'phone-one', 'cam-real', { A: 'zone-a', B: 'zone-b' }, 'suite-one', 'p0-real-movement-v1')
    expect(matches(run(3))).toBe(true)
    for (const change of [{ suite_id: 'other-suite' }, { trial_kind: 'stationary' }, { item_id: 'another-phone' }, { minimum_duration_seconds: 300 }]) expect(matches(run(3, change))).toBe(false)
  })

  it('忽略任意 runs/current 历史清单，刷新不会读取旧 run 或创建记录', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => commonRequest(String(input), init) || (() => { throw new Error(`unexpected request: ${input}`) })())
    vi.stubGlobal('fetch', fetchMock)
    renderPage('?runs=best-run-1,best-run-2&current=best-run-2&item=phone-one&camera=cam-real')
    await waitFor(() => expect(screen.getByRole('button', { name: /下一步：选择物品/ })).toBeEnabled())
    expect(screen.getByTestId('location')).not.toHaveTextContent('runs=')
    expect(screen.getByTestId('location')).not.toHaveTextContent('current=')
    expect(screen.queryByText('服务端权威汇总')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/acceptance/runs'))).toBe(false)
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })

  it('按唯一 suite 恢复并仅发送 GET，展示服务器 7/10 否决', async () => {
    const records = ACCEPTANCE_SCENARIOS.map((_, index) => run(index + 1, [12, 13, 14].includes(index + 1) ? { status: 'FAILED', outcome: 'FAILED', score_eligible: false, event_id: null } : {}))
    const serverSuite = suite(records, { suite_status: 'FAILED' })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/acceptance/suites/suite-one' ? json(serverSuite) : commonRequest(String(input), init) || (() => { throw new Error(`unexpected request: ${input}`) })())
    vi.stubGlobal('fetch', fetchMock)
    const { container } = renderPage('?suite=suite-one&runs=cherry-picked&current=cherry-picked&item=wrong-phone')
    expect(await screen.findByText('后端计入 7 / 10，门槛 8 / 10；总结果以后端总门禁为准')).toBeInTheDocument()
    expect(container.querySelector('.acceptance-score strong')).toHaveTextContent('7')
    expect(container.querySelector('.acceptance-score-meta')).toHaveTextContent('总门禁 不通过')
    expect(screen.getByTestId('location')).toHaveTextContent(/^\?suite=suite-one$/)
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/acceptance/runs/'))).toBe(false)
  })

  it('本地记录像全部成功但服务器 overall_passed=false 时不能宣布总通过', async () => {
    const serverSuite = suite(ACCEPTANCE_SCENARIOS.map((_, index) => run(index + 1)), { suite_status: 'FAILED', overall_passed: false })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/acceptance/suites/suite-one' ? json(serverSuite) : commonRequest(String(input), init)!))
    const { container } = renderPage('?suite=suite-one')
    await screen.findByText('后端计入 10 / 10，门槛 8 / 10；总结果以后端总门禁为准')
    expect(container.querySelector('.acceptance-score-meta')).toHaveTextContent('总门禁 不通过')
    expect(container.querySelector('.acceptance-scoreboard')).not.toHaveClass('passed')
  })

  it('显式开始先创建冻结套件，再只发送 suite_id 与轮次，不发送结果或造事件请求', async () => {
    let zoneCreateCount = 0
    let currentSuite = suite()
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/cameras/cam-real/zones' && init?.method === 'POST') return json(++zoneCreateCount === 1 ? zoneA : zoneB)
      if (path === '/api/acceptance/suites' && init?.method === 'POST') return json(currentSuite)
      if (path === '/api/acceptance/suites/suite-one') return json(currentSuite)
      if (path === '/api/acceptance/runs' && init?.method === 'POST') {
        const created = run(1, { status: 'CREATED', outcome: 'PENDING', score_eligible: false })
        currentSuite = suite([created]); return json(created)
      }
      if (path === '/api/acceptance/runs/run-1/start' && init?.method === 'POST') {
        const active = run(1, { status: 'ACTIVE', outcome: 'PENDING', score_eligible: false })
        currentSuite = suite([active]); return json(active)
      }
      return commonRequest(path, init) || (() => { throw new Error(`unexpected request: ${path}`) })()
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPage('?item=phone-one&camera=cam-real')
    await waitFor(() => expect(screen.getByRole('button', { name: /下一步：选择物品/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /下一步：选择物品/ }))
    fireEvent.click(screen.getByRole('button', { name: /下一步：划分 A \/ B/ }))
    fireEvent.click(screen.getByRole('button', { name: /保存区域并校准/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /开始第一轮真实验收/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /开始第一轮真实验收/ }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/acceptance/runs/run-1/start', expect.objectContaining({ method: 'POST' })))
    const bodyFor = (path: string) => JSON.parse(String(fetchMock.mock.calls.find(([input, init]) => String(input) === path && init?.method === 'POST')?.[1]?.body))
    expect(bodyFor('/api/acceptance/suites')).toEqual({ item_id: 'phone-one', camera_id: 'cam-real', zone_a_id: 'zone-a', zone_b_id: 'zone-b' })
    expect(bodyFor('/api/acceptance/runs')).toEqual({ suite_id: 'suite-one', scenario_index: 1 })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\?suite=suite-one$/))
    expect(fetchMock.mock.calls.some(([input]) => /complete|\/events?$/.test(String(input)))).toBe(false)
  })

  it('活动轮刷新恢复，在稳定基线与五秒媒体缓存就绪前阻止移动提示', async () => {
    const serverSuite = suite([run(1), run(2), run(3, { status: 'ACTIVE', outcome: 'PENDING', score_eligible: false, engine_status: { state: 'STABLE_AT_ORIGIN', baseline: { frame_sequence: 40 }, ring_seconds: 3.2, required_pre_seconds: 5 } })])
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/acceptance/suites/suite-one' ? json(serverSuite) : commonRequest(String(input), init)!))
    const { container } = renderPage('?suite=suite-one')
    expect(await screen.findByText('还不能移动')).toBeInTheDocument()
    expect(screen.getByText('事件前媒体缓存 3.2 / 5.0 秒')).toBeInTheDocument()
    expect(container.querySelector('.acceptance-instruction strong')).toHaveTextContent('先不要移动：把手机静止留在桌面左侧')
    expect(screen.queryByText('从 A 区拿起手机，移动到 B 区并放稳。')).not.toBeInTheDocument()
  })

  it('轮询收到失败终态后恢复固定下一轮，失败轮不提供重试', async () => {
    let suiteLoads = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/acceptance/suites/suite-one') return json(suite([run(1, ++suiteLoads === 1 ? { status: 'ACTIVE', outcome: 'PENDING', score_eligible: false } : { status: 'FAILED', outcome: 'FAILED', score_eligible: false })]))
      return commonRequest(String(input), init)!
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPage('?suite=suite-one')
    expect(await screen.findByText('本轮未通过')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /开始下一轮：轻调/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重试|明确开始本轮/ })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })

  it('合同包含其他套件记录时拒绝启动和总分通过', async () => {
    const invalid = suite([run(1, { suite_id: 'another-suite', status: 'CREATED', outcome: 'PENDING' })], { overall_passed: true })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/acceptance/suites/suite-one' ? json(invalid) : commonRequest(String(input), init)!))
    const { container } = renderPage('?suite=suite-one')
    expect(await screen.findByText('验收记录合同不一致')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /明确开始本轮|先等后端稳定识别/ })).toBeDisabled()
    expect(container.querySelector('.acceptance-score-meta')).toHaveTextContent('总门禁 合同异常')
  })

  it('按策略删除的历史媒体显示清理状态，不提供失效媒体链接', async () => {
    const records = ACCEPTANCE_SCENARIOS.map((_, index) => run(index + 1))
    records[13] = run(14, { evidence_integrity_status: 'policy_deleted', evidence_retained: false, retained_by_policy: false, evidence: { event_id: 'event-14', screenshot_path: 'real-media/deleted.jpg', clip_path: 'real-media/deleted.mp4' } })
    const serverSuite = suite(records, { overall_passed: true, suite_status: 'PASSED' })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/acceptance/suites/suite-one' ? json(serverSuite) : commonRequest(String(input), init)!))
    const { container } = renderPage('?suite=suite-one')
    expect(await screen.findByText(/此轮历史证据经后端验证通过，媒体已按保留策略清理/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /关键截图|事件短视频/ })).not.toBeInTheDocument()
    expect(container.querySelector('.acceptance-score-meta')).toHaveTextContent('总门禁 通过')
  })
})
