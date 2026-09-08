import './PhysicalAcceptance.css'
import {
  AlertTriangle,
  Camera,
  CheckCircle2,
  ChevronLeft,
  Eye,
  Maximize2,
  MonitorSmartphone,
  RefreshCw,
  ShieldCheck,
  Smartphone,
  Sun,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { RuntimeModeBanner } from '../components/RuntimeModeBanner'
import { Brand } from '../components/Brand'
import { useRuntime } from '../contexts/RuntimeContext'
import { acceptanceApi, api } from '../lib/api'
import type { AcceptanceMarkerBinding, AcceptanceMarkerStatus, Item } from '../types'

interface WakeLockHandle {
  release: () => Promise<void>
}

function statusRecognized(status: AcceptanceMarkerStatus | null, itemId: string, cameraId: string, validationRunId: string) {
  return Boolean(
    itemId
    && cameraId
    && status?.server_observed === true
    && status.item_id === itemId
    && status.camera_id === cameraId
    && (!status.validation_run_id || status.validation_run_id === validationRunId)
    && String(status.runtime_mode || '').toUpperCase() === 'REAL'
    && String(status.source_type || '').toLowerCase() === 'opencv_camera'
    && status.is_simulated === false
    && status.recognized === true,
  )
}

function readableTime(value?: string | null) {
  if (!value) return '还没有来自摄像头的识别时间'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false })
}

