import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CameraDiagnosticsPage } from '../src/pages/CameraDiagnosticsPage'

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })

function setup(runResult: unknown, runStatus = 202, cameras: unknown[] = []) {
  let envelope: unknown = { report: null, running_cameras: [], diagnostic_running: false }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runtime-config') return json({ mode: 'REAL', runtime_mode: 'REAL' })
    if (path === '/api/cameras') return json(cameras)
    if (path === '/api/cameras/cam1/start') return new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    })
    if (path === '/api/camera-diagnostics') return json(envelope)
    if (path === '/api/camera-diagnostics/run') { if (runStatus < 400) envelope = runResult; return json(runResult, runStatus) }
    if (path === '/api/camera-diagnostics/cancel') { envelope = { diagnostic_running: false, job: { id: 'job1', status: 'CANCELLED', message: '诊断已取消', completed: 1, total: 3, overall_timeout_seconds: 45 } }; return json(envelope) }
    throw new Error(`Unexpected UI contract request: ${path} ${init?.method}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<MemoryRouter><CameraDiagnosticsPage /></MemoryRouter>)
  return fetchMock
}

describe('Mock 组件合同：摄像头诊断交互，不证明真实硬件', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('进入和快速刷新页面只读状态，不自动调用硬件探测', async () => {
    const fetchMock = setup({})
    await waitFor(() => expect(screen.getByRole('button', { name: '快速刷新状态' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '快速刷新状态' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([path]) => String(path) === '/api/camera-diagnostics').length).toBeGreaterThan(1))
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    expect(screen.getByRole('textbox', { name: '待探测摄像头编号' })).toHaveValue('0')
  })

  it('真实接口返回拒绝时错误持续可见，不被报告刷新覆盖', async () => {
    setup({ detail: '已有采集正在释放，请稍后重试' }, 409)
    fireEvent.click(screen.getByRole('button', { name: '探测选定编号' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('已有采集正在释放')
    await waitFor(() => expect(screen.getByRole('button', { name: '快速刷新状态' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '快速刷新状态' }))
    expect(screen.getByRole('alert')).toHaveTextContent('已有采集正在释放')
  })

  it('后台任务显示进度，取消绑定当前job而不停止摄像头', async () => {
    const fetchMock = setup({ diagnostic_running: true, job: { id: 'job1', status: 'RUNNING', message: '正在检测编号 0', completed: 1, total: 3, overall_timeout_seconds: 45 } })
    fireEvent.click(screen.getByRole('button', { name: '探测选定编号' }))
    expect(await screen.findByRole('progressbar')).toHaveAttribute('value', '1')
    fireEvent.click(screen.getByRole('button', { name: '取消诊断' }))
    expect(await screen.findByText('诊断已取消')).toBeInTheDocument()
    const request = fetchMock.mock.calls.find(([path]) => String(path).endsWith('/cancel'))
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ job_id: 'job1' })
    expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith('/stop'))).toBe(false)
  })

  it('诊断轮询或状态刷新引起render时不会替换同一摄像头MJPEG地址', async () => {
    const fetchMock = setup({}, 202, [{ id: 'cam1', name: '受控UI摄像头', source_type: 'webcam', source: '0', room_name: '客厅' }])
    expect(await screen.findByRole('option', { name: /受控UI摄像头/ })).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: '完整链路摄像头' }), { target: { value: 'cam1' } })
    fireEvent.click(screen.getByRole('button', { name: '开始测试 C' }))
    const preview = await screen.findByAltText('测试 C 的后端实时连续画面')
    const initialSource = preview.getAttribute('src')
    expect(initialSource).toBe('/api/cameras/cam1/stream')
    await waitFor(() => expect(screen.getByRole('button', { name: '快速刷新状态' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '快速刷新状态' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([path]) => String(path) === '/api/camera-diagnostics').length).toBeGreaterThan(1))
    expect(screen.getByAltText('测试 C 的后端实时连续画面')).toBe(preview)
    expect(preview).toHaveAttribute('src', initialSource)
  })
})
