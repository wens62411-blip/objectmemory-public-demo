import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const unauthorized = () => json({ detail: '会话已失效' }, 401)

describe('Mock 会话恢复契约：不是实际服务重启证明', () => {
  beforeEach(() => vi.resetModules())
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('同源读取401时只重建一次会话，然后重新读取原地址', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(unauthorized())
      .mockResolvedValueOnce(json({ authenticated: true, local: true }))
      .mockResolvedValueOnce(json({ runtime_mode: 'REAL' }))
    const { api } = await import('../src/lib/api')
    await expect(api('/api/runtime-config')).resolves.toEqual({ runtime_mode: 'REAL' })
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/runtime-config', '/api/session', '/api/runtime-config'])
    expect(fetcher.mock.calls.every(([, init]) => init?.credentials === 'same-origin')).toBe(true)
  })

  it('并发和迟到的旧401共享一个恢复，不制造刷新风暴', async () => {
    let resolveSession!: (response: Response) => void, resolveLate!: (response: Response) => void
    const counts: Record<string, number> = {}
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const path = String(input); counts[path] = (counts[path] || 0) + 1
      if (path === '/api/session') return new Promise<Response>(resolve => { resolveSession = resolve })
      if (path === '/api/late' && counts[path] === 1) return new Promise<Response>(resolve => { resolveLate = resolve })
      return counts[path] === 1 ? unauthorized() : json({ ok: true })
    })
    const { api } = await import('../src/lib/api')
    const first = api('/api/first'), second = api('/api/second'), late = api('/api/late')
    await vi.waitFor(() => expect(counts['/api/session']).toBe(1))
    resolveSession(json({ authenticated: true }))
    await expect(Promise.all([first,second])).resolves.toEqual([{ok:true},{ok:true}])
    resolveLate(unauthorized())
    await expect(late).resolves.toEqual({ok:true})
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1)
  })

  it('LAN会话需访问码时不配对、不重复探测，显式配对后才放行', async () => {
    let paired = false
    const expired = vi.fn(); window.addEventListener('objectmemory:session-required', expired)
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (input === '/api/session') {
        if (init?.method === 'POST') { paired = true; return json({authenticated:true}) }
        return json({authenticated:false,local:false})
      }
      return paired ? json({ok:true}) : unauthorized()
    })
    try {
      const { api } = await import('../src/lib/api')
      await expect(api('/api/items')).rejects.toMatchObject({status:401})
      await expect(api('/api/items')).rejects.toMatchObject({status:401})
      expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1)
      expect(fetcher.mock.calls.some(([,init]) => init?.method === 'POST')).toBe(false)
      expect(expired).toHaveBeenCalledTimes(1)
      await api('/api/session', {method:'POST',json:{pin:'12345678'}})
      await expect(api('/api/items')).resolves.toEqual({ok:true})
    } finally { window.removeEventListener('objectmemory:session-required', expired) }
  })

  it.each(['POST','PATCH','DELETE'])('%s失效不会重放业务写入或自动提交访问码', async method => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(unauthorized())
    const { api } = await import('../src/lib/api')
    await expect(api('/api/firmware/flash',{method,json:{port:'COM5'}})).rejects.toMatchObject({status:401})
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('会话接口自身或第三方地址的401不会进入恢复递归', async () => {
    const fetcher = vi.spyOn(globalThis,'fetch').mockResolvedValue(unauthorized())
    const { api } = await import('../src/lib/api')
    await expect(api('/api/session')).rejects.toMatchObject({status:401})
    await expect(api('https://example.invalid/api/items')).rejects.toMatchObject({status:401})
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('恢复后仍401时停止重试，并要求显式重新连接', async () => {
    const fetcher = vi.spyOn(globalThis,'fetch').mockImplementation(async input => input === '/api/session' ? json({authenticated:true}) : unauthorized())
    const { api } = await import('../src/lib/api')
    await expect(api('/api/items')).rejects.toMatchObject({status:401})
    await expect(api('/api/items')).rejects.toMatchObject({status:401})
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/items')).toHaveLength(3)
  })

  it('调用方在共享恢复中取消，不再重放其读取', async () => {
    const controller = new AbortController()
    const fetcher = vi.spyOn(globalThis,'fetch').mockResolvedValueOnce(unauthorized()).mockImplementationOnce(async () => {
      controller.abort(); return json({authenticated:true})
    })
    const { api } = await import('../src/lib/api')
    await expect(api('/api/items',{signal:controller.signal})).rejects.toMatchObject({status:401})
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('临时网络失败有冷却但不永久锁死，后续正常轮询可恢复', async () => {
    vi.useFakeTimers(); vi.setSystemTime(10000)
    let online = false, authorized = false
    const fetcher = vi.spyOn(globalThis,'fetch').mockImplementation(async input => {
      if (input === '/api/session') {
        if (!online) throw new TypeError('offline')
        authorized = true; return json({authenticated:true})
      }
      return authorized ? json({ok:true}) : unauthorized()
    })
    const { api } = await import('../src/lib/api')
    await expect(api('/api/items')).rejects.toMatchObject({status:401})
    await expect(api('/api/items')).rejects.toMatchObject({status:401})
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1)
    online = true; vi.setSystemTime(16000)
    await expect(api('/api/items')).resolves.toEqual({ok:true})
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(2)
  })

  it('权限403不是登录过期，不自动重新授权', async () => {
    const fetcher = vi.spyOn(globalThis,'fetch').mockResolvedValue(json({detail:'禁止访问'},403))
    const { api } = await import('../src/lib/api')
    await expect(api('/api/firmware/devices')).rejects.toMatchObject({status:403})
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
