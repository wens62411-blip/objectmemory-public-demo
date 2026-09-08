import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../src/components/Toast'
import { RuntimeProvider } from '../src/contexts/RuntimeContext'
import { StoragePage } from '../src/pages/StoragePage'

const event = {
  id: 'event-one', event_id: 'event-one', item_id: 'keys', item_name: '钥匙', event_type: 'movement', camera_id: 'camera-one', camera_name: '客厅摄像头', room_name: '客厅',
  from_zone: '桌面', to_zone: '门口', timestamp_start: '2026-09-04T10:00:00Z', confidence: .92, detection_mode: 'aruco', evidence_status: 'confirmed', final_status: 'confirmed_placed',
  runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, pinned: false,
}

function json(value: unknown) {
  return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

describe('Mock 组件单测：存储管理 API 契约', () => {
  beforeEach(() => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/runtime-config') return json({ runtime_mode: 'REAL', mode: 'REAL', is_simulated: false })
      if (path === '/api/storage/status') return json({ runtime_mode: 'REAL', database_size: 1024 ** 2, screenshot_size: 512, clip_size: 2048, logs_size: 128, firmware_artifacts_size: 256, total_managed_bytes: 2 * 1024 ** 2, max_storage_mb: 500, events_by_item: { keys: 1 }, pinned_events: 0, orphan_files: 0, retention_policy: 'MINIMAL' })
      if (path === '/api/events?limit=100') return json([event])
      if (path === '/api/storage/policy' && init?.method === 'PATCH') return json({ retention_policy: 'BALANCED' })
      if (path === '/api/events/event-one/pin') return json({ ...event, pinned: true })
      if (path === '/api/storage/delete-real') return json({ deleted_events: 1, message: '真实记录已删除' })
      return json({ message: '完成' })
    }))
  })
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('显示真实后端统计，并使用独立端点切换策略、固定事件和删除 REAL 数据', async () => {
    render(<MemoryRouter><ToastProvider><RuntimeProvider><StoragePage /></RuntimeProvider></ToastProvider></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: '存储管理' })).toBeInTheDocument()
    expect(screen.getByText('1.0 MB', { exact: true })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: /BALANCED/ }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/storage/policy', expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ policy: 'BALANCED' }) })))

    fireEvent.click(screen.getByRole('button', { name: '固定事件' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/events/event-one/pin', expect.objectContaining({ method: 'POST', body: JSON.stringify({ pinned: true }) })))

    fireEvent.change(screen.getByPlaceholderText('DELETE REAL DATA'), { target: { value: 'DELETE REAL DATA' } })
    fireEvent.click(screen.getByRole('button', { name: '永久删除所有真实记录' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/storage/delete-real', expect.objectContaining({ method: 'POST', body: JSON.stringify({ confirmation: 'DELETE REAL DATA' }) })))
  })
})
