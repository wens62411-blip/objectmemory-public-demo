import { AlertTriangle, Camera, CheckCircle2, CircleOff, ExternalLink, FileJson, LoaderCircle, Play, RefreshCw, Server, Settings2, Square, TerminalSquare, Wifi, WifiOff } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { BrowserCamera, type BrowserCameraState } from '../components/BrowserCamera'
import { Badge, EmptyState, ErrorState, Loading, PageHeader } from '../components/UI'
import { useApi, usePolling } from '../hooks/useApi'
import { api, wsUrl } from '../lib/api'
import { cameraStatusText } from '../lib/cameraStatus'
import { normalizeRuntimeConfig, runtimeMode } from '../lib/runtime'
import type { Camera as CameraRecord, CameraDiagnosticReport, RuntimeConfig } from '../types'
import '../camera-usability.css'

type ChainState = { status: 'idle' | 'running' | 'success' | 'failed'; message: string; frames: number; wsMessages: number }
type DiagnosticJob = { id: string; status: string; message: string; completed: number; total: number; overall_timeout_seconds: number }
type DiagnosticEnvelope = CameraDiagnosticReport & { report?: CameraDiagnosticReport | null; report_error?: string | null; owners?: CameraDiagnosticReport['manager']; running_cameras?: CameraRecord[]; diagnostic_running?: boolean; job?: DiagnosticJob | null; report_is_previous?: boolean }

function displayOwner(manager?: CameraDiagnosticReport['manager']) {
  if (!manager) return '无占用记录'
  const values = Array.isArray(manager) ? manager : Object.values(manager).filter((item) => item && typeof item === 'object') as Array<Record<string, unknown>>
  const active = values.find((item) => item.owner)
  if (!active) return '当前没有模块持有摄像头'
  return `${String(active.owner)} · 线程 ${String(active.thread_id || '未知')} · ${String(active.subscribers ?? 0)} 个订阅`
}

function formatCameraLog(entry: unknown) {
  if (typeof entry === 'string') return entry
  if (!entry || typeof entry !== 'object') return String(entry ?? '')
  const log = entry as { timestamp?: string; level?: string; code?: string; message?: string; details?: unknown }
  const prefix = [log.timestamp, log.level?.toUpperCase(), log.code].filter(Boolean).join(' · ')
  const details = log.details && typeof log.details === 'object' && Object.keys(log.details as object).length ? ` · ${JSON.stringify(log.details)}` : ''
  return `${prefix}${prefix ? ' · ' : ''}${log.message || '摄像头状态更新'}${details}`
}

