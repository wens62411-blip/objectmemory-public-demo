import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../src/lib/api'

describe('API 客户端', () => {
  afterEach(() => vi.restoreAllMocks())
  it('所有请求都携带同源会话凭据', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await api('/api/health')
    expect(fetchMock).toHaveBeenCalledWith('/api/health', expect.objectContaining({ credentials: 'same-origin' }))
  })
  it('把后端中文 detail 传给用户', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: '串口没有检测到开发板' }), { status: 422, headers: { 'Content-Type': 'application/json' } }))
    await expect(api('/api/firmware/flash', { method: 'POST', json: { port: 'COM5' } })).rejects.toEqual(expect.objectContaining({ message: '串口没有检测到开发板', status: 422 }))
  })
  it('把网络断开与摄像头失败明确区分', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(api('/api/cameras/one/test')).rejects.toEqual(expect.objectContaining({ message: expect.stringContaining('后端未连接'), status: 0 }))
  })
})
