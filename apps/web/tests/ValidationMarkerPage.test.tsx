import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ValidationMarkerPage } from '../src/pages/ValidationMarkerPage'

function json(value: unknown) {
  return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

describe('Mock 组件单测：手机验收 Marker', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('按 run 读取后端识别状态，并用后台分配的数字 ID 生成 Marker', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/items') return json([{ id: 'phone-one', name: '我的手机', type: 'phone', aliases: [], aruco_id: 17, ring_enabled: false }])
      if (path === '/api/acceptance/marker-status?validation_run_id=run-one&item_id=phone-one&camera_id=cam-real') return json({
        item_id: 'phone-one', camera_id: 'cam-real', validation_run_id: 'run-one', aruco_id: 17,
        recognized: false, server_observed: true, runtime_mode: 'REAL',
        source_type: 'opencv_camera', is_simulated: false,
      })
      throw new Error(`unexpected request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<MemoryRouter initialEntries={['/companion/validation-marker?item=phone-one&camera=cam-real&run=run-one']}><ValidationMarkerPage /></MemoryRouter>)

    const marker = await screen.findByAltText('ArUco Marker 17')
    expect(marker).toHaveAttribute('src', '/api/acceptance/markers/17.png?size=1000&v=1')
    expect(await screen.findByText('后端尚未识别')).toBeInTheDocument()
    expect(screen.queryByText('后端摄像头已识别')).not.toBeInTheDocument()
    expect(screen.getByText(/在这台真实手机上全屏显示 Marker/)).toBeInTheDocument()
    expect(screen.getByText(/打印仅作备用/)).toBeInTheDocument()

    expect(screen.getByRole('button', { name: '本轮已锁定绑定' })).toBeDisabled()
  })

  it('没有 run 时不把 item_id 冒充 validation_run_id', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/items') return json([{ id: 'phone-one', name: '我的手机', type: 'phone', aliases: [], aruco_id: 8, ring_enabled: false }])
      if (path === '/api/acceptance/marker-status?item_id=phone-one&camera_id=cam-real') return json({ item_id: 'phone-one', camera_id: 'cam-real', aruco_id: 8, recognized: false, server_observed: true })
      if (path === '/api/acceptance/items/phone-one/marker' && init?.method === 'POST') return json({ item_id: 'phone-one', aruco_id: 8, bound: true, message: '绑定完成' })
      throw new Error(`unexpected request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<MemoryRouter initialEntries={['/companion/validation-marker?item=phone-one&camera=cam-real']}><ValidationMarkerPage /></MemoryRouter>)
    expect(await screen.findByAltText('ArUco Marker 8')).toBeInTheDocument()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/acceptance/marker-status?item_id=phone-one&camera_id=cam-real', expect.any(Object)))
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('validation_run_id=phone-one'))).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '绑定到这个物品' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/acceptance/items/phone-one/marker', expect.objectContaining({ method: 'POST', body: JSON.stringify({}) })))
    expect(await screen.findByText('绑定完成')).toBeInTheDocument()
    expect(screen.queryByText('后端摄像头已识别')).not.toBeInTheDocument()
  })

  it('成功后刷新断网会撤销绿色识别状态，不能沿用旧帧回报', async () => {
    let offline = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/items') return json([{ id: 'phone-one', name: '我的手机', type: 'phone', aliases: [], aruco_id: 8, ring_enabled: false }])
      if (offline) throw new Error('controlled network outage')
      return json({ item_id: 'phone-one', camera_id: 'cam-real', aruco_id: 8, recognized: true, server_observed: true, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, pipeline_status: 'running' })
    }))
    const { container } = render(<MemoryRouter initialEntries={['/companion/validation-marker?item=phone-one&camera=cam-real']}><ValidationMarkerPage /></MemoryRouter>)
    expect(await screen.findByText('后端摄像头已识别')).toBeInTheDocument()
    offline = true
    fireEvent.click(screen.getByRole('button', { name: '立即刷新识别状态' }))
    expect(await screen.findByText(/controlled network outage/)).toBeInTheDocument()
    expect(screen.queryByText('后端摄像头已识别')).not.toBeInTheDocument()
    expect(container.querySelector('.marker-recognition-result')).not.toHaveClass('confirmed')
    expect(screen.getByText('后端尚未识别')).toBeInTheDocument()
  })

  it('更换物品并请求失败时，不沿用上一物品的 Marker 或绑定', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/items') return json([
        { id: 'phone-one', name: '我的手机', type: 'phone', aliases: [], aruco_id: null, ring_enabled: false },
        { id: 'phone-two', name: '另一手机', type: 'phone', aliases: [], aruco_id: null, ring_enabled: false },
      ])
      if (path.includes('phone-two')) throw new Error('second item unavailable')
      if (init?.method === 'POST') return json({ item_id: 'phone-one', aruco_id: 8, bound: true, message: '绑定完成' })
      return json({ item_id: 'phone-one', camera_id: 'cam-real', aruco_id: 8, recognized: true, server_observed: true, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false })
    }))
    render(<MemoryRouter initialEntries={['/companion/validation-marker?item=phone-one&camera=cam-real']}><ValidationMarkerPage /></MemoryRouter>)
    await waitFor(() => expect(screen.getByRole('button', { name: '绑定到这个物品' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '绑定到这个物品' }))
    await screen.findByText('绑定完成')
    await waitFor(() => expect(screen.getByRole('combobox', { name: '这张 Marker 属于' })).toBeEnabled())
    fireEvent.change(screen.getByRole('combobox', { name: '这张 Marker 属于' }), { target: { value: 'phone-two' } })
    expect(await screen.findByText(/second item unavailable/)).toBeInTheDocument()
    expect(screen.queryByAltText('ArUco Marker 8')).not.toBeInTheDocument()
    expect(screen.queryByText('绑定完成')).not.toBeInTheDocument()
    expect(screen.queryByText('后端摄像头已识别')).not.toBeInTheDocument()
  })
})
