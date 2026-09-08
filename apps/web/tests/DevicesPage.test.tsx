import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DevicesPage } from '../src/pages/DevicesPage'

const runtime = vi.hoisted(() => ({ mode: 'REAL' }))
vi.mock('../src/contexts/RuntimeContext', () => ({ useRuntime: () => runtime }))
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })

function setup(session: object | null, options: { usbStatus?: object; historicalReply?: Promise<Response>; ports?: object[]; firmwareStatus?: object; networkStatus?: object } = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') {
      if (session === null) throw new Error('session unavailable')
      return json(session)
    }
    if (path === '/api/firmware/ports') return json({ platformio: { available: true }, ports: options.ports ?? [{ device: 'COM7', eligible: true, description: 'Mock USB bridge; no physical-board proof' }] })
    if (path.startsWith('/api/firmware/status')) return json(options.firmwareStatus || {})
    if (path === '/api/firmware/boards') return json({ boards: [], notice: '测试能力表，非物理设备证据' })
    if (path === '/api/firmware/network-status') return json(options.networkStatus || { status: 'unavailable', connections: [] })
    if (path === '/api/firmware/auto-usb') return json(options.usbStatus || { runtime_mode: 'REAL', enabled: false, message: 'Mock 未绑定' })
    if (path === '/api/firmware/auto-usb/recover' && init?.method === 'POST') return json({ id: 'accepted-recovery', job_type: 'usb_recover', status: 'queued' })
    if (path === '/api/firmware/jobs/historical-job') return options.historicalReply ? (await options.historicalReply).clone() : json({ id: 'historical-job', status: 'failed', stage: '旧任务，仅测试' })
    if (path === '/api/virtual-device/status') return json({ running: false })
    if (path === '/api/devices') return json([])
    throw new Error(`unexpected request: ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<MemoryRouter><DevicesPage /></MemoryRouter>)
  return fetchMock
}

describe('Mock component contracts: ESP32 LAN preflight, not hardware acceptance', () => {
  beforeEach(() => { localStorage.clear(); runtime.mode = 'REAL' })
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it.each([
    { lan_enabled: false, listener_verified: true, lan_urls: ['http://192.168.1.20:8018'] },
    { lan_enabled: true, listener_verified: false, lan_urls: ['http://192.168.1.20:8018'] },
    null,
  ])('keeps install and network enrollment disabled when listener is unverified: %j', async (session) => {
    const fetchMock = setup(session)
    await screen.findByText(/scripts\/start.ps1 -Lan/)
    fireEvent.change(screen.getByPlaceholderText('家庭 Wi-Fi'), { target: { value: 'Home' } })
    fireEvent.click(screen.getByRole('button', { name: '高级设置' }))
    fireEvent.change(screen.getByLabelText(/物忆后台地址/), { target: { value: 'http://192.168.1.20:8018' } })
    expect(screen.getByRole('button', { name: '一键安装并绑定' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '只编译固件' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: /备用热点网络申领/ }))
    expect(screen.getByRole('button', { name: '生成 10 分钟配对码' })).toBeDisabled()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/firmware/install'))).toBe(false)
  })

  it('enables a complete form only for a verified LAN listener and labels URL as candidate', async () => {
    setup({ lan_enabled: true, listener_verified: true, lan_urls: ['http://192.168.1.20:8018'] })
    expect(await screen.findByText(/后台候选地址/)).toHaveTextContent('尚未验证 ESP32、路由器或防火墙端到端可达')
    fireEvent.change(screen.getByPlaceholderText('家庭 Wi-Fi'), { target: { value: 'Home' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '一键安装并绑定' })).toBeEnabled())
    expect(screen.queryByText(/ESP32 将连接/)).not.toBeInTheDocument()
  })

  it('shows enumerated USB separately from unverified board identity, flash and real video', async () => {
    setup({ lan_enabled: true, listener_verified: true }, { firmwareStatus: {
      physical_board_detected: false, physical_flash_performed: false, real_video_verified: false,
      physical_status_note: '未检测到真实 ESP32-CAM 硬件，尚未进行真机刷写。',
    } })
    expect(await screen.findByText('USB 串口已发现，板卡身份未验证')).toBeInTheDocument()
    expect(screen.getByText(/已发现可用 USB 串口；这不代表板型/)).toBeInTheDocument()
    expect(screen.queryByText('未检测到真实 ESP32-CAM 硬件，尚未进行真机刷写。')).not.toBeInTheDocument()
    expect(screen.getByText('真机刷写').parentElement).toHaveTextContent('未执行')
    expect(screen.getByText('真机视频').parentElement).toHaveTextContent('未验证')
    expect(screen.queryByText('检测到候选物理板')).not.toBeInTheDocument()
  })

  it('keeps no-USB guidance mode-specific and physical installation disabled in DEMO', async () => {
    const session = { lan_enabled: true, listener_verified: true, lan_urls: ['http://192.168.1.20:8018'] }
    const realFetch = setup(session, { ports: [] })
    expect(await screen.findByText(/检查可传数据的 USB 线和板卡连接/)).toHaveTextContent('REAL 模式不运行虚拟设备')
    expect(screen.queryByRole('button', { name: '启动虚拟设备' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '一键安装并绑定' })).toBeDisabled()
    expect(realFetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    expect(realFetch.mock.calls.some(([path]) => path === '/api/virtual-device/status')).toBe(false)
    cleanup()
    runtime.mode = 'DEMO'
    const demoFetch = setup(session, { ports: [] })
    expect(await screen.findByText(/当前为 DEMO 模式，可在上方运行虚拟设备/)).toHaveTextContent('不代表已连接物理开发板')
    expect(screen.getByRole('button', { name: '启动虚拟设备' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '一键安装并绑定' })).toBeDisabled()
    expect(screen.getByText('仅 REAL 模式检测')).toBeInTheDocument()
    expect(demoFetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    await waitFor(() => expect(demoFetch.mock.calls.some(([path]) => path === '/api/virtual-device/status')).toBe(true))
  })

  it('remembers only the whitelisted board, never password or checkbox authorization', async () => {
    const session = { lan_enabled: true, listener_verified: true, lan_urls: ['http://192.168.1.20:8018'] }
    setup(session)
    await screen.findByText(/后台候选地址/)
    fireEvent.change(screen.getByLabelText(/开发板型号/), { target: { value: 'xiao_esp32s3_sense' } })
    fireEvent.change(screen.getByLabelText(/Wi-Fi 密码/), { target: { value: 'ephemeral-private-password' } })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(localStorage.getItem('objectmemory.usbBoardModel')).toBe('xiao_esp32s3_sense')
    expect(Object.keys(localStorage)).toEqual(['objectmemory.usbBoardModel'])
    cleanup()
    setup(session)
    await screen.findByText(/后台候选地址/)
    expect(screen.getByLabelText(/开发板型号/)).toHaveValue('xiao_esp32s3_sense')
    expect(screen.getByLabelText(/Wi-Fi 密码/)).toHaveValue('')
    expect(screen.getByRole('checkbox')).not.toBeChecked()
  })

  it('rejects a non-whitelisted remembered board value', async () => {
    localStorage.setItem('objectmemory.usbBoardModel', '../../arbitrary-board')
    setup({ lan_enabled: true, listener_verified: true })
    await waitFor(() => expect(screen.getByLabelText(/开发板型号/)).toHaveValue('ai_thinker_esp32cam'))
    expect(localStorage.getItem('objectmemory.usbBoardModel')).toBe('ai_thinker_esp32cam')
  })

  it('fills the unique host Wi-Fi, unique USB, names and backend so only a password is needed', async () => {
    const fetcher = setup({ lan_enabled: true, listener_verified: true, lan_urls: ['http://192.168.1.20:8018'] }, {
      networkStatus: { status: 'connected', connections: [{ ssid: 'Contract hotspot', band: '2.4 GHz', observed_2_4ghz: true }] },
    })
    await waitFor(() => expect(screen.getByLabelText('Wi-Fi 名称')).toHaveValue('Contract hotspot'))
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^USB 串口/ })).toHaveValue('COM7'))
    fireEvent.change(screen.getByLabelText(/Wi-Fi 密码/), { target: { value: 'contract-password' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '一键安装并绑定' })).toBeEnabled())
    expect(screen.getByLabelText('设备名称')).toHaveValue('客厅摄像头')
    expect(screen.getByLabelText('房间名称')).toHaveValue('客厅')
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
    expect(Object.keys(localStorage)).toEqual(['objectmemory.usbBoardModel'])
  })

  it('never guesses among multiple USB ports or multiple host networks', async () => {
    setup({ lan_enabled: true, listener_verified: true, lan_urls: ['http://192.168.1.20:8018'] }, {
      ports: [{ device: 'COM7', eligible: true }, { device: 'COM8', eligible: true }],
      networkStatus: { status: 'connected', connections: [{ ssid: 'Network A' }, { ssid: 'Network B' }] },
    })
    await screen.findByText(/检测到多个可用 USB 串口/)
    expect(screen.getByRole('combobox', { name: /^USB 串口/ })).toHaveValue('')
    expect(screen.getByLabelText('Wi-Fi 名称')).toHaveValue('')
    expect(screen.getByText(/只有密码无法确定网络/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '一键安装并绑定' })).toBeDisabled()
  })

  it('recovers a initially unavailable listener without reload and preserves manually entered backend', async () => {
    const session = { lan_enabled: false, listener_verified: false, lan_urls: [] as string[] }
    setup(session)
    await screen.findByText(/scripts\/start.ps1 -Lan/)
    session.lan_enabled = true; session.listener_verified = true; session.lan_urls = ['http://192.168.1.20:8018']
    fireEvent(document, new Event('visibilitychange'))
    await screen.findByText(/后台候选地址/)
    fireEvent.change(screen.getByLabelText('Wi-Fi 名称'), { target: { value: 'Manually chosen' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '一键安装并绑定' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '高级设置' }))
    fireEvent.change(screen.getByLabelText(/物忆后台地址/), { target: { value: 'http://192.168.1.21:8018' } })
    fireEvent(document, new Event('visibilitychange'))
    await waitFor(() => expect(screen.getByLabelText(/物忆后台地址/)).toHaveValue('http://192.168.1.21:8018'))
    expect(screen.getByLabelText('Wi-Fi 名称')).toHaveValue('Manually chosen')
  })

  it('restores the server-bound board profile and makes missing post-restart password explicit', async () => {
    setup({ lan_enabled: true, listener_verified: true }, { usbStatus: {
      runtime_mode: 'REAL', enabled: true, credentials_retained: false, message: 'Mock bound board',
      binding: { board_model: 'xiao_esp32s3_sense', firmware_version: 'contract-only', manifest_sha256: 'a'.repeat(64), bridge: {} },
    } })
    await waitFor(() => expect(screen.getByLabelText(/开发板型号/)).toHaveValue('xiao_esp32s3_sense'))
    expect(screen.getByText(/后台未保留 Wi-Fi 密码/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Wi-Fi 密码/)).toHaveValue('')
  })

  it('keeps newly typed password when an old receipt is read, then clears it after accepted recovery', async () => {
    localStorage.setItem('objectmemory.usbBoardModel', 'xiao_esp32s3_sense')
    let resolve!: (response: Response) => void
    const historicalReply = new Promise<Response>((done) => { resolve = done })
    const usbStatus = { runtime_mode: 'REAL', enabled: true, message: 'Mock 已绑定',
      binding: { board_model: 'xiao_esp32s3_sense', firmware_version: 'contract-only', manifest_sha256: 'a'.repeat(64),
        mac_address: '02:00:00:00:00:01', authorized_at: 'revision-1', bridge: {} },
      receipt: { status: 'flashed', flashed_at: 'contract-time', job_id: 'historical-job' },
      recovery: { state: 'reprovision_available', allowed_actions: ['reprovision'], binding_authorized_at: 'revision-1', message: '合同演示：只配网' } }
    const fetcher = setup({ lan_enabled: true, listener_verified: true, lan_urls: ['http://192.168.1.20:8018'] }, { usbStatus, historicalReply })
    const button = await screen.findByRole('button', { name: '只重新配网，不重刷' })
    fireEvent.change(screen.getByLabelText('Wi-Fi 名称'), { target: { value: 'contract-network' } })
    fireEvent.change(screen.getByLabelText(/Wi-Fi 密码/), { target: { value: 'new-password-not-for-old-job' } })
    await act(async () => { resolve(json({ id: 'historical-job', status: 'failed', stage: '旧任务，仅测试', job_type: 'usb_auto' })) })
    // Initial port discovery may invalidate the first poll. Read the old receipt
    // again after the port has settled, as returning to the visible page does.
    fireEvent(document, new Event('visibilitychange'))
    await screen.findByText('旧任务，仅测试')
    expect(screen.getByLabelText(/Wi-Fi 密码/)).toHaveValue('new-password-not-for-old-job')
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认恢复的是已绑定的/ }))
    expect(button).toBeEnabled()
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByLabelText(/Wi-Fi 密码/)).toHaveValue(''))
    const requests = fetcher.mock.calls.filter(([path, init]) => String(path).endsWith('/recover') && init?.method === 'POST')
    expect(requests).toHaveLength(1)
    expect(JSON.parse(String(requests[0][1]?.body)).password).toBe('new-password-not-for-old-job')
    expect(Object.keys(localStorage)).toEqual(['objectmemory.usbBoardModel'])
  })
})
