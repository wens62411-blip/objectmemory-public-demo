import { Activity, Camera as CameraIcon, Check, ChevronRight, CircleStop, FileVideo, Globe2, Laptop, MonitorUp, Network, PencilRuler, Play, Plus, RadioTower, RefreshCw, Search, ShieldCheck, Trash2, Upload, Video, Wifi } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Badge, EmptyState, ErrorState, Field, Loading, Modal, PageHeader } from '../components/UI'
import { useApi, usePolling } from '../hooks/useApi'
import { api } from '../lib/api'
import { statusTone } from '../lib/format'
import type { Camera, CameraHealth, ResolvedRuntimeMode, SourceType } from '../types'
import { useToast } from '../components/Toast'
import { BrowserCamera, type BrowserCameraState } from '../components/BrowserCamera'
import { cameraStatusText } from '../lib/cameraStatus'
import { useRuntime } from '../contexts/RuntimeContext'
import { SourceBadge } from '../components/ProvenanceBadge'
import { canonicalSourceType } from '../lib/provenance'
import '../camera-usability.css'

const sources: Array<{ type: SourceType; title: string; text: string; icon: typeof CameraIcon; tag?: string }> = [
  { type: 'webcam', title: '电脑 / USB 摄像头', text: '使用这台电脑内置或已插入的 USB 摄像头。', icon: Laptop, tag: '最快开始' },
  { type: 'browser', title: '手机 / 浏览器摄像头', text: '客户端 JPEG 无法证明来自物理镜头；REAL 模式仅更新“最后看到”，不生成真实确认事件。', icon: Video, tag: '仅观察记录' },
  { type: 'esp32', title: 'ESP32 / XIAO 开发板', text: '通过设备中心安装、配网；仅支持明确适配的两种板型。', icon: RadioTower },
  { type: 'rtsp', title: 'RTSP 摄像头', text: '使用你有权访问的标准 RTSP 地址。', icon: Network },
  { type: 'onvif', title: 'ONVIF', text: '仅在你点击搜索时发现本地私有网络设备。', icon: Search },
  { type: 'mjpeg', title: 'HTTP / MJPEG', text: '接入明确提供的 HTTP 视频流地址。', icon: Globe2 },
  { type: 'screen', title: '厂商窗口区域', text: '只采集你主动选择的屏幕矩形区域。', icon: MonitorUp, tag: '授权兜底' },
  { type: 'video', title: '视频文件回放', text: '用本地 MP4、AVI 或 MOV 验证完整流程。', icon: FileVideo, tag: '测试回放' },
]

interface WizardForm {
  source_type: SourceType
  source: string
  index: number
  username: string
  password: string
  screen_x: number
  screen_y: number
  screen_width: number
  screen_height: number
  screen_authorized: boolean
  loop: boolean
  name: string
  room_name: string
  installation: string
  inference_fps: number
  save_clips: boolean
  enabled: boolean
}

const initialForm: WizardForm = { source_type: 'webcam', source: '0', index: 0, username: '', password: '', screen_x: 0, screen_y: 0, screen_width: 1280, screen_height: 720, screen_authorized: false, loop: true, name: '客厅摄像头', room_name: '客厅', installation: '', inference_fps: 5, save_clips: true, enabled: true }

function connectionPresentation(sourceType: SourceType, mode: ResolvedRuntimeMode) {
  const source = canonicalSourceType(sourceType)
  if (source === 'video_file') return { message: '已读取到明确标识的测试视频画面', alt: '连接测试保存的测试视频截图' }
  if (source === 'browser_camera') return { message: '已读取到浏览器上传画面；帧来源仍未验证', alt: '连接测试保存的浏览器上传截图' }
  if (source === 'authorized_screen_capture') return { message: '已读取到授权窗口采集画面', alt: '连接测试保存的授权窗口采集截图' }
  if (['rtsp', 'onvif', 'mjpeg'].includes(String(source))) return { message: '已读取到网络视频流；物理来源仍未验证', alt: '连接测试保存的网络视频流截图' }
  if (source === 'esp32_real') return { message: '已读取到设备画面；物理身份以设备中心的 USB 串口验证状态为准', alt: '连接测试保存的 ESP32 画面' }
  if (source !== 'opencv_camera') {
    return { message: '已读取到画面，但来源仍未验证', alt: '连接测试保存的来源未验证截图' }
  }
  if (mode === 'REAL') return { message: '已从本机摄像头设备读取画面', alt: '连接测试保存的本机摄像头截图' }
  if (mode === 'DEMO' || mode === 'TEST') return { message: `已读取到摄像头画面；当前 ${mode} 事件仍标记为模拟`, alt: `连接测试保存的 ${mode} 模式摄像头截图` }
  return { message: '已读取到摄像头画面，但运行模式未确认', alt: '连接测试保存的运行模式未确认截图' }
}

