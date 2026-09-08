import { ArrowRight, Camera, CheckCircle2, Clock3, Cpu, PackageSearch, Play, Plus, Radio, ScanLine, Sparkles, Video } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useApi, usePolling } from '../hooks/useApi'
import { api } from '../lib/api'
import { eventLabel, formatTime, sourceLabels, statusTone } from '../lib/format'
import type { Camera as CameraType, EventRecord, Item } from '../types'
import { useHealth } from '../components/AppShell'
import { Badge, EmptyState, ErrorState, Loading, SearchBox } from '../components/UI'
import { useToast } from '../components/Toast'
import { EventProvenance, SourceBadge } from '../components/ProvenanceBadge'
import { useRuntime } from '../contexts/RuntimeContext'
import { evidenceBadge } from '../lib/provenance'

export function DashboardPage() {
  const [query, setQuery] = useState('')
  const [seeding, setSeeding] = useState(false)
  const navigate = useNavigate()
  const { notify } = useToast()
  const health = useHealth()
  const cameras = useApi<CameraType[]>('/api/cameras', [])
  const items = useApi<Item[]>('/api/items', [])
  const events = useApi<EventRecord[]>('/api/events?limit=6', [])
  const runtime = useRuntime()
  const firmware = usePolling<{ compile_passed?: boolean; physical_flash_performed?: boolean }>(runtime.mode === 'DEMO' ? '/api/firmware/status' : null, {}, 10000)
  const virtual = usePolling<{ running?: boolean; backend_online?: boolean; source?: string }>(runtime.mode === 'DEMO' ? '/api/virtual-device/status' : null, {}, 5000)
  const stats = health.data?.stats || {}
  const demoMode = runtime.mode === 'DEMO'

  async function seedDemo() {
    if (!demoMode) return notify('演示种子只允许在 DEMO 模式创建。', 'danger')
    setSeeding(true)
    try {
      const result = await api<{ message?: string }>('/api/system/demo-seed', { method: 'POST' })
      notify(result.message || '演示物品和测试回放已准备好', 'success')
      await Promise.all([cameras.reload(), items.reload(), events.reload(), health.reload()])
    } catch (value) { notify(value instanceof Error ? value.message : '演示数据生成失败', 'danger') }
    finally { setSeeding(false) }
  }

  function search() { if (query.trim()) navigate(`/search?q=${encodeURIComponent(query.trim())}`) }

  return (
    <div className="dashboard-page">
      <section className="hero-panel">
        <div className="hero-copy"><div className="hero-kicker"><Sparkles />让摄像头拥有物品记忆</div><h1>东西放哪了，<br /><em>问一句就知道。</em></h1><p>物忆自动记录手机、钥匙和钱包最后一次被看到、拿起或放下的位置。</p></div>
        <SearchBox value={query} onChange={setQuery} onSubmit={search} placeholder="你在找什么？例如：我的手机在哪里" />
        <div className="suggestions"><span>试着问</span>{['我的手机在哪里', '钥匙放哪了', '钱包最后出现在哪'].map((text) => <button key={text} onClick={() => { setQuery(text); navigate(`/search?q=${encodeURIComponent(text)}`) }}>{text}</button>)}</div>
        <div className="privacy-line"><span className="status-dot online" />本地处理 · 家庭视频不会上传</div>
      </section>

      {demoMode && <section className="no-hardware-mode panel" aria-label="无硬件演示状态">
        <div className="no-hardware-title"><Cpu /><div><p className="eyebrow">现场演示状态</p><h2>演示模式，当前事件不来自真实家庭摄像头</h2></div><Badge tone="warning">DEMO · 模拟数据隔离</Badge></div>
        <div className="no-hardware-facts"><div><Video /><span>视频来源</span><strong>测试视频或电脑摄像头</strong><small>当前：{virtual.data.source === 'webcam' ? '电脑摄像头' : '测试视频'}</small></div><div><Radio /><span>ESP32 状态</span><strong>虚拟设备</strong><small>{virtual.data.backend_online ? '已按真实协议在线' : virtual.data.running ? '正在注册或等待视频' : '尚未启动'}</small></div><div><CheckCircle2 /><span>固件状态</span><strong>{firmware.data.compile_passed ? firmware.data.physical_flash_performed ? '已编译，有历史真机刷写记录' : '已编译，未真机刷写' : '尚未完成有效编译'}</strong><small>{firmware.data.physical_flash_performed ? '检测到历史真机刷写证据' : '没有物理刷写记录'}</small></div></div>
        <Link className="button secondary small" to="/devices">打开设备中心查看证据 <ArrowRight /></Link>
      </section>}

      <section className="metric-grid" aria-label="系统概览">
        <article><div className="metric-icon green"><Camera /></div><div><span>在线摄像头</span><strong>{stats.online_cameras ?? cameras.data.filter((item) => item.health?.status === 'ready').length}<small> / {stats.cameras ?? cameras.data.length}</small></strong></div><Link to="/live">查看画面 <ArrowRight /></Link></article>
        <article><div className="metric-icon blue"><PackageSearch /></div><div><span>已注册物品</span><strong>{stats.items ?? items.data.length}</strong></div><Link to="/items">管理物品 <ArrowRight /></Link></article>
        <article><div className="metric-icon coral"><Clock3 /></div><div><span>今日新事件</span><strong>{stats.today_events ?? 0}</strong></div><Link to="/events">查看时间线 <ArrowRight /></Link></article>
        <article><div className="metric-icon yellow"><Radio /></div><div><span>物品识别速度</span><strong>{stats.fps !== undefined ? Number(stats.fps).toFixed(1) : '—'}<small> FPS</small></strong></div><Link to="/live">画面帧率单独查看 <ArrowRight /></Link></article>
      </section>

      <div className="dashboard-columns">
        <section className="panel recent-panel">
          <div className="section-heading"><div><p className="eyebrow">物品动态</p><h2>最近发生了什么</h2></div><Link to="/events">全部记录 <ArrowRight /></Link></div>
          {events.loading ? <Loading /> : events.error ? <ErrorState message={events.error} onRetry={events.reload} /> : events.data.length === 0 ? (
            <EmptyState icon={<ScanLine />} title="还没有物品事件" description={runtime.mode === 'REAL' ? '暂时没有真实摄像头产生的位置记录。' : demoMode ? '当前演示库还没有测试视频、虚拟设备或演示种子事件。' : '运行模式尚未确认，事件入口已关闭。'} action={<div className="empty-actions">{demoMode && <button className="button primary" disabled={seeding} onClick={seedDemo}><Play />{seeding ? '正在准备…' : '准备演示闭环'}</button>}<Link className="button secondary" to="/cameras"><Plus />添加摄像头</Link></div>} />
          ) : <div className="event-list">{events.data.map((event) => {
            const evidence = evidenceBadge(event)
            return <Link to={`/events?item_id=${event.item_id}`} className="event-row" key={event.id}>
            <div className={`event-symbol evidence-${evidence.tone}`}>{evidence.tone === 'success' || evidence.tone === 'blue' ? <PackageSearch /> : <Clock3 />}</div>
            <div className="event-main"><strong>{event.item_name} · {eventLabel(event)}</strong><span>{[event.room_name, event.to_zone || event.zone_name || event.new_zone].filter(Boolean).join(' · ') || '位置未确认'}</span><EventProvenance event={event} compact /></div>
            <div className="event-tail"><Badge tone={evidence.tone}>{Math.round(event.confidence * (event.confidence <= 1 ? 100 : 1))}%</Badge><time>{formatTime(event.timestamp_start)}</time></div>
          </Link>})}</div>}
        </section>

        <section className="panel cameras-summary">
          <div className="section-heading"><div><p className="eyebrow">空间状态</p><h2>摄像头</h2></div><Link to="/cameras"><Plus />添加</Link></div>
          {cameras.loading ? <Loading /> : cameras.error ? <ErrorState message={cameras.error} onRetry={cameras.reload} /> : cameras.data.length === 0 ? <EmptyState icon={<Camera />} title="还没有摄像头" description={demoMode ? 'DEMO 模式可添加测试视频；REAL 模式只接受真实视频源。' : '添加电脑、浏览器或网络摄像头。'} action={<Link className="button primary small" to="/cameras?add=1">添加摄像头</Link>} /> : <div className="camera-mini-list">{cameras.data.slice(0, 4).map((camera) => <Link to={`/live?camera=${camera.id}`} key={camera.id}><span className={`status-dot ${camera.health?.status === 'ready' ? 'online' : ''}`} /><div><strong>{camera.name}</strong><span>{camera.room_name} · {sourceLabels[camera.source_type] || camera.source_type}</span><SourceBadge record={camera} /></div><Badge tone={statusTone(camera.health?.status) as 'success' | 'warning' | 'danger' | 'neutral'}>{camera.health?.status === 'ready' ? '在线' : camera.health?.status || '未启动'}</Badge></Link>)}</div>}
        </section>
      </div>
    </div>
  )
}
