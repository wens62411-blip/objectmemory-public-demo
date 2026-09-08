import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ScenePage, validScenePolygon } from '../src/pages/ScenePage'
import { RuntimeProvider } from '../src/contexts/RuntimeContext'
import { App } from '../src/App'
import { evidenceBadge } from '../src/lib/provenance'
import { eventLabel } from '../src/lib/format'
import type { EventRecord, SearchResult } from '../src/types'

// This suite verifies page contracts, not video transport or physical-camera success.
// VisionFrame has separate frame/identity/expiry tests and a real-backend E2E gate.
vi.mock('../src/components/VisionFrame', () => ({ VisionFrame: ({ registeredItems = [], selectedItemId, onSelectItem }: { registeredItems?: { id: string; name: string }[]; selectedItemId?: string; onSelectItem?: (id: string) => void }) => <div aria-label="视频联动测试替身">{registeredItems.map(item => <button key={item.id} aria-pressed={selectedItemId === item.id} onClick={() => onSelectItem?.(item.id)}>视频选择{item.name}</button>)}</div> }))

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const camera = { id: 'cam', name: '书房相机', room_name: '书房', source_type: 'opencv_camera', source: '0', runtime_mode: 'REAL', is_simulated: false }
const polygon: [number, number][] = [[.1, .1], [.8, .1], [.8, .7], [.1, .7]]
const scene = {
  camera_id: camera.id, scene_version: 3, snapshot_id: 'snapshot-a', source_session_id: 'session-a', source_frame: 100, source_timestamp: '2026-09-05T12:00:00Z',
  runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, screenshot_path: '/media/scene-images/scene-a.jpg', screenshot_sha256: 'a'.repeat(64),
  geometry: { source_width: 640, source_height: 480, coordinate_space: 'source_normalized' }, calibration_status: 'schematic', proposals: [],
  surfaces: [{ surface_id: 'surface-a', name: '工作桌面', surface_type: 'table', image_polygon: polygon, map_polygon: polygon, confirmed_by_user: true }],
}
const observed: EventRecord = {
  id: null, item_id: 'keys', item_name: '家门钥匙', camera_id: 'cam', camera_name: camera.name, room_name: '书房', event_type: 'last_seen', zone_name: '桌面一侧',
  timestamp_start: '2026-09-05T12:00:03Z', timestamp_end: '2026-09-05T12:00:03Z', confidence: .92, evidence_status: 'observation_only', detection_mode: 'experimental',
  runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, source_session_id: 'session-a', source_frame: 130, final_position: [.32, .45],
  screenshot_path: '/media/observation-images/keys.jpg', screenshot_source_frame: 125, screenshot_observed_at: '2026-09-05T12:00:02Z', screenshot_source_session_id: 'session-a',
  identity_evidence: { accepted: true, best_score: .92, score_kind: 'cosine_similarity_not_probability' },
}
const result: SearchResult = { item: { id: 'keys', name: '家门钥匙', type: 'keys', aliases: [], aruco_id: null, ring_enabled: false }, last_observed: observed, last_confirmed: null,
  last_seen: null, last_exited: null, last_occluded: null, last_picked_up: null, status: 'last_seen', answer: '不可使用的预设答案', location_hypotheses: [] }
const response = (changes: Record<string, unknown> = {}) => ({ scene, items: [], runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, ...changes })

