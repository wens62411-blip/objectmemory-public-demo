import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AutoUsbSetup } from '../src/components/AutoUsbSetup'

const details = { port: 'COM7', ssid: 'contract-wifi', password: 'test-secret-not-stored', backend_url: 'http://192.168.1.20:8018', device_name: 'Contract camera', room_name: 'Contract room' }
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })

describe('Mock UI authorization contracts, not physical-board proof', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
  function setup() {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/bind')) return json({ id: 'test-bind-job', job_type: 'usb_bind', status: 'queued' })
      if (String(input).endsWith('/disable')) return json({ runtime_mode: 'REAL', enabled: false, message: '已停用' })
      if (init?.method && init.method !== 'GET') throw new Error('unexpected mutation')
      return json({ runtime_mode: 'REAL', enabled: false, binding: null, message: '默认关闭；未绑定不打开串口。' })
    })
    vi.stubGlobal('fetch', fetcher)
    return fetcher
  }
  it('never opens/binds/installs on mount and requires explicit board confirmation', async () => {
    const fetcher = setup(); const job = vi.fn()
    render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} onJob={job} />)
    await screen.findByText('默认关闭；未绑定不打开串口。')
    const button = screen.getByRole('button', { name: '绑定并开始自动安装' })
    expect(button).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: /将覆盖这块板原有固件，请确认没有需要保留的程序；安装后会启动物忆摄像头应用/ })).not.toBeChecked()
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(button)
    await waitFor(() => expect(job).toHaveBeenCalledTimes(1))
    const writes = fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(writes).toHaveLength(1)
    expect(String(writes[0][0])).toBe('/api/firmware/auto-usb/bind')
    expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ ...details, board_model: 'ai_thinker_esp32cam', authorize_fixed_firmware: true })
    expect(screen.getByText(/该流程尚需真实开发板验收/)).toBeInTheDocument()
    expect(localStorage.getItem('usb-password')).toBeNull()
  })
  it.each(['DEMO', 'TEST', 'UNKNOWN'])('does not poll or permit physical actions in %s', (mode) => {
    const fetcher = setup()
    render(<AutoUsbSetup mode={mode} lanEnabled busy={false} form={details} onJob={vi.fn()} />)
    expect(screen.getByText('当前模式不允许接触物理串口。')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('changing the selected port revokes the previous explicit checkbox consent', async () => {
    setup(); const onJob = vi.fn()
    const view = render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} onJob={onJob} />)
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '绑定并开始自动安装' })).toBeEnabled()
    view.rerender(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={{ ...details, port: 'COM9' }} onJob={onJob} />)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(screen.getByRole('button', { name: '绑定并开始自动安装' })).toBeDisabled()
  })
  it('XIAO confirmation is board-specific and never submits the AI Thinker profile', async () => {
    const fetcher = setup(); const onJob = vi.fn()
    const view = render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} onJob={onJob} />)
    fireEvent.click(screen.getByRole('checkbox'))
    view.rerender(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} boardModel="xiao_esp32s3_sense" onJob={onJob} />)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(screen.getByRole('button', { name: '绑定并开始自动安装' })).toBeDisabled()
    expect(screen.getByRole('checkbox')).toHaveAccessibleName(/Seeed XIAO ESP32S3 Sense/)
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: '绑定并开始自动安装' }))
    await waitFor(() => expect(onJob).toHaveBeenCalledTimes(1))
    const writes = fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(writes).toHaveLength(1)
    expect(JSON.parse(String(writes[0][1]?.body)).board_model).toBe('xiao_esp32s3_sense')
  })

  it('LAN verification is required even after checking the board', () => {
    setup()
    render(<AutoUsbSetup mode="REAL" lanEnabled={false} busy={false} form={details} onJob={vi.fn()} />)
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '绑定并开始自动安装' })).toBeDisabled()
  })

  function recoverable(action: 'reprovision' | 'retry_install' = 'reprovision') {
    return { runtime_mode: 'REAL', enabled: true, message: '绑定来自后台',
      binding: { board_model: 'xiao_esp32s3_sense', firmware_version: 'contract-only', manifest_sha256: 'a'.repeat(64),
        mac_address: '02:00:00:00:00:01', authorized_at: 'binding-revision-1', bridge: { serial_number: 'contract-only' } },
      receipt: { status: action === 'reprovision' ? 'flashed' : 'interrupted', flashed_at: action === 'reprovision' ? 'contract-time' : null },
      recovery: { state: action === 'reprovision' ? 'reprovision_available' : 'overwrite_authorization_required',
        allowed_actions: [action], binding_authorized_at: 'binding-revision-1', message: '仅来自服务端的恢复选项' } }
  }
  function recoveryFetch(status: ReturnType<typeof recoverable>, response?: () => Promise<Response>) {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/recover')) return response ? response() : json({ id: 'recovery-job', job_type: 'usb_recover', status: 'queued' })
      if (init?.method && init.method !== 'GET') throw new Error('unexpected mutation')
      return json(status)
    })
    vi.stubGlobal('fetch', fetcher)
    return fetcher
  }
  it('offers only server-authorized reprovision, binds the exact revision, and clears password only after acceptance', async () => {
    const fetcher = recoveryFetch(recoverable()); const onJob = vi.fn(); const accepted = vi.fn()
    render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} boardModel="xiao_esp32s3_sense" onJob={onJob} onAccepted={accepted} />)
    const button = await screen.findByRole('button', { name: '只重新配网，不重刷' })
    expect(button).toBeDisabled()
    expect(screen.queryByRole('button', { name: '授权覆盖并重试一次' })).not.toBeInTheDocument()
    expect(accepted).not.toHaveBeenCalled()
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认恢复的是已绑定的/ }))
    fireEvent.click(button)
    await waitFor(() => expect(onJob).toHaveBeenCalledTimes(1))
    expect(accepted).toHaveBeenCalledTimes(1)
    const writes = fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(writes).toHaveLength(1)
    expect(JSON.parse(String(writes[0][1]?.body))).toEqual({
      action: 'reprovision', port: 'COM7', board_model: 'xiao_esp32s3_sense', mac_address: '02:00:00:00:00:01',
      manifest_sha256: 'a'.repeat(64), binding_authorized_at: 'binding-revision-1', ssid: details.ssid,
      password: details.password, backend_url: details.backend_url, authorize_recovery: true, authorize_overwrite: false,
    })
    // Even if a stale GET leaves the prior revision visible, it is consumed.
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认恢复的是已绑定的/ }))
    expect(button).toBeDisabled()
  })
  it('requires independent overwrite consent and sends at most one request for repeated clicks', async () => {
    let resolve!: (response: Response) => void
    const pending = new Promise<Response>((done) => { resolve = done })
    const fetcher = recoveryFetch(recoverable('retry_install'), () => pending)
    const onJob = vi.fn()
    render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} boardModel="xiao_esp32s3_sense" onJob={onJob} />)
    const button = await screen.findByRole('button', { name: '授权覆盖并重试一次' })
    expect(screen.queryByRole('button', { name: '只重新配网，不重刷' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认恢复的是已绑定的/ }))
    expect(button).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /我明确同意覆盖/ }))
    fireEvent.click(button); fireEvent.click(button)
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    resolve(json({ id: 'recovery-job', status: 'queued', job_type: 'usb_recover' }))
    await waitFor(() => expect(onJob).toHaveBeenCalledTimes(1))
    const write = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(String(write[1]?.body)).authorize_overwrite).toBe(true)
  })
  it('does not offer recovery for a different board profile or absent allowed action', async () => {
    const status = recoverable()
    recoveryFetch(status)
    const view = render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} onJob={vi.fn()} />)
    await screen.findByText(/恢复前请将开发板型号切换为已绑定的/)
    expect(screen.queryByRole('button', { name: '只重新配网，不重刷' })).not.toBeInTheDocument()
    status.recovery.allowed_actions = []
    view.unmount()
    render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} boardModel="xiao_esp32s3_sense" onJob={vi.fn()} />)
    await screen.findByText('仅来自服务端的恢复选项')
    expect(screen.queryByRole('button', { name: '只重新配网，不重刷' })).not.toBeInTheDocument()
  })
  it('editing the network revokes recovery and overwrite confirmations', async () => {
    recoveryFetch(recoverable('retry_install'))
    const view = render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} boardModel="xiao_esp32s3_sense" onJob={vi.fn()} />)
    const button = await screen.findByRole('button', { name: '授权覆盖并重试一次' })
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认恢复的是已绑定的/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /我明确同意覆盖/ }))
    expect(button).toBeEnabled()
    view.rerender(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={{ ...details, backend_url: 'http://192.168.43.20:8018' }} boardModel="xiao_esp32s3_sense" onJob={vi.fn()} />)
    expect(button).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: /我确认恢复的是已绑定的/ })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /我明确同意覆盖/ })).not.toBeChecked()
  })
  it('a rejected recovery keeps credentials, revokes consent, and never retries automatically', async () => {
    const fetcher = recoveryFetch(recoverable(), async () => new Response(JSON.stringify({ detail: '受控恢复拒绝' }), { status: 409, headers: { 'Content-Type': 'application/json' } }))
    const accepted = vi.fn(); const onJob = vi.fn()
    render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} boardModel="xiao_esp32s3_sense" onJob={onJob} onAccepted={accepted} />)
    const button = await screen.findByRole('button', { name: '只重新配网，不重刷' })
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认恢复的是已绑定的/ }))
    fireEvent.click(button)
    await screen.findByText('受控恢复拒绝')
    expect(accepted).not.toHaveBeenCalled(); expect(onJob).not.toHaveBeenCalled()
    expect(button).toBeDisabled()
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  })
  it('reading a remembered receipt job never invokes the accepted-submission callback', async () => {
    const accepted = vi.fn(); const onJob = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input).includes('/jobs/')
      ? json({ id: 'historical-job', status: 'failed' })
      : json({ ...recoverable(), receipt: { status: 'flashed', job_id: 'historical-job', flashed_at: 'old-evidence' } })))
    render(<AutoUsbSetup mode="REAL" lanEnabled busy={false} form={details} boardModel="xiao_esp32s3_sense" onJob={onJob} onAccepted={accepted} />)
    await waitFor(() => expect(onJob).toHaveBeenCalledTimes(1))
    expect(accepted).not.toHaveBeenCalled()
  })
})
