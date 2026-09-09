import { BellRing, MapPin, SearchX } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { SearchBox, EmptyState, Loading, PageHeader } from '../components/UI'
import { SearchResultCard } from '../components/SearchResultCard'
import { useToast } from '../components/Toast'
import { api } from '../lib/api'
import { useApi } from '../hooks/useApi'
import { selectLocationEvidence } from '../lib/provenance'
import type { HealthResponse, SearchResult } from '../types'
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
  const missingLocationCount = results.filter(result => !selectLocationEvidence(result, runtime.mode)).length
  const partialLocations = missingLocationCount > 0 && missingLocationCount < results.length
  const needsSetup = searched && !loading && !error && (results.length === 0 || missingLocationCount > 0)
  const availability = useApi<HealthResponse | null>(needsSetup ? '/api/health' : null, null)
  const stats = availability.error ? undefined : availability.data?.stats
  const noItems = stats?.items === 0
  const noCameras = stats?.cameras === 0
  const emptyTitle = noItems ? '还没有添加物品' : '没有找到匹配的物品'
  const emptyDescription = noItems ? '先给要找的东西起个名字、添加照片，物忆才能知道你在找哪一件。' : noCameras ? '当前还没有连接摄像头。也可以换个名称或简称，检查物品是否已添加。' : '试试物品名称或别名；如果还没有添加这件东西，可以先保存它的照片。'


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
      <PageHeader title="你在找什么？" description="查找最后看到的位置和时间；没有可靠记录时，物忆会明确告诉你。" />
      <div className="search-stage"><SearchBox value={query} onChange={setQuery} onSubmit={() => search()} loading={loading} /><div className="suggestions center"><span>常用查询</span>{['我的手机在哪里', '钥匙放哪了', '找一下钱包', '手机最后出现在哪'].map((value) => <button key={value} onClick={() => { setQuery(value); void search(value) }}>{value}</button>)}</div></div>
      {error && <div className="inline-banner danger" role="alert">{error}<button className="button secondary small" onClick={() => void search()}>重新查找</button></div>}
      {refreshError && <p className="inline-banner info" role="status">{refreshError}</p>}
      <section className="search-results" aria-live="polite" aria-busy={loading}>
        {loading && <Loading label="正在寻找物品记录…" />}
        {results.map((result) => <SearchResultCard key={result.item.id} result={result} runtimeMode={runtime.mode} onRing={() => ring(result)} />)}
        {searched && !loading && !error && results.length === 0 && <EmptyState icon={<SearchX />} title={emptyTitle} description={emptyDescription} action={<div className="empty-actions"><Link className="button primary" to="/items?add=1">{noItems ? '添加第一个物品' : '添加要找的物品'}</Link>{noCameras && <Link className="button secondary" to="/cameras?add=1">连接摄像头</Link>}<Link className="button secondary" to="/items">查看已添加物品</Link></div>} />}
        {needsSetup && results.length > 0 && <div className="panel setup-panel"><h2>{partialLocations ? '部分物品还没有位置记录' : noCameras ? '尚未连接摄像头' : '还没有可用的位置记录'}</h2><p>{partialLocations ? '已有的可靠记录仍显示在对应物品卡片中。对尚无记录的物品，请检查摄像头连接、照片档案和现场识别；不会推测它们的位置。' : noCameras ? '物品已经添加，连接摄像头后才能开始留下位置线索。' : '确认物品照片并建立识别档案，再把实物放到镜头前验证。没有证据时不会猜测位置。'}</p><div className="empty-actions"><Link className="button primary" to={noCameras ? '/cameras?add=1' : '/items'}>{noCameras ? '连接摄像头' : '完善照片与测试识别'}</Link><Link className="button secondary" to="/live">查看实时画面</Link></div></div>}
        {!searched && <div className="search-explainer"><div><MapPin /><strong>最新可靠观察优先</strong><span>历史确认放置独立展示</span></div><div><BellRing /><strong>手机还能响铃</strong><span>需先绑定伴侣页面</span></div><div><SearchX /><strong>不确定会直说</strong><span>遮挡不会猜成确定位置</span></div></div>}
      </section>
      <VoiceSearch disabled={loading} evidenceScope={runtime.mode} onQuestion={async question => { setQuery(question); return search(question) }} />
    </div>
  )
}
