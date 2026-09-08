import { BellRing, MapPin, SearchX } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { SearchBox, EmptyState, PageHeader } from '../components/UI'
import { SearchResultCard } from '../components/SearchResultCard'
import { useToast } from '../components/Toast'
import { api } from '../lib/api'
import type { SearchResult } from '../types'
import { useRuntime } from '../contexts/RuntimeContext'
import { reportPollingError, useVisiblePolling } from '../hooks/useVisiblePolling'
import { VoiceSearch } from '../components/VoiceSearch'
import { spokenLocation } from '../lib/spokenLocation'

export function SearchPage() {
  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState(params.get('q') || '')
  const [results, setResults] = useState<SearchResult[]>([])
  const [searched, setSearched] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [refreshError, setRefreshError] = useState('')
  const [searchVersion, setSearchVersion] = useState(0)
  const generation = useRef(0)
  const request = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const { notify } = useToast()
  const runtime = useRuntime()
  const mode = useRef(runtime.mode)
  mode.current = runtime.mode

  async function search(next = query): Promise<string | null> {
    if (!next.trim()) return null
    const current = ++generation.current
    request.current?.abort()
    if (next.trim().length > 200) { setLoading(false); setResults([]); setError('查询最多 200 字，请缩短问题后重试。'); return null }
    const controller = new AbortController(); request.current = controller
    const timeout = window.setTimeout(() => controller.abort('search_timeout'), 8000)
    setLoading(true); setError(''); setRefreshError(''); setResults([]); setSearched(true); setParams({ q: next.trim() })
    try {
      const response = await api<{ query: string; results: SearchResult[] }>('/api/search', { method: 'POST', json: { query: next.trim() }, signal: controller.signal })
      if (mounted.current && current === generation.current && !controller.signal.aborted) { setResults(response.results); setSearchVersion(current); return spokenLocation(response.results, mode.current) }
    } catch (value) { if (mounted.current && current === generation.current) setError(controller.signal.reason === 'search_timeout' ? '查询超时，请重试。' : value instanceof Error ? value.message : '暂时无法查找') }
    finally { window.clearTimeout(timeout); if (mounted.current && current === generation.current) setLoading(false) }
    return null
  }

  useEffect(() => {
    mounted.current = true
    const value = params.get('q'); if (value) { setQuery(value); void search(value) }
    return () => { mounted.current = false; generation.current += 1; request.current?.abort() }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useVisiblePolling(async (signal) => {
    const current = generation.current
    const responses = await Promise.allSettled(results.map((result) => api<SearchResult>(`/api/items/${encodeURIComponent(result.item.id)}/last-location`, { signal })))
    if (!mounted.current || current !== generation.current || (signal.aborted && !reportPollingError(signal))) return
    const updates = new Map<string, SearchResult>()
    responses.forEach((value, index) => { if (value.status === 'fulfilled' && !signal.aborted) updates.set(results[index].item.id, value.value) })
    setResults((previous) => previous.map((result) => updates.get(result.item.id) || result))
    setRefreshError(updates.size !== results.length ? '位置刷新暂时失败，保留上次可靠记录；你输入的查询不会被改变。' : '')
  }, { enabled: !loading && results.length > 0, resetKey: searchVersion })

  async function ring(result: SearchResult) {
    try {
      const response = await api<{ delivered: boolean }>(`/api/items/${result.item.id}/ring`, { method: 'POST' })
      notify(response.delivered ? '响铃指令已送达手机伴侣' : '手机伴侣不在线，请先在手机上打开伴侣页面', response.delivered ? 'success' : 'info')
    } catch (value) { notify(value instanceof Error ? value.message : '响铃发送失败', 'danger') }
  }

  return (
    <div className="search-page">
      <PageHeader eyebrow="寻找物品" title="你在找什么？" description="优先显示最新可靠观察；历史确认放置独立列出，不替代更新的观察或未知状态。" />
      <div className="search-stage"><SearchBox value={query} onChange={setQuery} onSubmit={() => search()} loading={loading} /><div className="suggestions center"><span>常用查询</span>{['我的手机在哪里', '钥匙放哪了', '找一下钱包', '手机最后出现在哪'].map((value) => <button key={value} onClick={() => { setQuery(value); void search(value) }}>{value}</button>)}</div></div>
      <VoiceSearch disabled={loading} evidenceScope={runtime.mode} onQuestion={async question => { setQuery(question); return search(question) }} />
      {error && <div className="inline-banner danger">{error}</div>}
      {refreshError && <p className="inline-banner info" role="status">{refreshError}</p>}
      <section className="search-results" aria-live="polite">
        {results.map((result) => <SearchResultCard key={result.item.id} result={result} runtimeMode={runtime.mode} onRing={() => ring(result)} />)}
        {searched && !loading && !error && results.length === 0 && <EmptyState icon={<SearchX />} title="没有找到匹配的物品" description={runtime.mode === 'REAL' ? '暂时没有真实摄像头产生的位置记录。' : '可以换个简称试试，或先在当前隔离模式的物品管理中添加它。'} />}
        {!searched && <div className="search-explainer"><div><MapPin /><strong>最新可靠观察优先</strong><span>历史确认放置独立展示</span></div><div><BellRing /><strong>手机还能响铃</strong><span>需先绑定伴侣页面</span></div><div><SearchX /><strong>不确定会直说</strong><span>遮挡不会猜成确定位置</span></div></div>}
      </section>
    </div>
  )
}
