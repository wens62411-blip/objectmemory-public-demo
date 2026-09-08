import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode, useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../src/App'
import { Modal } from '../src/components/UI'
import { ToastProvider, useToast } from '../src/components/Toast'
import { RuntimeProvider } from '../src/contexts/RuntimeContext'
import { CamerasPage } from '../src/pages/CamerasPage'
import { SettingsPage } from '../src/pages/SettingsPage'

const json = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } })
function mockApi() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return json({ authenticated: true })
    if (path === '/api/runtime-config') return json({ runtime_mode: 'REAL' })
    if (path === '/api/health') return json({ status: 'ok', stats: { items: 0, cameras: 1 } })
    if (path === '/api/cameras') return json([{ id: 'cam-1', name: '客厅镜头', room_name: '客厅', source_type: 'webcam', health: { status: 'ready' } }])
    if (path === '/api/settings') return json({ detection_mode: 'aruco', inference_fps: 5, static_seconds: 1.8, pre_seconds: 5, post_seconds: 5, retention_days: 7, save_clips: true, hand_detection_enabled: true, show_hands: true, privacy_mode: true, record_events: true })
    return json([])
  }))
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('前端键盘操作与重复请求回归', () => {
  it('菜单隔离背景、循环焦点，并在 Escape 后回到实际入口', async () => {
    mockApi()
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
    render(<MemoryRouter><App /></MemoryRouter>)
    const user = userEvent.setup()
    const trigger = await screen.findByRole('button', { name: '更多' })
    await user.click(trigger)
    const menu = screen.getByRole('dialog', { name: '导航菜单' })
    expect(menu).toHaveFocus()
    expect(document.querySelector<HTMLElement>('.main-column')?.inert).toBe(true)
    const summaries = menu.querySelectorAll('summary')
    ;(summaries[1] as HTMLElement).focus()
    await user.tab()
    expect(within(menu).getByRole('link', { name: '物忆首页' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(document.querySelector<HTMLElement>('.main-column')?.inert).not.toBe(true)
    expect(trigger).toHaveFocus()
  })

  it('弹窗跳过关闭的折叠内容和负 tabindex，保留原有背景隔离状态', async () => {
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
    function Harness() {
      const [open, setOpen] = useState(false)
      return <><button onClick={() => setOpen(true)}>打开</button>{open && <Modal title="焦点测试" onClose={() => setOpen(false)}><button>操作</button><details><summary>选填</summary><button>隐藏操作</button></details><button tabIndex={-1}>仅程序聚焦</button></Modal>}</>
    }
    render(<Harness />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '打开' }))
    screen.getByText('选填').focus()
    await user.tab()
    expect(screen.getByRole('button', { name: '关闭' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.getByRole('button', { name: '打开' })).toHaveFocus()
    expect(document.body.style.overflow).toBe('')
  })

  it('识别方式有选中语义，1.8 秒等待值与滑块实际值一致', async () => {
    mockApi()
    render(<MemoryRouter><SettingsPage /></MemoryRouter>)
    const stable = await screen.findByRole('button', { name: /稳定标签模式/ })
    const experimental = screen.getByRole('button', { name: /无标签 AI 实验模式/ })
    expect(stable).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(experimental)
    expect(experimental).toHaveAttribute('aria-pressed', 'true')
    expect(stable).toHaveAttribute('aria-pressed', 'false')
    const slider = screen.getByRole('slider', { name: '确认放下等待' })
    expect(slider).toHaveValue('1.8')
    expect(slider).toHaveAttribute('step', '0.1')
  })

  it('弹窗内的失败通知保持可访问，关闭通知后焦点留在弹窗', async () => {
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
    function Harness() {
      const [open, setOpen] = useState(false)
      const { notify } = useToast()
      return <><button onClick={() => setOpen(true)}>打开</button>{open && <Modal title="保存物品" onClose={() => setOpen(false)}><button onClick={() => notify('保存失败，请重试', 'danger')}>保存</button></Modal>}</>
    }
    render(<ToastProvider><Harness /></ToastProvider>)
    const user = userEvent.setup()
    const trigger = screen.getByRole('button', { name: '打开' })
    await user.click(trigger)
    const dialog = screen.getByRole('dialog')
    expect(trigger.inert).toBe(true)
    const live = dialog.querySelector<HTMLElement>('[aria-live="polite"]')!
    expect(live).not.toBeNull()
    await user.click(within(dialog).getByRole('button', { name: '保存' }))
    expect(within(live).getByText('保存失败，请重试')).toBeInTheDocument()
    for (let element: HTMLElement | null = live; element; element = element.parentElement) expect(element.inert).not.toBe(true)
    const dismiss = within(dialog).getByRole('button', { name: '关闭提示' })
    dismiss.focus()
    await user.tab()
    expect(within(dialog).getByRole('button', { name: '关闭' })).toHaveFocus()
    await user.click(dismiss)
    expect(screen.queryByText('保存失败，请重试')).not.toBeInTheDocument()
    expect(dialog).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(trigger).toHaveFocus()
    expect(trigger.inert).not.toBe(true)
  })

  it('嵌套弹窗关闭时通知依次返回父弹窗和页面，不丢失或重复', async () => {
    function Harness() {
      const [outer, setOuter] = useState(false)
      const [inner, setInner] = useState(false)
      const { notify } = useToast()
      return <><button onClick={() => { notify('待处理通知'); setOuter(true) }}>打开</button>{outer && <Modal title="父弹窗" onClose={() => setOuter(false)}><button onClick={() => setInner(true)}>继续</button>{inner && <Modal title="子弹窗" onClose={() => setInner(false)}>{null}</Modal>}</Modal>}</>
    }
    render(<StrictMode><ToastProvider><Harness /></ToastProvider></StrictMode>)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '打开' }))
    const outer = screen.getByRole('dialog', { name: '父弹窗' })
    expect(within(outer).getByText('待处理通知')).toBeInTheDocument()
    await user.click(within(outer).getByRole('button', { name: '继续' }))
    const inner = screen.getByRole('dialog', { name: '子弹窗' })
    expect(within(inner).getByText('待处理通知')).toBeInTheDocument()
    await user.click(within(inner).getByRole('button', { name: '关闭' }))
    expect(within(outer).getByText('待处理通知')).toBeInTheDocument()
    await user.click(within(outer).getByRole('button', { name: '关闭' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getAllByText('待处理通知')).toHaveLength(1)
    expect(document.querySelectorAll('.toast-stack')).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: '关闭提示' }))
    expect(screen.queryByText('待处理通知')).not.toBeInTheDocument()
  })

  it('打开向导和修改选项不刷新已有摄像头缩略图，显式刷新后更新', async () => {
    mockApi()
    let now = 1000
    vi.spyOn(Date, 'now').mockImplementation(() => now++)
    render(<MemoryRouter><RuntimeProvider><CamerasPage /></RuntimeProvider></MemoryRouter>)
    const img = await screen.findByRole('img', { name: '客厅镜头当前画面' })
    await screen.findByText('视频正常')
    const first = img.getAttribute('src')
    fireEvent.click(screen.getAllByRole('button', { name: '添加摄像头' })[0])
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /手机 \/ 浏览器摄像头/ }))
    expect(img).toHaveAttribute('src', first)
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.getByRole('img', { name: '客厅镜头当前画面' })).not.toHaveAttribute('src', first))
  })
})
