import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../src/App'
import { AppShell } from '../src/components/AppShell'
import { ToastProvider } from '../src/components/Toast'
import { SettingsPage } from '../src/pages/SettingsPage'
import { RuntimeProvider } from '../src/contexts/RuntimeContext'

const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
const settings = { detection_mode: 'aruco', inference_fps: 5, static_seconds: 2, pre_seconds: 5, post_seconds: 5, retention_days: 7, save_clips: true, show_hands: true, privacy_mode: true, record_events: true }

describe('Mock 组件契约：品牌导航与手机入口（不是实机可达测试）', () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('首页和导航共享健康轮询，隐藏时暂停，REAL首页不读取演示固件状态', async () => {
    vi.useFakeTimers()
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const fetcher = vi.fn(async (input: RequestInfo | URL) => json({
      '/api/session': { authenticated: true }, '/api/runtime-config': { runtime_mode: 'REAL' },
      '/api/health': { status: 'ok', stats: { items: 7 } },
    }[String(input)] || []))
    vi.stubGlobal('fetch', fetcher)
    await act(async () => { render(<MemoryRouter><App /></MemoryRouter>) })
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    const healthReads = () => fetcher.mock.calls.filter(([path]) => path === '/api/health').length
    expect(healthReads()).toBe(1)
    expect(screen.getByText('本地服务正常')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(healthReads()).toBe(2)
    visibility.mockReturnValue('hidden')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
    expect(healthReads()).toBe(2)
    expect(fetcher.mock.calls.some(([path]) => path === '/api/firmware/status')).toBe(false)
  })

  it.each(['REAL', 'DEMO'])('顶栏持续保留当前%s模式，DEMO另有常驻模拟警示', async (mode) => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => json(String(input) === '/api/runtime-config' ? { runtime_mode: mode } : { status: 'ok' })))
    const { container } = render(<MemoryRouter><RuntimeProvider><AppShell /></RuntimeProvider></MemoryRouter>)
    await waitFor(() => expect(container.querySelector('.topbar-status')).toHaveAttribute('data-runtime-mode', mode))
    expect(container.querySelector('.topbar-status > .badge')?.textContent).toBe(mode === 'REAL' ? 'REAL · 真实模式' : 'DEMO')
    if (mode === 'DEMO') expect(container.querySelector('.runtime-mode-banner.runtime-demo')).toHaveTextContent('演示模式')
  })

  it('连接手机指向设置扫码入口，诊断/验收保留在默认收起的高级工具', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ status: 'ok' })))
    const { container } = render(<MemoryRouter><AppShell /></MemoryRouter>)
    expect(screen.getAllByRole('link', { name: /连接手机/ })).toHaveLength(2)
    for (const link of screen.getAllByRole('link', { name: /连接手机/ })) expect(link).toHaveAttribute('href', '/settings#mobile-access')
    expect(container.querySelector('details.advanced-nav')).not.toHaveAttribute('open')
    expect(screen.getByRole('link', { name: '摄像头诊断' })).toHaveAttribute('href', '/camera-diagnostics')
    expect(screen.getByRole('link', { name: '真实移动验收' })).toHaveAttribute('href', '/acceptance/real-movement')
    expect(screen.queryByText('物')).not.toBeInTheDocument()
    expect(container.querySelector('.brand-mark path')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('本地服务正常')).toBeInTheDocument())
  })

  it('设置生成LAN二维码，访问码默认隐藏且从不进入QR/链接，失联撤下旧地址', async () => {
    let offline = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/settings') return json(settings)
      if (offline) throw new Error('controlled offline')
      return json({ local: true, pairing_pin: '87654321', lan_enabled: true, listener_verified: true, lan_urls: ['http://192.168.1.102:8018'] })
    }))
    render(<MemoryRouter><ToastProvider><SettingsPage /></ToastProvider></MemoryRouter>)
    expect(await screen.findByRole('img', { name: '手机打开物忆的二维码' })).toBeInTheDocument()
    expect(screen.getByRole('slider', { name: '物品识别频率' })).toHaveValue('5')
    expect(screen.getByText(/不是画面帧率/)).toBeInTheDocument()
    expect(screen.queryByText('87654321')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看访问码' }))
    expect(screen.getByText('87654321')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '手机访问链接' })).toHaveValue('http://192.168.1.102:8018/')
    fireEvent.click(screen.getByRole('button', { name: '让这部手机可响铃' }))
    expect(screen.getByRole('textbox', { name: '手机访问链接' })).toHaveValue('http://192.168.1.102:8018/companion')
    offline = true
    fireEvent.click(screen.getByRole('button', { name: '刷新地址' }))
    await screen.findByRole('alert')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByText('87654321')).not.toBeInTheDocument()
  })

  it('未验证监听状态时不拿服务器提供的普通地址当可用手机入口', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => json(String(input) === '/api/settings' ? settings : { local: false, lan_urls: ['http://192.168.1.102:8018'], listener_verified: false, lan_enabled: false })))
    render(<MemoryRouter><ToastProvider><SettingsPage /></ToastProvider></MemoryRouter>)
    await screen.findByText('还没有可供手机访问的地址')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看访问码' })).not.toBeInTheDocument()
  })

  it('手机配对明确指出电脑设置位置，必须8位且未提交前不调用配对API', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => json({ authenticated: false }))
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><App /></MemoryRouter>)
    const input = await screen.findByRole('textbox', { name: '8 位主机访问码' })
    expect(screen.getByText(/设置 → 连接手机/)).toBeInTheDocument()
    fireEvent.change(input, { target: { value: '1234' } })
    expect(screen.getByRole('button', { name: '安全连接' })).toBeDisabled()
    fireEvent.change(input, { target: { value: '12345678' } })
    expect(screen.getByRole('button', { name: '安全连接' })).toBeEnabled()
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })

  it('主机失联时给连接说明和重试，不显示已连接业务页面', async () => {
    let offline = true
    vi.stubGlobal('fetch', vi.fn(async () => { if (offline) throw new Error('offline'); return json({ authenticated: false }) }))
    render(<MemoryRouter><App /></MemoryRouter>)
    await screen.findByRole('heading', { name: '暂时没有连接上' })
    expect(screen.queryByRole('navigation', { name: '主导航' })).not.toBeInTheDocument()
    offline = false
    fireEvent.click(screen.getByRole('button', { name: '重新连接' }))
    await screen.findByRole('textbox', { name: '8 位主机访问码' })
    expect(screen.queryByText(/还没有连上这台物忆电脑/)).not.toBeInTheDocument()
  })
})