export function CameraDiagnosticsPage() {
  const runtime = usePolling<RuntimeConfig>('/api/runtime-config', {}, 5000)
  const diagnostics = usePolling<DiagnosticEnvelope>('/api/camera-diagnostics', {}, 1500)
  const cameras = useApi<CameraRecord[]>('/api/cameras', [])
  const [browserState, setBrowserState] = useState<BrowserCameraState | null>(null)
  const [diagnosing, setDiagnosing] = useState(false)
  const [diagnosticError, setDiagnosticError] = useState('')
  const [indices, setIndices] = useState('0')
  const [diagnosticMessage, setDiagnosticMessage] = useState('')
  const chainAbort = useRef<AbortController | null>(null)
  const [nativeCameraId, setNativeCameraId] = useState('')
  const [chain, setChain] = useState<ChainState>({ status: 'idle', message: '尚未执行完整链路测试', frames: 0, wsMessages: 0 })
  const config = normalizeRuntimeConfig(runtime.data)
  const webcamCameras = cameras.data.filter((camera) => ['webcam', 'opencv_camera'].includes(camera.source_type))
  const report = diagnostics.data.report || diagnostics.data
  const successful = report.results?.filter((item) => item.frame_read_success) || []
  const logs = (diagnostics.data.logs || report.logs || []).slice(-50).map(formatCameraLog)
  const runningCameras = diagnostics.data.running_cameras || []
  const job = diagnostics.data.job
  const jobRunning = Boolean(diagnostics.data.diagnostic_running)
  useEffect(() => () => chainAbort.current?.abort(), [])

  async function runDiagnostics(profile: 'quick' | 'full' = 'quick') {
    if (browserState?.previewing) {
      setDiagnosticError('本页浏览器正在预览镜头，请先停止浏览器预览，再进行 OpenCV 设备探测；快速刷新状态不受影响。')
      return
    }
    setDiagnosing(true)
    setDiagnosticError(''); setDiagnosticMessage('')
    try {
      const result = await api<DiagnosticEnvelope>('/api/camera-diagnostics/run', { method: 'POST', json: { indices, profile, timeout_seconds: 8, overall_timeout_seconds: profile === 'quick' ? 45 : 90 } })
      diagnostics.setData(result)
      setDiagnosticMessage(result.message || '任务已提交，可继续使用页面；下方显示实际进度。')
    } catch (error) {
      setDiagnosticError(error instanceof Error ? error.message : '摄像头诊断失败')
    } finally { setDiagnosing(false) }
  }

  async function cancelDiagnostics() {
    try { diagnostics.setData(await api<DiagnosticEnvelope>('/api/camera-diagnostics/cancel', { method: 'POST', json: { job_id: job?.id } })) }
    catch (error) { setDiagnosticError(error instanceof Error ? error.message : '取消失败') }
  }

  async function loadFrame(cameraId: string) {
    const response = await fetch(`/api/cameras/${encodeURIComponent(cameraId)}/frame?t=${Date.now()}`, { cache: 'no-store', credentials: 'same-origin', signal: chainAbort.current?.signal })
    if (!response.ok || !response.headers.get('content-type')?.includes('image/jpeg')) throw new Error(`后端 JPEG 请求失败（${response.status}）`)
    const bytes = new Uint8Array(await response.arrayBuffer())
    if (bytes.length < 100) throw new Error('后端返回的 JPEG 画面为空')
    let hash = 2166136261
    for (let index = 0; index < bytes.length; index += 97) { hash ^= bytes[index]; hash = Math.imul(hash, 16777619) }
    const url = URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' }))
    try {
      const dimensions = await new Promise<{ width: number; height: number }>((resolve, reject) => {
        const image = new Image(); const timeout = window.setTimeout(() => reject(new Error('浏览器解码后端 JPEG 超时')), 4000)
        image.onload = () => { window.clearTimeout(timeout); resolve({ width: image.naturalWidth, height: image.naturalHeight }) }
        image.onerror = () => { window.clearTimeout(timeout); reject(new Error('浏览器无法解码后端 JPEG 画面')) }
        image.src = url
      })
      return { ...dimensions, hash: hash >>> 0, sourceFrame: response.headers.get('X-Frame-Sequence'), sourceSession: response.headers.get('X-Source-Session-Id') }
    } finally { URL.revokeObjectURL(url) }
  }

  async function waitForFrame(cameraId: string, timeoutMs = 8000) {
    const deadline = Date.now() + timeoutMs
    let lastError: unknown
    while (Date.now() < deadline) {
      if (chainAbort.current?.signal.aborted) throw new Error('链路测试已取消；原有摄像头保持运行。')
      try { return await loadFrame(cameraId) }
      catch (error) { lastError = error; await new Promise((resolve) => window.setTimeout(resolve, 250)) }
    }
    throw lastError instanceof Error ? lastError : new Error('摄像头预热超时，后端仍未生成可解码画面。')
  }

  async function runFullChain() {
    if (!nativeCameraId) return setChain({ status: 'failed', message: '请先选择一个已添加的电脑摄像头。', frames: 0, wsMessages: 0 })
    setChain({ status: 'running', message: '正在启动后端、读取连续画面并检查 WebSocket…', frames: 0, wsMessages: 0 })
    const controller = new AbortController()
    chainAbort.current = controller
    const overallTimeout = window.setTimeout(() => controller.abort(), 20000)
    let socket: WebSocket | null = null
    let wsTimeout: number | undefined
    try {
      if (!runningCameras.some((camera) => camera.id === nativeCameraId)) await api(`/api/cameras/${encodeURIComponent(nativeCameraId)}/start`, { method: 'POST', signal: controller.signal })
      const warmupDeadline = Date.now() + 8000
      let processedFrames = 0
      while (Date.now() < warmupDeadline) {
        const current = await api<{ health?: { processed_frames?: number } }>(`/api/cameras/${encodeURIComponent(nativeCameraId)}`, { signal: controller.signal })
        processedFrames = Number(current.health?.processed_frames || 0)
        if (processedFrames > 0) break
        await new Promise((resolve) => window.setTimeout(resolve, 200))
      }
      if (processedFrames < 1) throw new Error('摄像头已打开，但视觉服务在 8 秒内没有生成第一张画面。')
      let wsMessages = 0
      socket = new WebSocket(wsUrl(`/ws/cameras/${encodeURIComponent(nativeCameraId)}`))
      const websocketReady = new Promise<void>((resolve, reject) => {
        wsTimeout = window.setTimeout(() => reject(new Error('摄像头 WebSocket 在 8 秒内没有返回状态')), 8000)
        socket!.onmessage = () => { wsMessages += 1; if (wsMessages >= 3) { window.clearTimeout(wsTimeout); resolve() } }
        socket!.onerror = () => { window.clearTimeout(wsTimeout); reject(new Error('摄像头 WebSocket 连接失败')) }
        controller.signal.addEventListener('abort', () => { socket?.close(); reject(new Error('链路测试已取消或达到 20 秒时限；摄像头不会因此被停止。')) }, { once: true })
      })
      void websocketReady.catch(() => undefined)
      let frames = 0
      let dimensions = { width: 0, height: 0 }
      const hashes = new Set<number>()
      const sourceFrames = new Set<string>()
      const sourceSessions = new Set<string>()
      let lastSourceFrame = -1
      for (let attempt = 0; attempt < 5; attempt += 1) {
        const frame = await waitForFrame(nativeCameraId, attempt === 0 ? 8000 : 3000); dimensions = frame; hashes.add(frame.hash); frames += 1
        if (frame.sourceFrame && frame.sourceSession) {
          const sequence = Number(frame.sourceFrame)
          if (!Number.isSafeInteger(sequence) || sequence < lastSourceFrame) throw new Error('服务器源帧编号无效或发生倒退，不能确认视频连续性。')
          lastSourceFrame = sequence
          sourceFrames.add(`${frame.sourceSession}:${frame.sourceFrame}`); sourceSessions.add(frame.sourceSession)
        }
        setChain({ status: 'running', message: `已显示 ${frames}/5 张连续画面，正在检查状态通道…`, frames, wsMessages })
        await new Promise((resolve) => window.setTimeout(resolve, 350))
      }
      await websocketReady
      if (sourceSessions.size > 1) throw new Error('测试期间视频源发生重连，连续性已中断，请等待稳定后重新测试。')
      if (sourceFrames.size < 2) throw new Error('已解码画面，但没有两个递增的服务器源帧编号，不能证明采集持续更新。')
      setChain({ status: 'success', message: `视频链路连接成功：解码 ${frames} 张 ${dimensions.width}×${dimensions.height} 画面（${sourceFrames.size} 个源帧，${hashes.size} 种画面内容），收到 ${wsMessages} 条状态。这里只证明取流链路，不证明物品已识别或物理设备身份。`, frames, wsMessages })
    } catch (error) {
      setChain((value) => ({ ...value, status: 'failed', message: error instanceof Error ? error.message : '完整视频链路失败' }))
    } finally { window.clearTimeout(overallTimeout); window.clearTimeout(wsTimeout); socket?.close(); if (chainAbort.current === controller) chainAbort.current = null }
  }

  async function stopNative() {
    if (!nativeCameraId) return
    try { await api(`/api/cameras/${encodeURIComponent(nativeCameraId)}/stop`, { method: 'POST' }); setChain({ status: 'idle', message: '摄像头已停止并请求后端释放资源。', frames: 0, wsMessages: 0 }); await cameras.reload() }
    catch (error) { setChain({ status: 'failed', message: error instanceof Error ? error.message : '停止摄像头失败', frames: 0, wsMessages: 0 }) }
  }

  const browserDevices = browserState?.devices || []
  const systemStatus = successful.length ? `已找到 ${new Set(successful.map((item) => item.device_index)).size} 个可读取索引` : diagnostics.loading ? '正在读取诊断记录' : '尚未证明 OpenCV 可读取画面'
  const owner = useMemo(() => displayOwner(diagnostics.data.owners || report.manager), [diagnostics.data.owners, report.manager])

  return <div>
    <PageHeader title="找到画面断在哪一层" description="快速刷新只读现有状态；实际探测和完整视频链路分别执行，不重复打开正在使用的镜头。" actions={<button className="button primary" disabled={diagnostics.loading} onClick={() => void diagnostics.reload()}><RefreshCw />快速刷新状态</button>} />

    {runtime.error && <div className="backend-offline-banner"><WifiOff /><div><strong>后端未连接</strong><span>{runtime.error}。摄像头测试已暂停，先确认启动窗口中的端口和错误日志。</span></div></div>}
    {runtimeMode(config) === 'DEMO' && <div className="inline-banner info"><AlertTriangle />当前为 DEMO 模式。电脑摄像头诊断可读取真实本机设备，但该进程产生的事件仍属于隔离的模拟数据；虚拟 ESP32 不会算作真机。</div>}

    <section className="runtime-grid panel">
      <div><span>当前前端地址</span><strong>{config.frontend_url}</strong></div><div><span>当前 API 地址</span><strong>{config.api_base_url}</strong></div><div><span>当前 WebSocket 地址</span><strong>{config.websocket_base_url}</strong></div><div><span>后端健康状态</span><strong className={runtime.error ? 'danger-text' : 'success-text'}>{runtime.error ? '未连接' : config.backend_healthy === false ? '健康检查未通过' : '已连接'}</strong></div>
    </section>

    <div className="diagnostic-summary-grid">
      <article className="panel"><Camera /><span>Windows / OpenCV</span><strong>{systemStatus}</strong><small>{report.opencv_version ? `OpenCV ${report.opencv_version}` : '执行诊断后显示版本和后端'}</small></article>
      <article className="panel"><Server /><span>当前占用者</span><strong>{owner}</strong><small>测试连接和正式启动共享同一摄像头所有权</small></article>
      <article className="panel"><Wifi /><span>浏览器设备</span><strong>{browserDevices.length ? `${browserDevices.length} 个` : '等待授权'}</strong><small>{browserState?.secureContext ? '当前页面是安全上下文' : '请通过 localhost 或 HTTPS 打开'}</small></article>
    </div>

    <section className="diagnostic-test panel">
      <header><span className="test-letter">A</span><div><p className="eyebrow">浏览器直接预览</p><h2>测试浏览器权限与设备</h2></div><Badge tone={browserState?.previewing ? 'success' : 'neutral'}>{browserState?.previewing ? '真实预览中' : '未运行'}</Badge></header>
      <BrowserCamera onState={setBrowserState} />
    </section>

    <section className="diagnostic-test panel">
      <header><span className="test-letter">B</span><div><p className="eyebrow">Python / OpenCV</p><h2>测试索引与采集后端</h2></div><Badge tone={successful.length ? 'success' : report.status === 'failed' || diagnostics.data.status === 'failed' ? 'danger' : 'neutral'}>{successful.length ? `${successful.length} 组成功` : '等待诊断'}</Badge></header>
      <div className="camera-diagnostic-controls"><label>待探测摄像头编号<input aria-label="待探测摄像头编号" value={indices} maxLength={80} onChange={(event) => setIndices(event.target.value)} placeholder="0 或 0,1" /></label><button className="button primary" disabled={diagnosing || jobRunning || Boolean(runtime.error)} onClick={() => void runDiagnostics('quick')}>{diagnosing ? <LoaderCircle className="spin" /> : <RefreshCw />}探测选定编号</button><button className="button secondary" disabled={diagnosing || jobRunning || runningCameras.length > 0 || Boolean(browserState?.previewing)} onClick={() => void runDiagnostics('full')}>测试所有采集后端</button>{jobRunning && <button className="button secondary" disabled={job?.status === 'CANCELLING'} onClick={() => void cancelDiagnostics()}>取消诊断</button>}</div>
      <p className="field-help">默认只检查编号 0，找到可读取的后端即结束；完整后端测试最多 90 秒。PnP 设备名字不是 OpenCV 编号证明。</p>
      {runningCameras.length > 0 && <div className="camera-connection-notice"><strong>已有 {runningCameras.length} 路采集运行中</strong><p>快速检查复用当前状态，不开第二路摄像头。若要重新探测索引，请先用下方明确的“停止并释放”按钮停止目标来源。</p></div>}
      {diagnosticError && <p className="camera-diagnostic-error" role="alert">{diagnosticError}</p>}
      {diagnosticMessage && <p role="status">{diagnosticMessage}</p>}
      {job && <div className="camera-diagnostic-progress" aria-live="polite"><strong>{job.status} · 已测试 {job.completed}/{job.total} 组</strong>{jobRunning && <progress max={job.total} value={job.completed} />}<p>{job.message}</p><small>任务总时限 {job.overall_timeout_seconds} 秒；取消不会关闭其他应用。</small></div>}
      {report.generated_at && <p className="field-help">{diagnostics.data.report_is_previous ? '上一次报告（本次仍在运行）' : '最近报告'}：{report.generated_at}。历史通过不表示当前仍在线。</p>}
      {diagnostics.error ? <ErrorState message={diagnostics.error} onRetry={diagnostics.reload} /> : diagnostics.loading && !report.results ? <Loading label="正在读取诊断报告…" /> : report.results?.length ? <div className="diagnostic-table-wrap"><table className="diagnostic-table"><thead><tr><th>索引</th><th>后端</th><th>打开</th><th>读取帧</th><th>分辨率</th><th>FPS</th><th>诊断</th></tr></thead><tbody>{report.results.map((item, index) => <tr key={`${item.device_index}-${item.backend}-${index}`} className={item.frame_read_success ? 'passed' : ''}><td>{item.device_index}</td><td>{item.backend}</td><td>{item.opened ? '是' : '否'}</td><td>{item.successful_frames ?? 0} 成功 / {item.failed_frames ?? 0} 失败</td><td>{item.width && item.height ? `${item.width}×${item.height}` : '—'}</td><td>{item.actual_fps?.toFixed(1) || '—'}</td><td>{item.error || cameraStatusText(item.status_code, null)}</td></tr>)}</tbody></table></div> : <EmptyState icon={<TerminalSquare />} title="还没有实际诊断结果" description="选择确实需要检查的编号后开始探测。默认只检查编号 0，不会自动遍历 0–10 或把未完成的探测标为通过。" />}
      <div className="diagnostic-links">{report.screenshot_path && <a className="button secondary small" href="/api/camera-diagnostics/probe-image" target="_blank" rel="noreferrer"><Camera />查看真实探测截图</a>}{(diagnostics.data.report_path || report.report_path) && <a className="button ghost small" href="/api/camera-diagnostics/report" target="_blank" rel="noreferrer"><FileJson />查看 JSON 报告</a>}<a className="button ghost small" href="ms-settings:privacy-webcam"><Settings2 />打开 Windows 摄像头隐私设置 <ExternalLink /></a></div>
    </section>

    <section className="diagnostic-test panel">
      <header><span className="test-letter">C</span><div><p className="eyebrow">后端到前端</p><h2>完整视频链路</h2></div><Badge tone={chain.status === 'success' ? 'success' : chain.status === 'failed' ? 'danger' : chain.status === 'running' ? 'warning' : 'neutral'}>{chain.status === 'success' ? '系统连接成功' : chain.status === 'failed' ? '链路失败' : chain.status === 'running' ? '测试中' : '未运行'}</Badge></header>
      <div className="chain-controls"><label><span>已添加的电脑摄像头</span><select aria-label="完整链路摄像头" disabled={chain.status === 'running'} value={nativeCameraId} onChange={(event) => setNativeCameraId(event.target.value)}><option value="">请选择</option>{webcamCameras.map((camera) => <option value={camera.id} key={camera.id}>{camera.room_name} · {camera.name}</option>)}</select></label><button className="button primary" disabled={!nativeCameraId || chain.status === 'running' || Boolean(runtime.error)} onClick={() => void runFullChain()}><Play />开始测试 C</button>{chain.status === 'running' && <button className="button secondary" onClick={() => chainAbort.current?.abort()}>取消链路测试</button>}<button className="button secondary" disabled={!nativeCameraId || chain.status === 'running'} onClick={() => void stopNative()}><Square />停止并释放</button></div>
      <div className={`chain-result ${chain.status}`}>{chain.status === 'running' && <LoaderCircle className="spin" />}{chain.status === 'success' && <CheckCircle2 />}{chain.status === 'failed' && <CircleOff />}{chain.status === 'idle' && <Camera />}<div><strong>{chain.message}</strong><span>连续画面 {chain.frames} · WebSocket 状态 {chain.wsMessages}</span></div></div>
      {nativeCameraId && chain.status !== 'idle' && <div className="chain-preview"><img src={`/api/cameras/${encodeURIComponent(nativeCameraId)}/stream`} alt="测试 C 的后端实时连续画面" /></div>}
    </section>

    <section className="panel camera-log-panel"><div className="section-heading"><div><p className="eyebrow">最近状态</p><h2>摄像头日志</h2></div><span>最多显示 50 条，凭据由后端脱敏</span></div>{logs.length ? <div className="terminal">{logs.map((line, index) => <div key={`${index}-${line}`}><span>{String(index + 1).padStart(2, '0')}</span><code>{line}</code></div>)}</div> : <p className="muted-block">尚无摄像头日志。执行诊断或启动摄像头后再刷新。</p>}<p className="diagnostic-help"><AlertTriangle />本页不会关闭微信、腾讯会议、Teams、Zoom、浏览器或 Windows 相机。若状态显示占用，请由你自行关闭可能占用摄像头的软件后重试。</p><Link className="button ghost small" to="/cameras">返回摄像头管理</Link></section>
  </div>
}