function setup(value = response(), handlers: { propose?: () => Response; save?: (body: Record<string, unknown>) => Response; get?: () => Response; app?: boolean; cameras?: typeof camera[] } = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runtime-config') return json({ runtime_mode: 'REAL' })
    if (path === '/api/session') return json({ authenticated: true })
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/cameras') return json(handlers.cameras || [camera])
    if (path.endsWith('/scene/propose')) return handlers.propose?.() || json(value)
    if (path.endsWith('/scene') && init?.method === 'PUT') return handlers.save?.(JSON.parse(String(init.body))) || json(value)
    if (path.endsWith('/scene')) return handlers.get?.() || json(value)
    throw new Error(`Unexpected Mock contract request: ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const rendered = render(<MemoryRouter initialEntries={['/scene']}>{handlers.app ? <App /> : <RuntimeProvider><ScenePage /></RuntimeProvider>}</MemoryRouter>)
  return { fetchMock, ...rendered }
}
async function loadImage() {
  const image = await screen.findByRole('img', { name: '用于确认区域的服务端场景截图' })
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: 640 }, naturalHeight: { configurable: true, value: 480 } })
  fireEvent.load(image)
  return image
}
function editExisting() { fireEvent.click(screen.getByRole('button', { name: '工作桌面 已核对' })) }
function savedBodies(fetchMock: ReturnType<typeof setup>['fetchMock']) { return fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT').map(([, init]) => JSON.parse(String(init?.body))) }

describe('Mock 组件合同：场景来源、人工区域与观察点，不证明物理摄像头', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('没有摄像头时不创建地图或示例点', async () => {
    const { fetchMock } = setup(response(), { cameras: [] })
    await screen.findByText('先连接一个摄像头')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes('/scene'))).toBe(false)
  })

  it('空场景只读取，刷新或重新挂载均不会自动提案、保存或创建位置', async () => {
    const { fetchMock, unmount } = setup(response({ scene: null }))
    await screen.findByText('还没有这个房间的场景')
    fireEvent.click(screen.getByRole('button', { name: '刷新记录' }))
    await screen.findByText('已读取服务端最新场景和观察。')
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method)).toBe(true)
    expect(screen.queryByRole('button', { name: /的最后观察/ })).not.toBeInTheDocument()
    unmount()
    const next = setup(response({ scene: null }))
    await screen.findByText('还没有这个房间的场景')
    expect(next.fetchMock.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })

  it('主导航可进入真实ScenePage路由，保留集中四步指南而无验收通过声明', async () => {
    setup(response({ scene: null }), { app: true })
    await screen.findByText('还没有这个房间的场景')
    expect(screen.getByRole('link', { name: '家庭场景' })).toHaveAttribute('href', '/scene')
    expect(screen.getByRole('heading', { name: '第一次使用，按这四步' })).toBeInTheDocument()
    expect(screen.getByText(/不是物理移动验收已通过/)).toBeInTheDocument()
  })

  it('建议模型失败明确提示，获取当前画面仅由按钮发起且不自动确认区域', async () => {
    const { fetchMock } = setup(response({ scene: null }), { propose: () => json(response({ scene: { ...scene, calibration_status: 'needs_confirmation', surfaces: [], proposal_error: '家具建议模型不可用；可以手动画区域。' } })) })
    await screen.findByText('还没有这个房间的场景')
    fireEvent.click(screen.getByRole('button', { name: '识别当前场景' }))
    await loadImage()
    expect(screen.getByText(/家具建议模型不可用/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '手动画区域' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '保存已确认区域' })).toBeDisabled()
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(savedBodies(fetchMock)).toHaveLength(0)
  })

  it('改名撤销人工确认；保存原样绑定版本/帧/会话并始终map=image', async () => {
    const { fetchMock } = setup(response())
    await loadImage(); editExisting()
    fireEvent.change(screen.getByRole('textbox', { name: '场景区域名称' }), { target: { value: '工作台面' } })
    expect(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' })).not.toBeChecked()
    expect(screen.getByRole('button', { name: '保存已确认区域' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' }))
    fireEvent.click(screen.getByRole('button', { name: '保存已确认区域' }))
    await screen.findByText(/区域已由服务端保存/)
    const [body] = savedBodies(fetchMock)
    expect(body).toEqual({ scene_version: 3, snapshot_id: 'snapshot-a', source_session_id: 'session-a', source_frame: 100, surfaces: [{ surface_id: 'surface-a', name: '工作台面', surface_type: 'table', image_polygon: polygon, map_polygon: polygon, confirmed_by_user: true }] })
  })

  it('新画面即使保留旧轮廓也必须重新确认，不沿用过期proposal_id', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { fetchMock } = setup(response(), { propose: () => json(response({ scene: { ...scene, scene_version: 4, snapshot_id: 'snapshot-b', source_frame: 200, calibration_status: 'needs_confirmation', surfaces: [{ ...scene.surfaces[0], proposal_id: 'old-proposal', confirmed_by_user: false }] } })) })
    await loadImage(); fireEvent.click(screen.getByRole('button', { name: '重新识别场景' }))
    await screen.findByText('已获取新的来源画面。请逐一检查并确认区域。')
    await loadImage()
    fireEvent.click(screen.getByRole('button', { name: '工作桌面 待确认' }))
    expect(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' })).not.toBeChecked()
    fireEvent.click(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' }))
    fireEvent.click(screen.getByRole('button', { name: '保存已确认区域' }))
    await waitFor(() => expect(savedBodies(fetchMock)).toHaveLength(1))
    expect(savedBodies(fetchMock)[0]).toMatchObject({ scene_version: 4, snapshot_id: 'snapshot-b', source_frame: 200 })
    expect(savedBodies(fetchMock)[0].surfaces[0]).not.toHaveProperty('proposal_id')
  })

  it('支持可访问数值画凸区域、删除顶点和删除区域；未确认前不发PUT', async () => {
    const { fetchMock } = setup(response({ scene: { ...scene, surfaces: [] } }))
    await loadImage(); fireEvent.click(screen.getByRole('button', { name: '手动画区域' }))
    fireEvent.change(screen.getByRole('textbox', { name: '场景区域名称' }), { target: { value: '相机盲区' } })
    fireEvent.click(screen.getByText('用数值编辑顶点（百分比）'))
    for (const [index, [x, y]] of polygon.entries()) {
      fireEvent.click(screen.getByRole('button', { name: '添加数值顶点' }))
      fireEvent.change(screen.getByRole('spinbutton', { name: `顶点${index + 1}横坐标百分比` }), { target: { value: String(x * 100) } })
      fireEvent.change(screen.getByRole('spinbutton', { name: `顶点${index + 1}纵坐标百分比` }), { target: { value: String(y * 100) } })
    }
    expect(savedBodies(fetchMock)).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: '删除顶点4' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' }))
    expect(screen.getByRole('button', { name: '保存已确认区域' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '删除区域相机盲区' }))
    expect(screen.queryByRole('textbox', { name: '场景区域名称' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存已确认区域' }))
    await waitFor(() => expect(savedBodies(fetchMock)[0].surfaces).toEqual([]))
  })

  it('同一来源坐标只显示最后看到；点击读取该观察截图和独立帧时间，不虚构录像', async () => {
    setup(response({ items: [result] }))
    await loadImage()
    const marker = screen.getByRole('button', { name: '查看家门钥匙的最后观察' })
    expect(marker).toHaveStyle({ left: '32%', top: '45%' })
    fireEvent.click(marker)
    const details = within(screen.getByRole('region', { name: '选中物品的观察证据' }))
    expect(details.getByRole('img', { name: '家门钥匙最后可见截图' })).toHaveAttribute('src', observed.screenshot_path)
    expect(details.getByText(/最后观察帧：130/)).toBeInTheDocument()
    expect(details.getByText(/截图来源帧：125/)).toBeInTheDocument()
    expect(details.getByText(/外观分数 0.920（非正确率）/)).toBeInTheDocument()
    expect(details.queryByRole('link', { name: '播放事件录像' })).not.toBeInTheDocument()
    expect(details.queryByText('不可使用的预设答案')).not.toBeInTheDocument()
  })

  it('指针按真实显示宽高归一化描边，横纵不同缩放不会互相混用', async () => {
    vi.stubGlobal('PointerEvent', MouseEvent)
    const { container } = setup(response({ scene: { ...scene, surfaces: [] } }))
    await loadImage(); fireEvent.click(screen.getByRole('button', { name: '手动画区域' }))
    const canvas = screen.getByLabelText('场景区域编辑画布')
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ x: 100, y: 50, left: 100, top: 50, width: 600, height: 400, right: 700, bottom: 450, toJSON: () => ({}) })
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 90 })
    fireEvent.pointerDown(canvas, { clientX: 580, clientY: 90 })
    fireEvent.pointerDown(canvas, { clientX: 580, clientY: 330 })
    expect(container.querySelector('.scene-surface polygon')).toHaveAttribute('points', '100,100 800,100 800,700')
    expect(screen.getByRole('button', { name: '保存已确认区域' })).toBeDisabled()
  })

  it('不把遮挡历史点画成可确定的当前位置', async () => {
    setup(response({ items: [{ ...result, last_observed: { ...observed, event_type: 'occluded' }, status: 'occluded' }] }))
    await loadImage()
    const marker = screen.getByRole('button', { name: '查看家门钥匙的最后观察' })
    expect(marker).toHaveClass('uncertain')
    expect(screen.getByText('被遮挡 · 最后可见点')).toBeInTheDocument()
  })

  it('保存中切换相机不会把旧相机迟到的响应写进新场景', async () => {
    let resolveSave: (value: Response) => void = () => undefined
    const delayed = new Promise<Response>((resolve) => { resolveSave = resolve })
    const secondCamera = { ...camera, id: 'cam-two', name: '客厅相机' }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/runtime-config') return json({ runtime_mode: 'REAL' })
      if (path === '/api/cameras') return json([camera, secondCamera])
      if (init?.method === 'PUT') return delayed
      return json(path.includes('cam-two') ? response({ scene: null }) : response())
    })
    vi.stubGlobal('fetch', fetchMock); vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<MemoryRouter><RuntimeProvider><ScenePage /></RuntimeProvider></MemoryRouter>)
    await loadImage(); editExisting()
    fireEvent.change(screen.getByRole('textbox', { name: '场景区域名称' }), { target: { value: '新桌面' } })
    fireEvent.click(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' }))
    fireEvent.click(screen.getByRole('button', { name: '保存已确认区域' }))
    fireEvent.change(screen.getByRole('combobox', { name: '选择场景摄像头' }), { target: { value: 'cam-two' } })
    await screen.findByText('还没有这个房间的场景')
    resolveSave(json(response()))
    await waitFor(() => expect(screen.getByRole('combobox', { name: '选择场景摄像头' })).toHaveValue('cam-two'))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByText(/区域已由服务端保存/)).not.toBeInTheDocument()
  })

  it.each([
    ['不同相机', { camera_id: 'another' }], ['不同会话', { source_session_id: 'old-session' }],
    ['越界坐标', { final_position: [1.2, .3] }], ['非数字坐标', { final_position: ['.2', .3] }],
    ['演示数据', { runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true }], ['拒绝身份', { identity_evidence: { accepted: false } }],
  ])('%s不画成此场景的真实位置点', async (_name, changes) => {
    setup(response({ items: [{ ...result, last_observed: { ...observed, ...changes } }] }))
    await loadImage()
    expect(screen.queryByRole('button', { name: '查看家门钥匙的最后观察' })).not.toBeInTheDocument()
  })

  it.each(['needs_confirmation', 'needs_review'])('场景%s时不能叠加旧位置点', async (calibration_status) => {
    setup(response({ scene: { ...scene, calibration_status }, items: [result] }))
    await loadImage()
    expect(screen.queryByRole('button', { name: '查看家门钥匙的最后观察' })).not.toBeInTheDocument()
  })

  it('可能持有、遮挡和推测明确区分，不能把下方家具当放置证据', async () => {
    setup(response({ items: [{ ...result, last_observed: { ...observed, holding_status: 'possibly_held' }, location_hypotheses: [{ label: '可能接近门口', reason: '上一段可靠观察的移动方向', evidence_type: 'inferred', source_session_id: 'session-a' }] }] }))
    await loadImage()
    expect(screen.getByText('可能持有 · 仅附近位置，未确认放下')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看家门钥匙的最后观察' }))
    expect(screen.getByRole('region', { name: '有依据的候选位置' })).toHaveTextContent('上一段可靠观察的移动方向')
    expect(screen.queryByRole('button', { name: /可能接近门口/ })).not.toBeInTheDocument()
  })

  it.each([
    ['co_moving', '连续共同移动 · 可能携带，未确认放下'],
    ['holding_uncertain', '持有状态不确定 · 当前位置未确认'],
    ['nearby', '手在附近 · 未建立持有关系'],
  ])('场景对新holding_status=%s保守描述，不变为确认放下', async (holding_status, label) => {
    setup(response({ items: [{ ...result, last_observed: { ...observed, holding_status } }] }))
    await loadImage()
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.queryByText('持有中 · 仅附近位置，未确认放下')).not.toBeInTheDocument()
  })

  it('图片缺失、尺寸错配和场景失效都不能保存，即使轮廓已存在', async () => {
    setup(response({ scene: { ...scene, invalid_reason: '摄像头方向变化' }, items: [result] }))
    const image = await loadImage()
    expect(screen.getByRole('button', { name: '手动画区域' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: '查看家门钥匙的最后观察' })).not.toBeInTheDocument()
    fireEvent.error(image)
    expect(screen.getByText(/场景截图无法读取或尺寸不符/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存已确认区域' })).toBeDisabled()
  })

  it.each(['insufficient_detail', 'failed'])('稳定性%s只标注人工参考，不冒充稳定成功且允许人工区域编辑', async (status) => {
    setup(response({ scene_stability: { status } }))
    await loadImage()
    expect(screen.getByText(/画面纹理不足或稳定性检查未完成，位置图仅供人工参考/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '手动画区域' })).toBeEnabled()
  })

  it('版本冲突不显示保存成功、不自动重试或伪造新版本', async () => {
    const { fetchMock } = setup(response(), { save: () => json({ detail: '场景版本已变化，请重新加载。' }, 409) })
    await loadImage(); editExisting()
    fireEvent.change(screen.getByRole('textbox', { name: '场景区域名称' }), { target: { value: '桌面改名' } })
    fireEvent.click(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' }))
    fireEvent.click(screen.getByRole('button', { name: '保存已确认区域' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('场景版本已变化')
    expect(screen.queryByText(/区域已由服务端保存/)).not.toBeInTheDocument()
    expect(savedBodies(fetchMock)).toHaveLength(1)
    expect(screen.getByRole('textbox', { name: '场景区域名称' })).toHaveValue('桌面改名')
  })

  it('场景刷新同一截图仍可重新解码编辑，不把loading永久卡住', async () => {
    setup(response())
    const first = await loadImage()
    fireEvent.click(screen.getByRole('button', { name: '刷新记录' }))
    await screen.findByText('已读取服务端最新场景和观察。')
    const second = await loadImage()
    expect(second).not.toBe(first)
    expect(screen.getByRole('button', { name: '手动画区域' })).toBeEnabled()
  })

  it('视频选择与物品列表使用同一item_id，刷新只读且保留当前选择', async () => {
    const { fetchMock } = setup(response({ items: [result] }))
    await loadImage()
    fireEvent.click(screen.getByRole('button', { name: '视频选择家门钥匙' }))
    expect(screen.getByRole('region', { name: '选中物品的观察证据' })).toHaveTextContent('家门钥匙')
    expect(within(screen.getByRole('region', { name: '这个摄像头的最后观察' })).getByRole('button')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: '刷新记录' }))
    await screen.findByText('已读取服务端最新场景和观察。')
    expect(screen.getByRole('region', { name: '选中物品的观察证据' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })

  it('旧二维场景不凭空生成3D，只显式保存用户确认的尺寸和四角', async () => {
    const { fetchMock } = setup(response())
    await loadImage()
    expect(await screen.findByText('还没有已确认的三维家具')).toBeInTheDocument()
    editExisting()
    fireEvent.click(screen.getByRole('checkbox', { name: '为此区域设置三维尺寸与支撑面高度' }))
    expect(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' })).not.toBeChecked()
    fireEvent.change(screen.getByRole('spinbutton', { name: '宽度' }), { target: { value: '1.8' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: '纵深' }), { target: { value: '.8' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: '支撑面高度' }), { target: { value: '.75' } })
    fireEvent.click(screen.getByRole('button', { name: '将当前四个角确认为平面标定锚点' }))
    expect(savedBodies(fetchMock)).toHaveLength(0)
    fireEvent.click(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' }))
    fireEvent.click(screen.getByRole('button', { name: '保存已确认区域' }))
    await waitFor(() => expect(savedBodies(fetchMock)).toHaveLength(1))
    const saved = savedBodies(fetchMock)[0].surfaces[0]
    expect(saved.physical).toEqual({ width: 1.8, depth: .8, height: .75, thickness: .05, origin_x: 0, origin_z: 0, rotation_y: 0, unit: 'relative' })
    expect(saved.calibration).toEqual({ image_points: polygon, plane_points: [[0, 0], [1.8, 0], [1.8, .8], [0, .8]], validation_points: [] })
  })

  it('变更尺寸废弃旧投影，独立验证点为空时禁止确认', async () => {
    setup(response())
    await loadImage(); editExisting()
    fireEvent.click(screen.getByRole('checkbox', { name: '为此区域设置三维尺寸与支撑面高度' }))
    fireEvent.click(screen.getByRole('button', { name: '将当前四个角确认为平面标定锚点' }))
    fireEvent.click(screen.getByRole('button', { name: '添加独立验证点' }))
    expect(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' })).toBeDisabled()
    for (const [name, value] of [['图像横', '45'], ['图像纵', '40'], ['平面横', '.5'], ['平面纵', '.5']]) fireEvent.change(screen.getByRole('spinbutton', { name: `验证点1${name}` }), { target: { value } })
    expect(screen.getByRole('checkbox', { name: '我已核对名称、类型与区域边界' })).toBeEnabled()
    fireEvent.change(screen.getByRole('spinbutton', { name: '宽度' }), { target: { value: '2' } })
    expect(screen.queryByRole('spinbutton', { name: '锚点1图像横' })).not.toBeInTheDocument()
    expect(screen.queryByRole('spinbutton', { name: '验证点1图像横' })).not.toBeInTheDocument()
  })

  it('unknown marker保留null来源字段，不被页面补成当前版本或原点', async () => {
    setup(response({ items: [result], markers: [{ item_id: 'keys', camera_id: 'cam', scene_version: null, frame_id: null, source_session_id: null, observation_timestamp: null, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, position_status: 'unknown', mapping_method: 'none', map_position: null, reason: '旧观察缺少场景版本' }] }))
    await loadImage(); fireEvent.click(screen.getByRole('button', { name: '视频选择家门钥匙' }))
    const details = screen.getByRole('region', { name: '选中物品的观察证据' })
    expect(details).toHaveTextContent('当前位置未知：旧观察缺少场景版本')
    expect(details).not.toHaveTextContent('已标定位置')
  })
})

describe('场景凸多边形与未确认移动标签纯函数', () => {
  it('接受有效矩形，拒绝重复点/凹/交叉/面积过小/越界', () => {
    expect(validScenePolygon(polygon)).toBe(true)
    for (const invalid of [[], [[0, 0], [1, 1]], [[0, 0], [1, 0], [1, 0], [0, 1]], [[0, 0], [1, 1], [1, 0], [0, 1]], [[0, 0], [1, 0], [.5, .2], [1, 1], [0, 1]], [[0, 0], [.001, 0], [0, .001]], [[0, 0], [1.2, 0], [0, 1]]] as [number, number][][]) expect(validScenePolygon(invalid)).toBe(false)
  })
  it('位置变化事件不能使用确认放下文案或成功徽章', () => {
    const event = { ...observed, event_type: 'movement' as const, evidence_status: 'confirmed', final_status: 'position_changed' }
    expect(eventLabel(event)).toBe('位置变化，未确认放下')
    expect(evidenceBadge(event)).toEqual({ label: '位置变化，未确认放下', tone: 'warning' })
  })
})