export function CamerasPage() {
  const [params, setParams] = useSearchParams()
  const cameras = useApi<Camera[]>('/api/cameras', [])
  const backendHealth = usePolling<{ status: string }>('/api/health', { status: 'connecting' }, 5000)
  const snapshotRevision = useMemo(() => Date.now(), [cameras.data, backendHealth.data])
  const [wizard, setWizard] = useState(params.get('add') === '1')
  const [step, setStep] = useState(1)
  const [form, setForm] = useState<WizardForm>(initialForm)
  const [draftId, setDraftId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<(CameraHealth & { success?: boolean; message?: string; frame_url?: string; snapshot_url?: string }) | null>(null)
  const [busy, setBusy] = useState(false)
  const [creationUncertain, setCreationUncertain] = useState(false)
  const [actionCameraId, setActionCameraId] = useState<string | null>(null)
  const testAbort = useRef<AbortController | null>(null)
  const [videoUploading, setVideoUploading] = useState(false)
  const [onvifMessage, setOnvifMessage] = useState('')
  const [captureGrant, setCaptureGrant] = useState('')
  const [browserState, setBrowserState] = useState<BrowserCameraState | null>(null)
  const { notify } = useToast()
  const runtime = useRuntime()
  const connectionKey = JSON.stringify({
    mode: runtime.mode, source_type: form.source_type, source: form.source, index: form.index,
    username: form.username, password: form.password, loop: form.loop,
    screen_x: form.screen_x, screen_y: form.screen_y, screen_width: form.screen_width,
    screen_height: form.screen_height, screen_authorized: form.screen_authorized,
    browser_previewing: form.source_type === 'browser' ? Boolean(browserState?.previewing) : null,
  })
  const connectionGeneration = useRef(0)
  const testedConnection = useRef<string | null>(null)
  const [connectionChanged, setConnectionChanged] = useState(false)
  const currentTestResult = testedConnection.current === connectionKey ? testResult : null
  const existingWebcam = form.source_type === 'webcam' ? cameras.data.find((camera) => camera.id !== draftId && camera.source_type === 'webcam' && Number(camera.source) === form.index) : undefined

  useEffect(() => {
    testAbort.current?.abort()
    connectionGeneration.current += 1
    if (testedConnection.current !== null) setConnectionChanged(true)
    testedConnection.current = null
    setTestResult(null)
  }, [connectionKey])
  useEffect(() => () => { testAbort.current?.abort(); connectionGeneration.current += 1 }, [])

  useEffect(() => { if (params.get('add') === '1') setWizard(true) }, [params])
  const selectedSource = useMemo(() => sources.find((item) => item.type === form.source_type), [form.source_type])
  const allowedSources = useMemo(() => runtime.mode === 'DEMO' ? sources : sources.filter((item) => item.type !== 'video' && item.type !== 'video_file'), [runtime.mode])
  useEffect(() => {
    if (runtime.mode !== 'DEMO' && (form.source_type === 'video' || form.source_type === 'video_file')) setForm(initialForm)
  }, [runtime.mode, form.source_type])

  function openWizard() { testedConnection.current = null; setConnectionChanged(false); setCreationUncertain(false); setWizard(true); setStep(1); setForm(initialForm); setDraftId(null); setTestResult(null); setBrowserState(null); setParams({ add: '1' }) }
  async function closeWizard() {
    testAbort.current?.abort(); connectionGeneration.current += 1
    if (draftId && step < 4) {
      try { await api(`/api/cameras/${draftId}`, { method: 'DELETE' }) } catch { /* Best effort cleanup of an unfinished draft. */ }
    }
    setWizard(false); setDraftId(null); setParams({}); void cameras.reload()
  }

  function payload(grant = captureGrant) {
    const config: Record<string, unknown> = {}
    let source = form.source.trim()
    if (form.source_type === 'webcam') { source = String(form.index); config.index = form.index }
    if (form.source_type === 'browser') Object.assign(config, { device_id: form.source || undefined, width: 640, height: 480, upload_fps: 4 })
    if (form.source_type === 'rtsp' || form.source_type === 'onvif') { if (form.username) config.username = form.username; if (form.password) config.password = form.password }
    if (form.source_type === 'screen') Object.assign(config, { authorized: form.screen_authorized, region: { left: form.screen_x, top: form.screen_y, width: form.screen_width, height: form.screen_height }, capture_grant: grant })
    if (form.source_type === 'video') config.loop = form.loop
    return { name: form.name, room_name: form.room_name, installation: form.installation, source_type: form.source_type, source, config, enabled: form.enabled, inference_fps: form.inference_fps, save_clips: form.save_clips }
  }

  async function uploadVideo(file?: File) {
    if (!file) return
    if (runtime.mode !== 'DEMO') { notify('视频文件只允许在 DEMO 模式接入。', 'danger'); return }
    setVideoUploading(true)
    try {
      const data = new FormData(); data.append('file', file)
      const response = await api<{ source: string }>('/api/cameras/upload-video', { method: 'POST', body: data })
      setForm((value) => ({ ...value, source: response.source, name: file.name.replace(/\.[^.]+$/, '') || '测试回放' }))
      notify('视频已复制到本地测试目录', 'success')
    } catch (value) { notify(value instanceof Error ? value.message : '视频上传失败', 'danger') }
    finally { setVideoUploading(false) }
  }

  async function testConnection() {
    const generation = connectionGeneration.current
    testedConnection.current = connectionKey
    setBusy(true); setTestResult(null); setConnectionChanged(false)
    const controller = new AbortController()
    testAbort.current = controller
    let timedOut = false
    let createPending = false
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort() }, 20000)
    try {
      if (creationUncertain) throw new Error('上次创建结果尚未确认，请先核对已保存摄像头，不要重复提交。')
      if (existingWebcam) throw new Error(`编号 ${form.index} 已添加为“${existingWebcam.name}”，请使用已有摄像头，不需要再次添加。`)
      if (form.source_type === 'webcam' && (!Number.isInteger(form.index) || form.index < 0 || form.index > 16)) throw new Error('摄像头编号必须是 0 到 16 之间的整数。')
      if ((form.source_type === 'video' || form.source_type === 'video_file') && runtime.mode !== 'DEMO') throw new Error('视频文件只允许在 DEMO 模式接入。')
      if (backendHealth.error) throw new Error('后端未连接：摄像头测试已暂停，请先检查启动窗口显示的端口。')
      if (form.source_type === 'browser' && !browserState?.previewing) throw new Error('请先点击“授权并选择摄像头”，确认浏览器已经显示真实预览。')
      let grant = captureGrant
      if (form.source_type === 'screen') {
        if (!form.screen_authorized) throw new Error('请先确认只采集你主动选择的矩形区域。')
        const authorization = await api<{ capture_grant: string }>('/api/screen-capture/authorize', { method: 'POST', signal: controller.signal, json: { region: { left: form.screen_x, top: form.screen_y, width: form.screen_width, height: form.screen_height } } })
        grant = authorization.capture_grant
        setCaptureGrant(grant)
      }
      let id = draftId
      if (!id) {
        createPending = true
        const camera = await api<Camera>('/api/cameras', { method: 'POST', signal: controller.signal, json: payload(grant) })
        createPending = false
        id = camera.id; setDraftId(id)
        if (generation !== connectionGeneration.current) { notify('摄像头配置已保存，但测试已取消。请先核对摄像头列表，不会继续启动或自动重复创建。', 'info'); void cameras.reload(); return }
      } else await api(`/api/cameras/${id}`, { method: 'PATCH', signal: controller.signal, json: payload(grant) })
      if (form.source_type === 'browser') await api(`/api/cameras/${id}/start`, { method: 'POST', signal: controller.signal })
      let testGrant: string | undefined
      if (form.source_type === 'screen') {
        const authorization = await api<{ capture_grant: string }>('/api/screen-capture/authorize', { method: 'POST', signal: controller.signal, json: { region: { left: form.screen_x, top: form.screen_y, width: form.screen_width, height: form.screen_height } } })
        testGrant = authorization.capture_grant
      }
      if (controller.signal.aborted) throw new Error('连接测试已取消。')
      const response = await api<CameraHealth & { success: boolean; message?: string; frame_url?: string; snapshot_url?: string }>(`/api/cameras/${id}/test`, { method: 'POST', signal: controller.signal, ...(testGrant ? { json: { capture_grant: testGrant } } : {}) })
      if (generation !== connectionGeneration.current) return
      setTestResult(response)
      if (response.success) notify(connectionPresentation(form.source_type, runtime.mode).message, 'success')
    } catch (value) {
      // Aborting fetch cannot roll back a server-side POST. Without a returned
      // id we must not automatically create a second record on the next click.
      if (createPending && controller.signal.aborted) setCreationUncertain(true)
      if (generation === connectionGeneration.current) setTestResult({ status: 'failed', error: timedOut ? '测试超过 20 秒，已取消等待。后端可能仍在完成或释放已提交的操作；请先核对状态，勿反复添加同一设备。' : controller.signal.aborted ? '连接测试已取消，没有判定连接成功。' : value instanceof Error ? value.message : '连接失败', success: false })
    }
    finally { window.clearTimeout(timeout); if (testAbort.current === controller) { testAbort.current = null; setBusy(false) } }
  }

  async function saveInfo() {
    if (!draftId) return
    if (!currentTestResult?.success) { setStep(2); notify('连接配置已更改，请重新测试后再保存。', 'info'); return }
    setBusy(true)
    try {
      let grant: string | undefined
      if (form.source_type === 'screen') {
        const authorization = await api<{ capture_grant: string }>('/api/screen-capture/authorize', { method: 'POST', json: { region: { left: form.screen_x, top: form.screen_y, width: form.screen_width, height: form.screen_height } } })
        grant = authorization.capture_grant
      }
      await api(`/api/cameras/${draftId}`, { method: 'PATCH', json: payload(grant) })
      // Saving a label never implicitly opens a device. The wizard's explicit
      // enabled choice owns this separate, idempotent start action.
      if (form.enabled && form.source_type !== 'screen') await api(`/api/cameras/${draftId}/start`, { method: 'POST' })
      setStep(4); await cameras.reload()
    }
    catch (value) { notify(value instanceof Error ? value.message : '保存失败', 'danger') }
    finally { setBusy(false) }
  }

  async function discoverOnvif() {
    setOnvifMessage('正在搜索本地私有网络…')
    try {
      const response = await api<{ devices: Array<{ address: string; name?: string }>; message?: string }>('/api/camera-discovery/onvif', { method: 'POST', json: { authorized: true } })
      const found = response.devices
      setOnvifMessage(response.message || (found.length ? `发现 ${found.length} 台设备，请选择地址。` : '没有发现设备，可以手动填写设备地址。'))
      if (found[0]) setForm((value) => ({ ...value, source: found[0].address, name: found[0].name || value.name }))
    } catch (value) { setOnvifMessage(value instanceof Error ? value.message : '搜索失败，请手动填写地址。') }
  }

  async function cameraAction(camera: Camera, action: 'test' | 'start' | 'stop' | 'delete') {
    if (actionCameraId) return
    if (action === 'delete' && !window.confirm(`删除“${camera.name}”？关联历史事件仍可按后端保留策略处理。`)) return
    setActionCameraId(camera.id)
    try {
      if (action === 'delete') await api(`/api/cameras/${camera.id}`, { method: 'DELETE' })
      else {
        let grant: string | undefined
        if (camera.source_type === 'screen' && (action === 'test' || action === 'start')) {
          const region = camera.config?.region
          if (!region) throw new Error('屏幕区域未配置，请重新编辑并授权。')
          const authorization = await api<{ capture_grant: string }>('/api/screen-capture/authorize', { method: 'POST', json: { region } })
          grant = authorization.capture_grant
        }
        const response = await api<{ success?: boolean; message?: string }>(`/api/cameras/${camera.id}/${action}`, { method: 'POST', ...(grant ? { json: { capture_grant: grant } } : {}) })
        notify(response.message || (action === 'test' && response.success ? '连接测试成功' : '操作已提交'), response.success === false ? 'danger' : 'success')
      }
      await cameras.reload()
    } catch (value) { notify(value instanceof Error ? value.message : '操作失败', 'danger') }
    finally { setActionCameraId(null) }
  }

  return (
    <div>
      <PageHeader title="连接你已有的画面" description="支持电脑摄像头、浏览器直连、标准协议、已适配的 ESP32 / XIAO 开发板和明确授权的窗口区域。" actions={<div className="page-actions"><Link className="button secondary" to="/camera-diagnostics"><Activity />摄像头诊断</Link><button className="button primary" onClick={openWizard}><Plus />添加摄像头</button></div>} />
      {backendHealth.error && <div className="backend-offline-banner"><Wifi /><div><strong>后端未连接</strong><span>{backendHealth.error}。这不是摄像头故障；请先确认物忆后端正在当前端口运行。</span></div></div>}
      {cameras.loading ? <Loading /> : cameras.error ? <ErrorState message={cameras.error} onRetry={cameras.reload} /> : cameras.data.length === 0 ? <EmptyState icon={<CameraIcon />} title="还没有摄像头" description={runtime.mode === 'DEMO' ? '可添加测试视频；所有生成事件都会标记为模拟并写入 DEMO 数据库。' : '请添加电脑、浏览器或网络真实视频源。'} action={<button className="button primary" onClick={openWizard}><Plus />添加第一个摄像头</button>} /> : <div className="camera-card-grid">{cameras.data.map((camera) => <article className="camera-card" key={camera.id}>
        <div className="camera-card-preview">{(['ready', 'online', 'running', 'streaming'].includes((camera.health?.status || '').toLowerCase()) || camera.health?.status_code === 'STREAMING') && <img key={`${camera.id}:${snapshotRevision}`} loading="lazy" decoding="async" src={`/api/cameras/${encodeURIComponent(camera.id)}/frame?t=${snapshotRevision}`} alt={`${camera.name}当前画面`} onError={(event) => { event.currentTarget.style.display = 'none' }} />}<CameraIcon /><div><Badge tone={statusTone(camera.health?.status) as 'success' | 'warning' | 'danger' | 'neutral'}>{camera.health?.status === 'ready' || camera.health?.status === 'streaming' ? '视频正常' : cameraStatusText(camera.health?.status_code || camera.health?.status, null)}</Badge><SourceBadge record={camera} />{['browser', 'browser_camera'].includes(camera.source_type) && <Badge tone="blue">浏览器直连</Badge>}</div></div>
        <div className="camera-card-body"><div className="camera-card-title"><div><h2>{camera.name}</h2><p>{camera.room_name}{camera.installation ? ` · ${camera.installation}` : ''}</p></div><span className={`status-dot ${camera.health?.status === 'ready' ? 'online' : ''}`} /></div>
        <dl><div><dt>采集 / 预览</dt><dd>{camera.health?.capture_fps?.toFixed(1) ?? '—'} / {camera.health?.preview_fps?.toFixed(1) ?? '—'} FPS</dd></div><div><dt>物品推理</dt><dd>{(camera.health?.inference_fps ?? camera.health?.fps)?.toFixed(1) ?? '—'} FPS</dd></div><div><dt>处理延迟</dt><dd>{camera.health?.latency_ms !== undefined ? `${Math.round(camera.health.latency_ms)} ms` : '—'}</dd></div></dl>
        {camera.health?.error && <p className="card-error">{camera.health.error}</p>}
        {actionCameraId === camera.id && <p className="camera-operation-status" role="status">正在处理该摄像头操作，请勿重复点击；已有采集不会为每个观看者重新开启。</p>}
        <div className="camera-card-actions"><Link className="button secondary small" to={`/live?camera=${camera.id}`}><Play />查看</Link><Link className="button ghost small" to={`/cameras/${camera.id}/zones`}><PencilRuler />区域</Link><button className="icon-button" title="测试连接" onClick={() => cameraAction(camera, 'test')}><RefreshCw /></button>{camera.health?.status === 'ready' ? <button className="icon-button" title="停止" onClick={() => cameraAction(camera, 'stop')}><CircleStop /></button> : <button className="icon-button" title="启动" onClick={() => cameraAction(camera, 'start')}><Play /></button>}<button className="icon-button danger-icon" title="删除" onClick={() => cameraAction(camera, 'delete')}><Trash2 /></button></div>
        </div></article>)}</div>}

      {wizard && <Modal title="添加摄像头" description="四步完成连接、命名和位置区域设置。" onClose={() => void closeWizard()} wide>
        <div className="wizard-steps">{['选择来源', '测试连接', '设备信息', '划分区域'].map((label, index) => <div aria-current={step === index + 1 ? 'step' : undefined} className={`${step === index + 1 ? 'active' : ''} ${step > index + 1 ? 'done' : ''}`} key={label}><span>{step > index + 1 ? <Check /> : index + 1}</span><strong>{label}</strong></div>)}</div>
        {step === 1 && <div className="wizard-body">
          <p>选择你手边的设备；画面读取成功后，再设置名称和区域。</p>
          <div className="source-groups">{[
            { title: '电脑或手机摄像头', types: ['webcam', 'browser'], advanced: false },
            { title: '网络与专用设备（展开选择）', types: ['esp32', 'rtsp', 'onvif', 'mjpeg', 'screen'], advanced: true },
            ...(runtime.mode === 'DEMO' ? [{ title: '演示视频', types: ['video'], advanced: false }] : []),
          ].map(group => {
            const options = <div className="source-grid">{allowedSources.filter(source => group.types.includes(source.type)).map(({ type, title, text, tag, icon: Icon }) => <button type="button" aria-pressed={form.source_type === type} className={form.source_type === type ? 'selected' : ''} key={type} onClick={() => setForm(value => ({ ...value, source_type: type, source: type === 'webcam' ? '0' : type === 'video' ? 'demo/sample-videos/object-memory-demo.mp4' : '' }))}><Icon /><div><strong>{title}</strong><span>{text}</span></div>{tag && <Badge tone={type === 'video' ? 'warning' : 'blue'}>{tag}</Badge>}</button>)}</div>
            return group.advanced ? <details className="optional-details" key={group.title} open={group.types.includes(form.source_type) || undefined}><summary>{group.title}</summary>{options}</details> : <section key={group.title} aria-label={group.title}><h3>{group.title}</h3>{options}</section>
          })}</div>
          <p className="source-selection" role="status">已选择：{selectedSource?.title}</p>
          {runtime.mode !== 'DEMO' && <p className="photo-help">测试视频入口只在 DEMO 模式出现。没有设备也可以先浏览首页示例；示例不会成为真实位置记录。</p>}
          <div className="wizard-footer"><button className="button primary" onClick={() => setStep(2)}>下一步：测试连接 <ChevronRight /></button></div>
        </div>}
        {step === 2 && <div className="wizard-body connection-step"><div className="selected-source"><span className="metric-icon green">{selectedSource && <selectedSource.icon />}</span><div><strong>{selectedSource?.title}</strong><span>{selectedSource?.text}</span></div></div>
          <div className="form-grid">
            {form.source_type === 'webcam' && <><Field label="摄像头编号" hint="编号不代表物理设备身份；通常从 0 开始。已经添加的编号应直接使用，不要重复添加。"><input type="number" min={0} max={16} step={1} value={form.index} onChange={(event) => setForm((value) => ({ ...value, index: Number(event.target.value), source: event.target.value }))} /></Field>{existingWebcam && <div className="camera-connection-notice full-field" role="status"><strong>编号 {form.index} 已添加：{existingWebcam.name}</strong><p>这是已有摄像头，不是连接失败。重复打开可能产生设备占用；可直接查看现有画面，或选择另一台实际连接的设备编号。</p><Link className="button secondary small" to={`/live?camera=${existingWebcam.id}`}>使用已有摄像头</Link></div>}</>}
            {form.source_type === 'esp32' && <div className="camera-connection-notice full-field"><strong>ESP32 / XIAO 开发板从设备中心接入</strong><p>当前仅适配 AI Thinker ESP32-CAM 与 Seeed XIAO ESP32S3 Sense。请在设备中心选择实际板型，再执行受控 USB 安装、配网和读取测试；USB 枚举不等于通用板型自动适配，也不等于刷写成功。没有硬件时仅可在 DEMO 模式使用虚拟设备。</p><Link className="button secondary" to="/devices">前往设备中心</Link></div>}
            {form.source_type === 'browser' && <div className="full-field"><BrowserCamera cameraId={draftId || undefined} targetFps={4} onState={(value) => { setBrowserState(value); if (value.selectedDeviceId) setForm((current) => current.source === value.selectedDeviceId ? current : { ...current, source: value.selectedDeviceId }) }} /><p className="field-help">浏览器模式与 Python 原生模式不能同时占用同一个物理摄像头。浏览器上传通道不能证明帧一定来自 getUserMedia，因此 REAL 模式只记录“最后看到”，不会把它标成真实确认移动。若需要确认事件，请使用电脑 / USB 摄像头原生采集。</p></div>}
            {['esp32', 'mjpeg', 'rtsp'].includes(form.source_type) && <Field label={form.source_type === 'esp32' ? '设备地址' : '视频流地址'} hint={form.source_type === 'esp32' ? '例如 http://192.168.1.88；系统会检查 /health、/device、/capture 和 /stream。' : '只填写你拥有或被授权使用的地址。'} className="full-field"><input value={form.source} onChange={(event) => setForm((value) => ({ ...value, source: event.target.value }))} placeholder={form.source_type === 'rtsp' ? 'rtsp://192.168.1.20:554/stream' : 'http://192.168.1.88'} /></Field>}
            {form.source_type === 'onvif' && <><Field label="设备地址"><input value={form.source} onChange={(event) => setForm((value) => ({ ...value, source: event.target.value }))} placeholder="http://192.168.1.20" /></Field><div className="field action-field"><span className="field-label">局域网发现</span><button className="button secondary" type="button" onClick={discoverOnvif}><Search />主动搜索</button>{onvifMessage && <small>{onvifMessage}</small>}</div></>}
            {['rtsp', 'onvif'].includes(form.source_type) && <><Field label="管理员账号"><input autoComplete="username" value={form.username} onChange={(event) => setForm((value) => ({ ...value, username: event.target.value }))} /></Field><Field label="密码" hint="密码不会出现在连接日志中。"><input type="password" autoComplete="new-password" value={form.password} onChange={(event) => setForm((value) => ({ ...value, password: event.target.value }))} /></Field></>}
            {form.source_type === 'screen' && <><div className="inline-banner info full-field"><ShieldCheck />软件只读取你填写的矩形区域，不抓取 Cookie 或密码。授权必须在运行物忆的这台电脑上完成；窗口最小化或被遮挡时可能无法采集。</div><Field label="左上角 X"><input type="number" min={0} value={form.screen_x} onChange={(event) => setForm((value) => ({ ...value, screen_x: Number(event.target.value) }))} /></Field><Field label="左上角 Y"><input type="number" min={0} value={form.screen_y} onChange={(event) => setForm((value) => ({ ...value, screen_y: Number(event.target.value) }))} /></Field><Field label="宽度"><input type="number" min={160} value={form.screen_width} onChange={(event) => setForm((value) => ({ ...value, screen_width: Number(event.target.value) }))} /></Field><Field label="高度"><input type="number" min={120} value={form.screen_height} onChange={(event) => setForm((value) => ({ ...value, screen_height: Number(event.target.value) }))} /></Field><label className="authorization-check full-field"><input type="checkbox" checked={form.screen_authorized} onChange={(event) => { setCaptureGrant(''); setForm((value) => ({ ...value, screen_authorized: event.target.checked })) }} /><span><strong>我确认只采集上述矩形区域</strong><small>区域内是我本人已登录且有权使用的厂商页面或客户端。</small></span></label></>}
            {form.source_type === 'video' && <><Field label="本地视频" hint="支持 MP4、AVI、MOV。页面会始终标注“测试回放”。" className="full-field"><div className="file-drop compact-drop"><Upload /><span>{videoUploading ? '正在保存…' : form.source || '选择视频文件'}</span><input type="file" accept="video/mp4,video/x-msvideo,video/quicktime,.avi,.mov" onChange={(event) => void uploadVideo(event.target.files?.[0])} /></div></Field><label className="check-row full-field"><input type="checkbox" checked={form.loop} onChange={(event) => setForm((value) => ({ ...value, loop: event.target.checked }))} />循环播放测试视频</label></>}
          </div>
          {connectionChanged && <div className="inline-banner info"><RefreshCw />连接配置已更改，请重新测试。旧画面不能证明当前配置可用。</div>}
          {creationUncertain && <div className="camera-connection-notice" role="alert"><strong>创建结果尚未确认，已暂停重复提交</strong><p>请求被取消，但服务器可能已经保存了摄像头。请先关闭向导核对列表，使用已有记录；这里不会自动重试创建。</p><button className="button secondary small" onClick={() => void closeWizard()}>关闭并核对摄像头列表</button></div>}
          {currentTestResult && <div className={`test-result ${currentTestResult.success ? 'success' : 'danger'}`}><div>{currentTestResult.success ? <Check /> : <Wifi />}</div><section><strong>{currentTestResult.success ? connectionPresentation(form.source_type, runtime.mode).message : '连接没有成功'}</strong><p>{currentTestResult.message || currentTestResult.error || cameraStatusText(currentTestResult.status_code || currentTestResult.status, null)}</p>{currentTestResult.success && <dl><div><dt>分辨率</dt><dd>{currentTestResult.width} × {currentTestResult.height}</dd></div><div><dt>实际 FPS</dt><dd>{currentTestResult.fps?.toFixed(1)}</dd></div><div><dt>采集后端</dt><dd>{currentTestResult.backend || '浏览器'}</dd></div></dl>}</section>{(currentTestResult.snapshot_url || currentTestResult.frame_url) && <img src={currentTestResult.snapshot_url || currentTestResult.frame_url} alt={connectionPresentation(form.source_type, runtime.mode).alt} />}</div>}
          {busy && <p className="camera-operation-status" role="status">正在读取当前配置的实际画面，最多等待 20 秒；不会把未完成测试显示为成功。<button className="button ghost small" onClick={() => testAbort.current?.abort()}>取消测试</button></p>}
          <div className="wizard-footer split"><button className="button ghost" onClick={() => setStep(1)}>返回</button><div><button className="button secondary" disabled={busy || creationUncertain || videoUploading || Boolean(backendHealth.error) || Boolean(existingWebcam) || form.source_type === 'esp32' || (form.source_type === 'screen' ? !form.screen_authorized : form.source_type === 'browser' ? !browserState?.previewing : !form.source)} onClick={testConnection}><RefreshCw className={busy ? 'spin' : ''} />{busy ? '正在读取画面…' : form.source_type === 'screen' ? '授权并测试连接' : form.source_type === 'browser' ? '测试浏览器到视觉后端' : '测试连接'}</button><button className="button primary" disabled={!currentTestResult?.success || busy} onClick={() => setStep(3)}>连接成功，继续 <ChevronRight /></button></div></div>
        </div>}
        {step === 3 && <div className="wizard-body"><div className="form-grid"><Field label="摄像头名称"><input value={form.name} onChange={(event) => setForm((value) => ({ ...value, name: event.target.value }))} /></Field><Field label="房间名称"><input value={form.room_name} onChange={(event) => setForm((value) => ({ ...value, room_name: event.target.value }))} /></Field><Field label="安装位置" hint="例如：电视柜上方"><input value={form.installation} onChange={(event) => setForm((value) => ({ ...value, installation: event.target.value }))} /></Field><Field label="推理帧率"><input type="number" min={1} max={15} value={form.inference_fps} onChange={(event) => setForm((value) => ({ ...value, inference_fps: Number(event.target.value) }))} /></Field><label className="toggle-row"><div><strong>启用摄像头</strong><span>保存后自动开始处理画面</span></div><input type="checkbox" checked={form.enabled} onChange={(event) => setForm((value) => ({ ...value, enabled: event.target.checked }))} /></label><label className="toggle-row"><div><strong>保存事件录像</strong><span>只在事件发生前后保存短片</span></div><input type="checkbox" checked={form.save_clips} onChange={(event) => setForm((value) => ({ ...value, save_clips: event.target.checked }))} /></label></div><div className="wizard-footer split"><button className="button ghost" onClick={() => setStep(2)}>返回</button><button className="button primary" disabled={busy || !form.name.trim() || !form.room_name.trim()} onClick={saveInfo}>保存并划分区域 <ChevronRight /></button></div></div>}
        {step === 4 && <div className="wizard-body finish-step"><div className="success-orbit"><Check /></div><p className="eyebrow">摄像头已添加</p><h2>{form.room_name} · {form.name}</h2><p>最后一步是在静态画面上圈出桌面、沙发或柜子。区域使用归一化坐标，分辨率变化后仍然有效。</p><div className="finish-actions">{draftId && <Link className="button primary" to={`/cameras/${draftId}/zones`} onClick={() => { setWizard(false); setParams({}) }}><PencilRuler />现在划分区域</Link>}<button className="button secondary" onClick={() => { setDraftId(null); void closeWizard() }}>稍后再做</button></div></div>}
      </Modal>}
    </div>
  )
}
