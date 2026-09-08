import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SearchPage } from '../src/pages/SearchPage'
import type { SearchResult } from '../src/types'

const phone = { id: 'phone-search', name: '主力手机', type: '手机', aliases: [], aruco_id: null, ring_enabled: false }
const result = (message: string): SearchResult => ({ item: phone, status: 'unknown', answer: '', last_confirmed: null, last_seen: null, last_picked_up: null, last_occluded: null, last_exited: null, evidence: null, observation_hint: { code: 'not_observed', message } })
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })

describe('搜索刷新Mock契约，不证明实物识别', () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('8千字手工查询在前端拒绝，不发送超长请求或写入长URL', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    render(<MemoryRouter><SearchPage /></MemoryRouter>)
    fireEvent.change(screen.getByRole('textbox', { name: '寻找物品' }), { target: { value: '物'.repeat(8000) } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '帮我找' })))
    expect(fetch).not.toHaveBeenCalled()
    expect(screen.getByText('查询最多 200 字，请缩短问题后重试。')).toBeInTheDocument()
  })

  it('自动GET更新现有结果，不重复POST搜索或覆盖正在编辑的输入；失败保留结果', async () => {
    vi.useFakeTimers()
    let fail = false
    const requests = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/search' && init?.method === 'POST') return json({ results: [result('原始观察提示')] })
      if (input === '/api/items/phone-search/last-location' && !init?.method) {
        if (fail) throw new Error('offline')
        return json(result('更新后的观察提示'))
      }
      throw new Error(`unexpected request ${input}`)
    })
    vi.stubGlobal('fetch', requests)
    await act(async () => { render(<MemoryRouter initialEntries={['/search?q=手机']}><SearchPage /></MemoryRouter>) })
    expect(screen.getByText('原始观察提示')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: '寻找物品' }), { target: { value: '正在输入钥匙' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.getByText('更新后的观察提示')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '寻找物品' })).toHaveValue('正在输入钥匙')
    expect(requests.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    fail = true
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.getByText('更新后的观察提示')).toBeInTheDocument()
    expect(screen.getByText(/位置刷新暂时失败，保留上次可靠记录/)).toBeInTheDocument()
  })

  it('新查询完成后旧位置GET晚到不会覆盖新结果', async () => {
    vi.useFakeTimers()
    let finishOld!: (value: Response) => void
    let searches = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/search' && init?.method === 'POST') return Promise.resolve(json({ results: [result(++searches === 1 ? '第一查询结果' : '第二查询结果')] }))
      if (input === '/api/items/phone-search/last-location') return new Promise<Response>((resolve) => { finishOld = resolve })
      throw new Error(`unexpected request ${input}`)
    }))
    await act(async () => { render(<MemoryRouter initialEntries={['/search?q=手机']}><SearchPage /></MemoryRouter>) })
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    fireEvent.change(screen.getByRole('textbox', { name: '寻找物品' }), { target: { value: '新的查询' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '帮我找' })) })
    expect(screen.getByText('第二查询结果')).toBeInTheDocument()
    await act(async () => { finishOld(json(result('晚到的旧位置'))) })
    expect(screen.getByText('第二查询结果')).toBeInTheDocument()
    expect(screen.queryByText('晚到的旧位置')).not.toBeInTheDocument()
  })
})
