import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CamerasPage } from '../src/pages/CamerasPage'
import { RuntimeProvider } from '../src/contexts/RuntimeContext'
import { ToastProvider } from '../src/components/Toast'

const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
const success = { status: 'ready', success: true, width: 640, height: 480, fps: 5, snapshot_url: '/media/test-contract-frame.jpg' }

function setup(testResponse: (init?: RequestInit) => Response | Promise<Response> = () => json(success), existingCameras: unknown[] = []) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runtime-config') return json({ runtime_mode: 'REAL', mode: 'REAL' })
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/cameras' && !init?.method) return json(existingCameras)
    if (path === '/api/cameras' && init?.method === 'POST') return json({ id: 'draft-camera', ...JSON.parse(String(init.body)) })
    if (path === '/api/cameras/draft-camera' && init?.method === 'PATCH') return json({ id: 'draft-camera', ...JSON.parse(String(init.body)) })
    if (path === '/api/cameras/draft-camera/test' && init?.method === 'POST') return testResponse(init)
    if (path === '/api/cameras/draft-camera/start' && init?.method === 'POST') return json({ id: 'draft-camera', health: { status: 'ready' } })
    throw new Error(`unexpected request: ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<MemoryRouter initialEntries={['/cameras?add=1']}><ToastProvider><RuntimeProvider><CamerasPage /></RuntimeProvider></ToastProvider></MemoryRouter>)
  return fetchMock
}

async function openConnectionStep(source?: string) {
  if (source) fireEvent.click(await screen.findByRole('button', { name: new RegExp(source) }))
  fireEvent.click(await screen.findByRole('button', { name: '下一步：测试连接' }))
  if (!source) await waitFor(() => expect(screen.getByRole('button', { name: '测试连接' })).toBeEnabled())
}

describe('Mock 组件单测：摄像头被测配置绑定', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('测试编号 0 后改为 1 必须重新测试，改回 0 也不能复用旧成功', async () => {
    const fetchMock = setup()
    await openConnectionStep()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeEnabled())
    fireEvent.change(screen.getByRole('spinbutton', { name: /摄像头编号/ }), { target: { value: '1' } })
    expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeDisabled()
    expect(screen.getByText(/连接配置已更改，请重新测试/)).toBeInTheDocument()
    expect(screen.queryByAltText('连接测试保存的本机摄像头截图')).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('spinbutton', { name: /摄像头编号/ }), { target: { value: '0' } })
    expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeDisabled()
    fireEvent.change(screen.getByRole('spinbutton', { name: /摄像头编号/ }), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeEnabled())
    const patch = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/cameras/draft-camera' && init?.method === 'PATCH')
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({ source: '1', config: { index: 1 } })
    expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith('/test'))).toHaveLength(2)
  })

  it('测试未完成时改变来源，迟到的旧成功响应不能恢复继续按钮', async () => {
    let resolveResponse!: (response: Response) => void
    const response = new Promise<Response>((resolve) => { resolveResponse = resolve })
    const fetchMock = setup(() => response)
    await openConnectionStep()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/cameras/draft-camera/test', expect.objectContaining({ method: 'POST' })))
    fireEvent.change(screen.getByRole('spinbutton', { name: /摄像头编号/ }), { target: { value: '1' } })
    await act(async () => { resolveResponse(json(success)); await response })
    expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeDisabled()
    expect(screen.queryByAltText('连接测试保存的本机摄像头截图')).not.toBeInTheDocument()
  })

  it('只修改 RTSP 密码也会撤销之前连接成功的证明', async () => {
    setup()
    await openConnectionStep('RTSP 摄像头')
    fireEvent.change(screen.getByRole('textbox', { name: /视频流地址/ }), { target: { value: 'rtsp://192.168.1.10/live' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '测试连接' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeEnabled())
    fireEvent.change(screen.getByLabelText(/密码/), { target: { value: 'changed-test-only-value' } })
    expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeDisabled()
    expect(screen.getByText(/连接配置已更改，请重新测试/)).toBeInTheDocument()
  })

  it('已添加编号显示复用入口且不再次写入或启动摄像头', async () => {
    const fetchMock = setup(() => json(success), [{ id: 'existing', name: '已有镜头', source_type: 'webcam', source: '0', room_name: '客厅', enabled: true, health: { status: 'ready' } }])
    fireEvent.click(await screen.findByRole('button', { name: '下一步：测试连接' }))
    expect(await screen.findByRole('link', { name: '使用已有摄像头' })).toHaveAttribute('href', '/live?camera=existing')
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '测试连接' })).toBeDisabled()
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
  })

  it('取消测试会中断等待且不能保留连接成功', async () => {
    const fetchMock = setup((init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    }))
    await openConnectionStep()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith('/test'))).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: '取消测试' }))
    expect(await screen.findByText('连接测试已取消，没有判定连接成功。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeDisabled()
  })

  it.each([true, false])('保存向导 enabled=%s 时明确区分保存配置和启动', async (enabled) => {
    const fetchMock = setup()
    await openConnectionStep()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '连接成功，继续' }))
    if (!enabled) fireEvent.click(screen.getByRole('checkbox', { name: /启用摄像头/ }))
    fireEvent.click(screen.getByRole('button', { name: '保存并划分区域' }))
    expect(await screen.findByText('摄像头已添加')).toBeInTheDocument()
    const starts = fetchMock.mock.calls.filter(([path, init]) => String(path).endsWith('/start') && init?.method === 'POST')
    expect(starts).toHaveLength(enabled ? 1 : 0)
    const updates = fetchMock.mock.calls.filter(([path, init]) => String(path) === '/api/cameras/draft-camera' && init?.method === 'PATCH')
    expect(updates).toHaveLength(1)
    expect(JSON.parse(String(updates[0][1]?.body))).toMatchObject({ enabled })
  })

  it.each(['cancel', 'timeout'])('创建摄像头 POST 挂起时 %s 仍可结束等待且阻止盲目重建', async (action) => {
    const fetchMock = setup()
    const ordinaryFetch = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/api/cameras' && init?.method === 'POST') return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      })
      return ordinaryFetch(input, init)
    })
    const timers = vi.spyOn(window, 'setTimeout')
    await openConnectionStep()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path) === '/api/cameras' && init?.method === 'POST')).toBe(true))
    const creation = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/cameras' && init?.method === 'POST')
    expect(creation?.[1]?.signal).toBeInstanceOf(AbortSignal)
    if (action === 'cancel') fireEvent.click(screen.getByRole('button', { name: '取消测试' }))
    else {
      const deadline = timers.mock.calls.find(([, delay]) => delay === 20000)?.[0]
      expect(typeof deadline).toBe('function')
      await act(async () => { if (typeof deadline === 'function') deadline() })
    }
    expect(await screen.findByRole('alert')).toHaveTextContent('创建结果尚未确认')
    expect(screen.queryByRole('button', { name: '取消测试' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '测试连接' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    expect(fetchMock.mock.calls.filter(([path, init]) => String(path) === '/api/cameras' && init?.method === 'POST')).toHaveLength(1)
    expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith('/test'))).toBe(false)
  })

  it('已取得编号后 PATCH 挂起可取消，重试沿用同一编号不再次 POST 创建', async () => {
    const fetchMock = setup()
    await openConnectionStep()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeEnabled())
    const ordinaryFetch = fetchMock.getMockImplementation()!
    let blockPatch = true
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/api/cameras/draft-camera' && init?.method === 'PATCH' && blockPatch) return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      })
      return ordinaryFetch(input, init)
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: /摄像头编号/ }), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: '取消测试' }))
    expect(await screen.findByText('连接测试已取消，没有判定连接成功。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '测试连接' })).toBeEnabled()
    blockPatch = false
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '连接成功，继续' })).toBeEnabled())
    expect(fetchMock.mock.calls.filter(([path, init]) => String(path) === '/api/cameras' && init?.method === 'POST')).toHaveLength(1)
  })
})
