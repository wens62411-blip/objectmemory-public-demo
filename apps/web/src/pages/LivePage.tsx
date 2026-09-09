import { Activity, Camera as CameraIcon, CircleStop, Eye, Hand, Map, Play, Settings2, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { VisionFrame } from '../components/VisionFrame'
import { HandActionsCard } from '../components/HandActionsCard'
import { BrowserCamera } from '../components/BrowserCamera'
import { Badge, EmptyState, ErrorState, Loading, PageHeader } from '../components/UI'
import { useApi, usePolling } from '../hooks/useApi'
import { api } from '../lib/api'
import { eventLabel, formatTime, sourceLabels, statusTone } from '../lib/format'
import type { Camera, EventRecord, Item, Settings, Zone } from '../types'
import { useToast } from '../components/Toast'
import { EventProvenance, SourceBadge } from '../components/ProvenanceBadge'
import { useRuntime } from '../contexts/RuntimeContext'
import { sourceCategory } from '../lib/provenance'
import { resolveDetectionMode } from '../lib/runtime'

const defaultSettings: Settings = { detection_mode: 'aruco', inference_fps: 5, static_seconds: 2, pre_seconds: 5, post_seconds: 5, retention_days: 7, save_clips: true, hand_detection_enabled: true, show_hands: true, privacy_mode: true, record_events: true }

export function LivePage() {
  const [params, setParams] = useSearchParams()
  const cameras = usePolling<Camera[]>('/api/cameras', [], 4000)
  const settings = useApi<Settings>('/api/settings', defaultSettings)
  const [showZones, setShowZones] = useState(true)
  const items = useApi<Item[]>('/api/items', [])
  const [busy, setBusy] = useState(false)
  const { notify } = useToast()
  const runtime = useRuntime()
  const selectedId = params.get('camera') || cameras.data[0]?.id
  const selected = cameras.data.find((camera) => camera.id === selectedId) || cameras.data[0]
  const zones = useApi<Zone[]>(selected ? `/api/cameras/${selected.id}/zones` : null, [])
  const events = usePolling<EventRecord[]>(selected ? `/api/events?camera_id=${encodeURIComponent(selected.id)}&limit=8` : null, [], 5000)

  useEffect(() => {
    if (selected && params.get('camera') !== selected.id) setParams({ camera: selected.id }, { replace: true })
  }, [selected?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const health = selected?.health
  const running = ['ready', 'running', 'online', 'streaming'].includes((health?.status || '').toLowerCase())
  const currentState = events.data[0]
  const detectionMode = resolveDetectionMode(health?.detection_mode, settings.data.detection_mode, !settings.loading && !settings.error)

  async function toggleCamera() {
    if (!selected) return
    setBusy(true)
    try {
      await api(`/api/cameras/${selected.id}/${running ? 'stop' : 'start'}`, { method: 'POST' })
      notify(running ? '摄像头已停止' : '摄像头正在连接', 'success'); await cameras.reload()
    } catch (value) { notify(value instanceof Error ? value.message : '操作失败', 'danger') }
    finally { setBusy(false) }
  }

  async function toggleHands() {
    try {
      const updated = await api<Settings>('/api/settings', { method: 'PATCH', json: { show_hands: !settings.data.show_hands } })
      settings.setData(updated)
    } catch (value) { notify(value instanceof Error ? value.message : '设置失败', 'danger') }
  }

  return (
    <div>
      <PageHeader title="看见，也记得发生过什么" description="一个连续画面，同帧显示物品位置；只有通过真实证据检查才保存记录。" actions={selected && <button className={`button ${running ? 'secondary' : 'primary'}`} disabled={busy} onClick={toggleCamera}>{running ? <><CircleStop />停止</> : <><Play />启动摄像头</>}</button>} />
      {cameras.loading && cameras.data.length === 0 ? <Loading label="正在读取摄像头…" /> : cameras.error ? <ErrorState message={cameras.error} onRetry={cameras.reload} /> : cameras.data.length === 0 ? <EmptyState icon={<CameraIcon />} title="先添加一个摄像头" description="添加电脑、浏览器或网络摄像头；测试回放仅在 DEMO 模式可用。" action={<Link className="button primary" to="/cameras?add=1">添加摄像头</Link>} /> : selected && (
        <>
          <div className="live-toolbar panel">
            <label className="camera-select"><span>当前画面</span><select value={selected.id} onChange={(event) => setParams({ camera: event.target.value })}>{cameras.data.map((camera) => <option value={camera.id} key={camera.id}>{camera.room_name} · {camera.name}</option>)}</select></label>
            <div className="view-toggles">
              <button className={showZones ? 'active' : ''} aria-pressed={showZones} onClick={() => setShowZones((value) => !value)}><Map />区域</button>
              <button className={settings.data.show_hands ? 'active' : ''} aria-pressed={settings.data.show_hands} disabled={settings.loading || Boolean(settings.error)} onClick={toggleHands}><Hand />手部关键点</button>
            </div>
            <a className="button secondary small live-recognition-jump" href="#live-recognition">查看物品识别</a>
            <Link className="button ghost small" to={`/cameras/${selected.id}/zones`}><Settings2 />编辑区域</Link>
          </div>
          <div className="live-layout live-workspace">
            <section>
              <div className="live-view-single">
              {['browser', 'browser_camera'].includes(selected.source_type) && <><BrowserCamera key={`capture:${selected.id}`} cameraId={selected.id} targetFps={selected.inference_fps || 4} showPreview={false} /><p className="feed-help">浏览器保留本地采集和上传；当前上传上限为 8 FPS，未按 30 FPS 回传优化。</p></>}
              <div id="live-recognition" className="live-recognition-pane" tabIndex={-1}>
              <VisionFrame key={`vision:${selected.id}`} cameraId={selected.id} camera={selected} primary mode={detectionMode} zones={zones.data} showZones={showZones} active={running} showHands={settings.data.show_hands} registeredItems={items.data} />
              </div>
              <p className="feed-help"><Eye />画面来自当前所示来源；测试视频和虚拟设备不计为真实家庭摄像头证据。</p>
              </div>
              <HandActionsCard key={`hands:${selected.id}`} cameraId={selected.id} enabled={settings.data.hand_detection_enabled !== false} settingsKnown={!settings.loading && !settings.error} cameraRunning={running} />
            </section>
            <aside className="live-inspector panel">
              <div className="inspector-head"><div><p className="eyebrow">画面状态</p><h2>{selected.room_name}</h2></div><Badge tone={statusTone(health?.status) as 'success' | 'warning' | 'danger' | 'neutral'}>{health?.status === 'ready' ? '视频正常' : health?.status || '未启动'}</Badge></div>
              <dl className="health-grid">
                <div><dt>来源</dt><dd>{sourceLabels[selected.source_type] || selected.source_type} <SourceBadge record={selected} /></dd></div><div><dt>分辨率</dt><dd>{health?.width && health?.height ? `${health.width} × ${health.height}` : '等待画面'}</dd></div><div><dt>采集帧率</dt><dd>{health?.capture_fps?.toFixed(1) ?? '—'} FPS</dd></div><div><dt>画面帧率</dt><dd>{health?.preview_fps?.toFixed(1) ?? '—'} FPS</dd></div><div><dt>识别帧率</dt><dd>{(health?.inference_fps ?? health?.fps)?.toFixed(1) ?? '—'} FPS</dd></div><div><dt>识别延迟</dt><dd>{health?.latency_ms !== undefined ? `${Math.round(health.latency_ms)} ms` : '—'}</dd></div><div><dt>丢弃过时待识别帧</dt><dd>{health?.dropped_frames ?? 0}</dd></div><div><dt>重连</dt><dd>{health?.reconnects ?? 0}</dd></div>
              </dl>
              {health?.error && <div className="diagnostic"><TriangleAlert /><div><strong>连接提示</strong><span>{health.error}</span></div></div>}
              <div className="current-evidence"><p className="eyebrow">最近事件证据（非当前状态表）</p>{currentState ? <><strong>{currentState.item_name}</strong><span>{eventLabel(currentState)} · {[currentState.room_name, currentState.to_zone || currentState.zone_name].filter(Boolean).join(' · ')}</span><EventProvenance event={currentState} compact /><time>{formatTime(currentState.timestamp_start)}</time></> : <p>{runtime.mode === 'REAL' ? '暂时没有真实摄像头产生的位置记录。' : '当前来源还没有产生事件。'}</p>}</div>
            </aside>
          </div>
          <section className="panel live-events"><div className="section-heading"><div><p className="eyebrow">实时证据流</p><h2>最近事件</h2></div><Link to={`/events?camera_id=${selected.id}`}>查看全部</Link></div>{events.data.length ? <div className="event-chips">{events.data.map((event) => <div key={event.id}><Activity /><strong>{event.item_name}</strong><span>{eventLabel(event)} · {event.to_zone || event.zone_name || '未定义区域'}</span><EventProvenance event={event} compact /><time>{formatTime(event.timestamp_start)}</time></div>)}</div> : <p className="muted-block">{runtime.mode === 'REAL' && sourceCategory(selected) === 'real' ? '暂时没有真实摄像头产生的位置记录。' : '当前来源尚未产生满足证据门的事件。'}</p>}</section>
        </>
      )}
    </div>
  )
}
