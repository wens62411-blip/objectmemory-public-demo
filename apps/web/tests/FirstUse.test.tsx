import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../src/App'
import { Modal } from '../src/components/UI'
import { RuntimeProvider } from '../src/contexts/RuntimeContext'
import { CamerasPage } from '../src/pages/CamerasPage'
import { ItemsPage } from '../src/pages/ItemsPage'
import { SearchPage } from '../src/pages/SearchPage'

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const item = { id: 'new-phone', name: '我的手机', type: '手机', aliases: [], ring_enabled: false }
function mockApi(options: { cameras?: unknown[]; items?: unknown[]; results?: unknown[]; healthFails?: boolean; mode?: string } = {}) {
  const cameras = options.cameras || [], items = options.items || []
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return json({ authenticated: true })
    if (path === '/api/runtime-config') return json({ runtime_mode: options.mode || 'REAL' })
    if (path === '/api/health') return options.healthFails ? json({ detail: '暂不可读' }, 503) : json({ status: 'ok', stats: { cameras: cameras.length, items: items.length } })
    if (path === '/api/items') return json(items)
    if (path === '/api/cameras') return json(cameras)
    if (path === '/api/search' && init?.method === 'POST') return json({ results: options.results || [] })
    if (path === '/api/events?limit=6') return json([])
    return json({})
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('首次使用：示例隔离、恢复路径和渐进表单', () => {
  it.each([
    { cameras: [], items: [], action: '连接设备开始使用', href: '/cameras?add=1' },
    { cameras: [{ id: 'camera' }], items: [], action: '添加第一个物品', href: '/items?add=1' },
    { cameras: [{ id: 'camera' }], items: [item], action: '验证物品识别', href: '/items' },
  ])('已有资料决定下一步：$action', async ({ cameras, items, action, href }) => {
    mockApi({ cameras, items })
    render(<MemoryRouter><App /></MemoryRouter>)
    expect(await screen.findByRole('link', { name: action })).toHaveAttribute('href', href)
    expect(screen.getByText('查看系统概览与识别速度').closest('details')).not.toHaveAttribute('open')
  })

  it('零移动历史不否定可能已有的最后观察，也不误报首次识别尚未完成', async () => {
    // An observed stationary item updates current state without creating an event.
    // The dashboard does not fetch that state and must not infer its absence.
    const fetcher = mockApi({ cameras: [{ id: 'camera', name: '电脑摄像头' }], items: [item] })
    render(<MemoryRouter><App /></MemoryRouter>)
    await screen.findByRole('link', { name: '验证物品识别' })
    expect(screen.queryByText(/还没有位置记录/)).not.toBeInTheDocument()
    expect(screen.queryByText('暂时没有真实摄像头产生的位置记录。')).not.toBeInTheDocument()
    expect(screen.queryByText('完成首次识别')).not.toBeInTheDocument()
    expect(screen.getByText('查看识别与位置')).toBeInTheDocument()
    expect(screen.getByText(/还没有历史移动事件。最后看到的位置可能已经存在，可以直接查询/)).toBeInTheDocument()
    expect(fetcher.mock.calls.every(([input, init]) =>
      (!init?.method || init.method === 'GET') && !String(input).includes('/last-location'),
    )).toBe(true)
  })

  it('示例切换和关闭不会写入 API，也不会改变真实运行模式', async () => {
    const fetcher = mockApi()
    render(<MemoryRouter><App /></MemoryRouter>)
    const trigger = await screen.findByRole('button', { name: '先看示例' })
    trigger.focus(); fireEvent.click(trigger)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('示例说明 · 非真实记录')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: '没有记录' }))
    expect(within(dialog).getByText('示例：还没有钥匙的位置记录')).toBeInTheDocument()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    expect(screen.getByText('REAL · 真实模式')).toBeInTheDocument()
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })

  it.each([
    { items: [], cameras: [], heading: '还没有添加物品', action: '添加第一个物品' },
    { items: [item], cameras: [], heading: '没有找到匹配的物品', action: '连接摄像头' },
    { items: [item], cameras: [{ id: 'cam' }], heading: '没有找到匹配的物品', action: '查看已添加物品' },
  ])('空搜索结果提供可执行下一步：$action', async ({ items, cameras, heading, action }) => {
    mockApi({ items, cameras })
    render(<MemoryRouter initialEntries={['/search?q=手机']}><RuntimeProvider><SearchPage /></RuntimeProvider></MemoryRouter>)
    await waitFor(() => expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument())
    expect(await screen.findByRole('link', { name: action })).toBeInTheDocument()
  })

  it('已经添加但没有观察记录时引导接入，不生成假位置', async () => {
    mockApi({ items: [item], results: [{ item, evidence: null }] })
    render(<MemoryRouter initialEntries={['/search?q=手机']}><RuntimeProvider><SearchPage /></RuntimeProvider></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: '尚未连接摄像头' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '连接摄像头' })).toHaveAttribute('href', '/cameras?add=1')
    expect(screen.queryByText(/玄关桌面/)).not.toBeInTheDocument()
  })

  it.each([{ cameras: [] }, { cameras: [{ id: 'cam' }] }])('混合搜索结果不把有观察的物品也说成没有记录：$cameras', async ({ cameras }) => {
    const unobserved = { ...item, id: 'other-phone', name: '备用手机' }
    const observed = { id: 'observation', item_id: item.id, event_type: 'last_seen',
      evidence_status: 'observation_only', runtime_mode: 'REAL', source_type: 'opencv_camera',
      is_simulated: false, timestamp_start: '2026-09-09T02:00:00Z',
      zone_name: '测试桌面', confidence: 0.8 }
    mockApi({ items: [item, unobserved], cameras, results: [
      { item, last_observed: observed }, { item: unobserved, evidence: null },
    ] })
    render(<MemoryRouter initialEntries={['/search?q=手机']}><RuntimeProvider><SearchPage /></RuntimeProvider></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: '部分物品还没有位置记录' })).toBeInTheDocument()
    expect(screen.getByText('测试桌面')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '还没有可用的位置记录' })).not.toBeInTheDocument()
  })

  it('状态请求失败时，不把未知状态误报为零物品或零设备', async () => {
    const fetcher = mockApi({ healthFails: true })
    render(<MemoryRouter initialEntries={['/search?q=手机']}><SearchPage /></MemoryRouter>)
    await screen.findByRole('heading', { name: '没有找到匹配的物品' })
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/health', expect.anything()))
    expect(screen.queryByText('还没有添加物品')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '连接摄像头' })).not.toBeInTheDocument()
  })

  it('首页添加物品链接直接打开表单，照片前置且选填可展开', async () => {
    mockApi()
    render(<MemoryRouter initialEntries={['/items?add=1']}><ItemsPage /></MemoryRouter>)
    const dialog = await screen.findByRole('dialog')
    const photo = within(dialog).getByText('上传第一张参考照片')
    const optional = within(dialog).getByText('补充信息（选填）').closest('details')!
    expect(optional).not.toHaveAttribute('open')
    expect(photo.compareDocumentPosition(optional) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(within(dialog).getByText('补充信息（选填）'))
    expect(optional).toHaveAttribute('open')
    expect(within(dialog).getByRole('combobox', { name: '类型' })).toBeInTheDocument()
  })

  it('影响候选匹配的类型是外显主字段，不藏在选填信息里', async () => {
    const fetcher = mockApi()
    render(<MemoryRouter initialEntries={['/items?add=1']}><ItemsPage /></MemoryRouter>)
    const dialog = await screen.findByRole('dialog')
    const category = within(dialog).getByRole('combobox', { name: '类型' })
    expect(category.closest('details')).toBeNull()
    expect(within(dialog).getByText(/识别会按类型筛选候选/)).toBeInTheDocument()
    fireEvent.change(category, { target: { value: '手机' } })
    expect(category).toHaveValue('手机')
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })

  it('专业来源折叠，展开仍能选择 RTSP，REAL 不提供测试回放', async () => {
    const fetcher = mockApi()
    render(<MemoryRouter initialEntries={['/cameras?add=1']}><RuntimeProvider><CamerasPage /></RuntimeProvider></MemoryRouter>)
    const summary = await screen.findByText('网络与专用设备（展开选择）')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    fireEvent.click(summary)
    fireEvent.click(screen.getByRole('button', { name: /RTSP 摄像头/ }))
    expect(screen.getByText('已选择：RTSP 摄像头')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /视频文件回放/ })).not.toBeInTheDocument()
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })
})

it('弹窗键盘焦点循环，Escape 关闭后回到触发按钮', async () => {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
  function Harness() {
    const [open, setOpen] = useState(false)
    return <><button onClick={() => setOpen(true)}>打开示例</button>{open && <Modal title="示例" onClose={() => setOpen(false)}><button>最后一个操作</button></Modal>}</>
  }
  render(<Harness />)
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: '打开示例' }))
  const close = screen.getByRole('button', { name: '关闭' }), last = screen.getByRole('button', { name: '最后一个操作' })
  last.focus(); await user.tab(); expect(close).toHaveFocus()
  await user.tab({ shift: true }); expect(last).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '打开示例' })).toHaveFocus()
})