export function ValidationMarkerPage() {
  const runtime = useRuntime()
  const [params, setParams] = useSearchParams()
  const validationRunId = params.get('run') || params.get('validation_run_id') || ''
  const cameraId = params.get('camera') || ''
  const [items, setItems] = useState<Item[]>([])
  const [itemId, setItemId] = useState(params.get('item') || '')
  const [status, setStatus] = useState<AcceptanceMarkerStatus | null>(null)
  const [binding, setBinding] = useState<AcceptanceMarkerBinding | null>(null)
  const [loadingItems, setLoadingItems] = useState(true)
  const [statusError, setStatusError] = useState('')
  const [bindingBusy, setBindingBusy] = useState(false)
  const [bindingMessage, setBindingMessage] = useState('')
  const [markerNonce, setMarkerNonce] = useState(1)
  const [fullscreen, setFullscreen] = useState(false)
  const [wakeState, setWakeState] = useState<'idle' | 'active' | 'unsupported' | 'failed'>('idle')
  const markerPageRef = useRef<HTMLElement>(null)
  const wakeLockRef = useRef<WakeLockHandle | null>(null)
  const statusRequestRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    api<Item[]>('/api/items')
      .then((value) => {
        if (cancelled) return
        setItems(value)
        if (!itemId) {
          const phone = value.find((item) => /手机|phone/i.test(`${item.name} ${item.type} ${item.aliases?.join(' ') || ''}`)) || value[0]
          if (phone) setItemId(phone.id)
        }
      })
      .catch((error) => { if (!cancelled) setStatusError(error instanceof Error ? error.message : '暂时无法读取物品') })
      .finally(() => { if (!cancelled) setLoadingItems(false) })
    return () => { cancelled = true }
  }, []) // The initial URL selection is intentionally read only once.

  useEffect(() => {
    if (!itemId) return
    const next = new URLSearchParams(params)
    next.set('item', itemId)
    setParams(next, { replace: true })
  }, [itemId, setParams])

  const refreshStatus = useCallback(async () => {
    const request = ++statusRequestRef.current
    if (!itemId || !cameraId) { setStatus(null); setStatusError(''); return }
    try {
      const next = await acceptanceApi.markerStatus({
        validationRunId: validationRunId || undefined,
        itemId,
        cameraId: cameraId || undefined,
      })
      if (request !== statusRequestRef.current) return
      setStatus(next)
      setStatusError('')
    } catch (error) {
      if (request !== statusRequestRef.current) return
      setStatus(null)
      setStatusError(error instanceof Error ? error.message : '后端识别状态暂时不可用')
    }
  }, [cameraId, itemId, validationRunId])

  useEffect(() => {
    if (!itemId || !cameraId) { setStatus(null); return }
    setStatus(null)
    void refreshStatus()
    const timer = window.setInterval(() => void refreshStatus(), 1500)
    return () => { window.clearInterval(timer); statusRequestRef.current += 1 }
  }, [cameraId, itemId, refreshStatus])

  useEffect(() => { setBinding(null); setBindingMessage('') }, [itemId])

  useEffect(() => {
    const changed = () => setFullscreen(Boolean(document.fullscreenElement))
    document.addEventListener('fullscreenchange', changed)
    return () => document.removeEventListener('fullscreenchange', changed)
  }, [])

  useEffect(() => () => { void wakeLockRef.current?.release() }, [])

  const selected = useMemo(() => items.find((item) => item.id === itemId), [itemId, items])
  const matchingStatus = status?.item_id === itemId && status.camera_id === cameraId ? status : null
  const matchingBinding = binding?.item_id === itemId ? binding : null
  const markerId = matchingStatus?.marker_id ?? matchingStatus?.aruco_id ?? matchingBinding?.marker_id ?? matchingBinding?.aruco_id ?? selected?.aruco_id ?? null
  const recognized = statusRecognized(status, itemId, cameraId, validationRunId)
  const pipelineStatus = status?.pipeline_status || status?.recognition_status || 'waiting'
  const pipelineOnline = ['streaming', 'running', 'ready', 'online'].includes(pipelineStatus.toLowerCase())

  async function bind() {
    if (!itemId) return
    statusRequestRef.current += 1
    setBindingBusy(true); setBinding(null); setStatus(null); setBindingMessage('')
    try {
      const next = await acceptanceApi.bindMarker(itemId)
      setBinding({ ...next, item_id: itemId })
      setBindingMessage(next.message || 'Marker 已绑定。是否识别成功仍以后端摄像头结果为准。')
      setMarkerNonce((value) => value + 1)
      await refreshStatus()
    } catch (error) {
      setBindingMessage(error instanceof Error ? error.message : '绑定失败，请回到主机检查后端')
    } finally {
      setBindingBusy(false)
    }
  }

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen()
      else await markerPageRef.current?.requestFullscreen()
    } catch {
      setBindingMessage('浏览器没有允许全屏；可从浏览器菜单手动进入全屏。')
    }
  }

  async function requestWakeLock() {
    const wakeLock = (navigator as Navigator & { wakeLock?: { request: (kind: 'screen') => Promise<WakeLockHandle> } }).wakeLock
    if (!wakeLock) { setWakeState('unsupported'); return }
    try {
      await wakeLockRef.current?.release()
      wakeLockRef.current = await wakeLock.request('screen')
      setWakeState('active')
    } catch {
      setWakeState('failed')
    }
  }

  return (
    <main className="validation-marker-page" ref={markerPageRef}>
      <RuntimeModeBanner mode={runtime.mode} showReal />
      <header className="validation-marker-header">
        <Link to="/acceptance/real-movement"><ChevronLeft />返回真实移动验收</Link>
        <div><Brand compact /><strong>识别标签</strong></div>
        <span className={`marker-pipeline-chip ${recognized ? 'recognized' : pipelineOnline ? 'watching' : ''}`}>
          <span />{recognized ? '后端已识别' : pipelineOnline ? '后端正在看' : '等待摄像头'}
        </span>
      </header>

      <div className="validation-marker-layout">
        <section className="validation-marker-main" aria-labelledby="marker-title">
          <div className="marker-heading">
            <p className="eyebrow">给真实摄像头看的标签</p>
            <h1 id="marker-title">在这台真实手机上全屏显示 Marker</h1>
            <p>让摄像头看见整块“{selected?.name || '我的手机'}”屏幕，保持 Marker 完整、不要反光或裁边。打印仅作备用，不是默认验收方式；页面也不会因为打开了 Marker 就声称识别成功。</p>
          </div>

          <div className="aruco-display" aria-label={markerId === null ? '等待 Marker ID' : `ArUco Marker ${markerId}`}>
            {markerId !== null ? <img src={acceptanceApi.markerImageUrl(markerId, 1000, markerNonce)} alt={`ArUco Marker ${markerId}`} /> : <div className="marker-empty"><Smartphone /><span>{itemId ? '先绑定，后台才会分配 Marker ID' : '先选择要绑定的手机'}</span></div>}
          </div>
          <div className="marker-id-line"><span>MARKER ID</span><strong>{markerId ?? '等待后台分配'}</strong></div>

          <div className="marker-screen-actions">
            <button type="button" className="button secondary" onClick={() => void toggleFullscreen()}><Maximize2 />{fullscreen ? '退出全屏' : '全屏显示'}</button>
            <button type="button" className="button secondary" onClick={() => void requestWakeLock()}><Sun />{wakeState === 'active' ? '屏幕常亮中' : '尝试保持常亮'}</button>
          </div>
          <div className="marker-brightness-note"><AlertTriangle /><div><strong>请手动把亮度调高，并临时关闭自动锁屏</strong><span>网页不能强制修改手机亮度。常亮功能也可能被省电模式或浏览器拒绝。</span></div></div>
        </section>

        <aside className="validation-marker-side">
          <section className="marker-binding-card panel">
            <p className="eyebrow">绑定物品</p>
            <label className="field"><span className="field-label">这张 Marker 属于</span><select aria-label="这张 Marker 属于" value={itemId} disabled={loadingItems || bindingBusy || Boolean(validationRunId)} onChange={(event) => { setItemId(event.target.value); setBinding(null); setBindingMessage('') }}><option value="">选择物品</option>{items.map((item) => <option key={item.id} value={item.id}>{item.name}{/手机|phone/i.test(`${item.name} ${item.type}`) ? ' · 手机' : ''}</option>)}</select></label>
            <button type="button" className="button primary full" disabled={!itemId || bindingBusy || Boolean(validationRunId)} onClick={() => void bind()}>{bindingBusy ? <RefreshCw className="spin" /> : <ShieldCheck />}{bindingBusy ? '正在绑定…' : validationRunId ? '本轮已锁定绑定' : '绑定到这个物品'}</button>
            {validationRunId && <p className="marker-binding-message">验收轮次建立后不能重绑 Marker，以免摄像头重启并中断本轮。需要修改时请先回主机取消本轮。</p>}
            {bindingMessage && <p className="marker-binding-message">{bindingMessage}</p>}
          </section>

          <section className="marker-truth-card panel" aria-live="polite">
            <div className="section-heading compact-heading"><div><p className="eyebrow">摄像头管线回报</p><h2>识别状态</h2></div><button type="button" className="icon-button" aria-label="立即刷新识别状态" onClick={() => void refreshStatus()}><RefreshCw /></button></div>
            {statusError && <div className="marker-status-error"><AlertTriangle /><span>{statusError}</span></div>}
            <div className={`marker-recognition-result ${recognized ? 'confirmed' : ''}`}>
              {recognized ? <CheckCircle2 /> : <Eye />}
              <div><strong>{recognized ? '后端摄像头已识别' : '后端尚未识别'}</strong><span>{status?.message || (!cameraId ? '请从主机验收页打开本页，地址必须包含所选摄像头。' : pipelineOnline || status?.server_observed ? '继续让 Marker 完整出现在画面里；只有后端真实帧能改变这里的结果。' : '先在主机启动 REAL 模式的本机摄像头。')}</span></div>
            </div>
            <dl className="marker-pipeline-facts">
              <div><dt>检测后台</dt><dd>{status?.detector_backend || '后端尚未回报'}</dd></div>
              <div><dt>摄像头</dt><dd>{status?.camera_name || status?.camera_id || '尚未绑定管线'}</dd></div>
              <div><dt>来源</dt><dd>{status?.source_type || '尚未回报'}</dd></div>
              <div><dt>最后识别</dt><dd>{readableTime(status?.last_seen_at || status?.last_observed_at)}</dd></div>
              <div><dt>可信度</dt><dd>{typeof status?.confidence === 'number' ? `${Math.round(status.confidence * 100)}%` : '—'}</dd></div>
              <div><dt>帧 / 轨迹</dt><dd>{status?.source_frame ?? status?.frame ?? '—'} / {status?.track_id ?? '—'}</dd></div>
            </dl>
            <p className="marker-truth-footnote"><Camera />此处每 1.5 秒读取一次{validationRunId ? `验收 ${validationRunId.slice(0, 8)}… 的` : ''}后端状态；绑定成功不等于摄像头识别成功。</p>
          </section>

          <section className="marker-privacy-card"><MonitorSmartphone /><div><strong>这是显示页，不采集手机摄像头</strong><span>图像识别发生在运行 ObjectMemory 的主机摄像头管线。</span></div></section>
        </aside>
      </div>
    </main>
  )
}
