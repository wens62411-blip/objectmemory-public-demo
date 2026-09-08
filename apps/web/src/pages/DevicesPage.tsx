import { AlertTriangle, Camera, CheckCircle2, ChevronDown, ChevronRight, CircleOff, Code2, Cpu, Download, FileCheck2, HardDrive, LoaderCircle, MonitorPlay, Play, RadioTower, RefreshCw, RotateCcw, Router, ShieldCheck, Signal, Square, TerminalSquare, Trash2, Usb, Wifi, Wrench } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, EmptyState, ErrorState, Field, Loading, PageHeader } from '../components/UI'
import { usePolling } from '../hooks/useApi'
import { api } from '../lib/api'
import { formatTime } from '../lib/format'
import type { FirmwareDevice, FirmwareJob, FirmwarePort } from '../types'
import { useToast } from '../components/Toast'
import { useRuntime } from '../contexts/RuntimeContext'
import { deviceHardwareCategory } from '../lib/provenance'
import { AutoUsbSetup, usbBoardLabels, type UsbBoardModel } from '../components/AutoUsbSetup'
import { DeviceNetworkCheck } from '../components/DeviceNetworkCheck'
import { FirmwareCompatibility } from '../components/FirmwareCompatibility'
import { useVisiblePolling } from '../hooks/useVisiblePolling'

interface PortsResponse { platformio: { available: boolean | null; version?: string | null; python?: string | null }; ports: FirmwarePort[] }
interface InstallForm { port: string; ssid: string; password: string; backend_url: string; device_name: string; room_name: string }
interface ListenerSession { lan_enabled?: boolean; listener_verified?: boolean; lan_urls?: string[]; lan_message?: string }
interface ArtifactRecord { name: string; path: string; sha256: string; size: number; hash_verified?: boolean }
interface UsageRecord { used_bytes: number; total_bytes: number; percent: number }
interface FirmwareStatus {
  firmware_version?: string; platformio_core?: string; platform?: string; framework?: string; target_board?: string; built_at?: string
  source_present?: boolean; compile_passed?: boolean; manifest_hashes_verified?: boolean; manifest_path?: string; artifacts?: ArtifactRecord[]
  ram_usage?: UsageRecord | null; flash_usage?: UsageRecord | null; physical_usb_serial_detected?: boolean; suspected_esp32_usb_serial?: boolean
  physical_board_detected?: boolean; physical_flash_performed?: boolean; serial_provision_performed?: boolean; real_video_verified?: boolean; physical_status_note?: string
}
interface VirtualStatus { running: boolean; label?: string; source?: string; device_id?: string; camera_id?: string; stage?: string; backend_online?: boolean; last_heartbeat?: string; stream_status?: string; expected_offline_after_seconds?: number; log_path?: string; error?: string | null; message?: string }

const emptyPorts: PortsResponse = { platformio: { available: null }, ports: [] }
const boardPreferenceKey = 'objectmemory.usbBoardModel'
function rememberedBoard(): UsbBoardModel {
  try {
    const value = localStorage.getItem(boardPreferenceKey)
    if (value === 'ai_thinker_esp32cam' || value === 'xiao_esp32s3_sense') return value
  } catch { /* Storage may be unavailable; never store credentials as a fallback. */ }
  return 'ai_thinker_esp32cam'
}

export function DevicesPage() {
  const runtime = useRuntime()
  const [boardModel, setBoardModel] = useState<UsbBoardModel>(rememberedBoard)
  const boardManuallySelected = useRef(false)
  const backendManuallyEdited = useRef(false)
  const onBoundBoard = useCallback((board: UsbBoardModel) => { if (!boardManuallySelected.current) setBoardModel(board) }, [])
  useEffect(() => { try { localStorage.setItem(boardPreferenceKey, boardModel) } catch { /* Optional preference only. */ } }, [boardModel])
  const ports = usePolling<PortsResponse>('/api/firmware/ports', emptyPorts, 5000)
  const devices = usePolling<FirmwareDevice[]>('/api/devices', [], 5000)
  const firmware = usePolling<FirmwareStatus>(boardModel === 'ai_thinker_esp32cam' ? '/api/firmware/status' : `/api/firmware/status?board_model=${boardModel}`, {}, 5000)
  const virtual = usePolling<VirtualStatus>(runtime.mode === 'DEMO' ? '/api/virtual-device/status' : null, { running: false }, 3000)
  const [form, setForm] = useState<InstallForm>({ port: '', ssid: '', password: '', backend_url: '', device_name: '客厅摄像头', room_name: '客厅' })
  const onDetectedSsid = useCallback((ssid: string) => setForm((value) => value.ssid ? value : { ...value, ssid }), [])
  const [lanMessage, setLanMessage] = useState('正在验证当前服务是否实际监听局域网…')
  const [lanEnabled, setLanEnabled] = useState(false)
  const [job, setJob] = useState<FirmwareJob | null>(null)
  const [expandedDevice, setExpandedDevice] = useState<string | null>(null)
  const [advanced, setAdvanced] = useState(false)
  const [fallbackOpen, setFallbackOpen] = useState(false)
  const [virtualBusy, setVirtualBusy] = useState(false)
  const [virtualSource, setVirtualSource] = useState<'video' | 'webcam'>('video')
  const [enrollment, setEnrollment] = useState<{ pairing_code: string; expires_at: string; expires_in: number } | null>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const { notify } = useToast()
  const realHardwareMode = runtime.mode === 'REAL'

  const eligiblePorts = useMemo(() => ports.data.ports.filter((port) => port.eligible === true), [ports.data.ports])
  useEffect(() => {
    if (!form.port && eligiblePorts.length === 1) setForm((value) => ({ ...value, port: eligiblePorts[0].device }))
    if (form.port && !ports.loading && !ports.error && !eligiblePorts.some((port) => port.device === form.port)) setForm((value) => ({ ...value, port: '' }))
  }, [eligiblePorts, form.port, ports.loading, ports.error])
  useVisiblePolling(async (signal) => {
    try {
      const session = await api<ListenerSession>('/api/session', { signal })
      if (signal.aborted) return
      const enabled = session.lan_enabled === true && session.listener_verified === true
      setLanEnabled(enabled)
      const candidate = enabled ? session.lan_urls?.find((url) => !/\/\/(127\.0\.0\.1|localhost)(:|\/|$)/i.test(url)) : undefined
      if (candidate && !backendManuallyEdited.current) setForm((value) => session.lan_urls?.includes(value.backend_url) ? value : { ...value, backend_url: candidate })
      setLanMessage(enabled
        ? `${candidate ? `后台候选地址：${candidate}。` : '已验证局域网监听，但尚未找到可用的本机地址。'}尚未验证 ESP32、路由器或防火墙端到端可达，安装前会再检查地址。`
        : '当前后台仅监听本机或监听状态未经验证，已禁用需要局域网的安装与配网。请停止当前服务后双击 Start-Device.bat（内部调用 scripts/start.ps1 -Lan）；纯编译和纯刷写不受影响。')
    } catch {
      if (signal.aborted) return
      setLanEnabled(false)
      setLanMessage('无法验证后台监听状态，已禁用安装与配网。请双击 Start-Device.bat（内部调用 scripts/start.ps1 -Lan）重启服务；页面会自动重新检查，手填地址不能替代监听验证。')
    }
  }, { enabled: realHardwareMode, immediate: true, intervalMs: 5000, resetKey: runtime.mode })
  useVisiblePolling(async (signal) => {
    if (!job) return
      try {
        const next = await api<FirmwareJob>(`/api/firmware/jobs/${job.id}`, { signal })
        if (signal.aborted) return
        setJob(next)
        if (next.status === 'succeeded') { notify(next.job_type === 'usb_bind' ? '板卡身份已绑定，固定固件安装随后自动开始；当前尚不代表刷写完成。' : next.job_type === 'install' || next.job_type === 'usb_auto' ? '设备安装或恢复任务已完成，视频状态以实际读取为准。' : '固件任务已完成', 'success'); void devices.reload(); void firmware.reload(); void ports.reload() }
        if (next.status === 'failed') notify(next.error || '固件任务失败，请查看日志', 'danger')
      } catch (value) { if (!signal.aborted) notify(value instanceof Error ? value.message : '任务状态读取失败', 'danger') }
  }, { enabled: Boolean(job && ['queued', 'running'].includes(job.status)), intervalMs: 1000, resetKey: job?.id })
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight }, [job?.logs])
  useEffect(() => {
    if (!enrollment || enrollment.expires_in <= 0) return
    const timer = window.setInterval(() => setEnrollment((value) => value ? { ...value, expires_in: Math.max(0, value.expires_in - 1) } : null), 1000)
    return () => window.clearInterval(timer)
  }, [Boolean(enrollment)]) // eslint-disable-line react-hooks/exhaustive-deps

  async function createJob(path: string, payload: object) {
    try {
      const next = await api<FirmwareJob>(path, { method: 'POST', json: payload })
      setJob(next)
      if ('password' in payload) setForm((value) => ({ ...value, password: '' }))
    } catch (value) { notify(value instanceof Error ? value.message : '无法启动任务', 'danger') }
  }
  async function install(event: React.FormEvent) {
    event.preventDefault()
    if (boardModel !== 'ai_thinker_esp32cam') { notify('XIAO 请使用下方“绑定并开始自动安装”，先验证芯片和固定固件。', 'info'); return }
    if (!realHardwareMode) { notify('USB 刷写、安装和物理设备配网只允许在 REAL 模式执行；请切换模式并重启服务。', 'danger'); return }
    if (!lanEnabled) { notify('当前服务未验证局域网监听，请双击 Start-Device.bat 重启后再安装。', 'danger'); return }
    await createJob('/api/firmware/install', form)
  }
  async function createFallbackEnrollment() {
    if (!realHardwareMode) { notify('网络申领只允许在 REAL 模式创建；请切换模式并重启服务。', 'danger'); return }
    if (!lanEnabled) { notify('网络申领需要局域网监听，请双击 Start-Device.bat 重启。', 'danger'); return }
    try {
      const next = await api<{ pairing_code: string; expires_at: string; expires_in: number }>('/api/device-enrollment/create', { method: 'POST', json: { device_name: form.device_name, room_name: form.room_name } })
      setEnrollment(next)
    } catch (value) { notify(value instanceof Error ? value.message : '配对码生成失败', 'danger') }
  }
  async function command(device: FirmwareDevice, name: 'reboot' | 'factory_reset') {
    if (name === 'factory_reset' && !window.confirm(`恢复“${device.device_name || device.name || device.device_id}”出厂设置？设备将清除 Wi-Fi、配对码和令牌，需要 USB 重新安装。`)) return
    try { await api(`/api/devices/${device.id || device.device_id}/commands`, { method: 'POST', json: { command: name } }); notify('指令已排队；收到设备确认前不会显示为已执行。', 'info') }
    catch (value) { notify(value instanceof Error ? value.message : '指令发送失败', 'danger') }
  }
  async function revoke(device: FirmwareDevice) {
    if (!window.confirm(`撤销“${device.device_name || device.device_id}”的绑定令牌？设备会失去后台访问权限。`)) return
    try { await api(`/api/devices/${device.id || device.device_id}/revoke`, { method: 'POST' }); notify('设备令牌已撤销', 'success'); await devices.reload() }
    catch (value) { notify(value instanceof Error ? value.message : '撤销失败', 'danger') }
  }
  async function toggleVirtual() {
    if (runtime.mode !== 'DEMO') { notify('虚拟设备只允许在 DEMO 模式启动。', 'danger'); return }
    setVirtualBusy(true)
    try {
      const next = virtual.data.running
        ? await api<VirtualStatus>('/api/virtual-device/stop', { method: 'POST' })
        : await api<VirtualStatus>('/api/virtual-device/start', { method: 'POST', json: { source: virtualSource, camera_index: 0, device_name: '虚拟 ESP32-CAM', room_name: '客厅' } })
      virtual.setData(next)
      notify(next.message || (next.running ? '虚拟设备进程已启动，正在通过真实配对接口注册。' : '虚拟设备进程已停止，后台将按心跳超时标记离线。'), 'info')
      await Promise.all([virtual.reload(), devices.reload()])
    } catch (value) { notify(value instanceof Error ? value.message : '虚拟设备操作失败', 'danger') }
    finally { setVirtualBusy(false) }
  }

  const logLines = Array.isArray(job?.logs) ? job.logs : typeof job?.logs === 'string' ? job.logs.split('\n') : []
  const activeJob = job && ['queued', 'running'].includes(job.status)
  const portsPending = ports.data.platformio.available === null
  const platformVersion = ports.data.platformio.version || '已就绪'
  const platformLabel = ports.error ? '工具链检测失败' : portsPending ? '正在检测 PlatformIO…' : ports.data.platformio.available ? (/platformio/i.test(platformVersion) ? platformVersion : `PlatformIO ${platformVersion}`) : 'PlatformIO 未就绪'
  const manifestVerified = firmware.data.compile_passed === true && firmware.data.manifest_hashes_verified === true
  const usbDetected = eligiblePorts.length > 0 || firmware.data.physical_usb_serial_detected === true
  const boardStatus = !realHardwareMode ? '仅 REAL 模式检测' : firmware.data.physical_board_detected
    ? '检测到候选物理板' : usbDetected ? 'USB 串口已发现，板卡身份未验证' : portsPending ? '正在检测 USB 串口…' : '未发现可用 USB 串口'
  const physicalNote = realHardwareMode && usbDetected && !firmware.data.physical_board_detected
    ? '已发现可用 USB 串口；这不代表板型、固件写入、配网或摄像头视频已通过验证，各项以对应证据为准。'
    : firmware.data.physical_status_note || '设备状态只根据实际端口、构建、刷写和视频记录判断。'

  return (
    <div>
      <PageHeader eyebrow="设备中心" title="插上 USB，一键装好摄像头" description="编译、刷写、串口配网、后台注册和绑定由受控任务依次完成；未检测到硬件时不会显示刷写成功。" actions={<button className="button secondary" onClick={() => void ports.reload()}><RefreshCw />刷新串口</button>} />
      <section className="hardware-evidence panel">
        <div className="section-heading"><div><p className="eyebrow">真实验收状态</p><h2>固件与物理硬件证据</h2></div><Badge tone={manifestVerified ? 'success' : 'warning'}>{manifestVerified ? '编译与清单哈希已核验' : firmware.data.compile_passed ? '编译通过，清单哈希未核验' : '尚无有效构建清单'}</Badge></div>
        {firmware.error ? <ErrorState message={firmware.error} onRetry={firmware.reload} /> : <div className="truth-status-grid">
          <div><FileCheck2 /><span>固件源码</span><strong>{firmware.data.source_present ? '已存在' : '未确认'}</strong></div>
          <div><Code2 /><span>固件编译</span><strong>{firmware.data.compile_passed ? firmware.data.manifest_hashes_verified ? '已通过，哈希已核验' : '已通过，哈希未核验' : '未通过或产物缺失'}</strong></div>
          <div><Usb /><span>物理 ESP32</span><strong>{boardStatus}</strong></div>
          <div><Download /><span>真机刷写</span><strong>{firmware.data.physical_flash_performed ? '已实际执行' : '未执行'}</strong></div>
          <div><Wifi /><span>串口配网</span><strong>{firmware.data.serial_provision_performed ? '已实际执行' : '未执行'}</strong></div>
          <div><MonitorPlay /><span>真机视频</span><strong>{firmware.data.real_video_verified ? '已读取验证' : '未验证'}</strong></div>
        </div>}
        <p className="physical-note"><AlertTriangle />{physicalNote}</p>
        {firmware.data.compile_passed && <div className="firmware-build-evidence"><dl><div><dt>固件版本</dt><dd>{firmware.data.firmware_version || '未知'}</dd></div><div><dt>构建时间</dt><dd>{formatTime(firmware.data.built_at)}</dd></div><div><dt>PlatformIO</dt><dd>{firmware.data.platformio_core || '未知'}</dd></div><div><dt>Arduino ESP32</dt><dd>{firmware.data.framework || '未知'}</dd></div><div><dt>目标板</dt><dd>{firmware.data.target_board || 'AI Thinker ESP32-CAM'}</dd></div><div><dt>RAM / Flash</dt><dd>{firmware.data.ram_usage ? `${firmware.data.ram_usage.percent}%` : '—'} / {firmware.data.flash_usage ? `${firmware.data.flash_usage.percent}%` : '—'}</dd></div></dl><div className="artifact-list">{firmware.data.artifacts?.map((artifact) => <div key={artifact.name}><div><strong>{artifact.name}</strong><span>{artifact.path}</span></div><code title={artifact.sha256}>{artifact.sha256}</code><Badge tone={artifact.hash_verified === true ? 'success' : artifact.hash_verified === false ? 'danger' : 'neutral'}>{artifact.hash_verified === true ? 'SHA-256 已核验' : artifact.hash_verified === false ? '哈希不符' : '哈希未核验'}</Badge></div>)}</div><small>清单：{firmware.data.manifest_path || 'artifacts/firmware/manifest.json'}</small></div>}
      </section>

      {runtime.mode === 'DEMO' && <section className="virtual-device-panel panel">
        <div className="virtual-device-copy"><div className="device-icon"><Cpu /></div><div><p className="eyebrow">无开发板也能真实验收协议</p><h2>Virtual ESP32-CAM</h2><p>虚拟设备使用与真机相同的一次性配对、设备令牌、心跳和 MJPEG 协议，全程标记“虚拟设备 · 非真实硬件”。</p></div></div>
        <div className="virtual-device-state"><Badge tone={virtual.data.running ? virtual.data.backend_online ? 'success' : 'warning' : 'neutral'}>{virtual.data.running ? virtual.data.backend_online ? '虚拟设备在线' : '虚拟设备启动中' : '虚拟设备未运行'}</Badge><span>{virtual.data.device_id || '尚未创建 device_id'}</span><small>{virtual.data.running ? `${virtual.data.source === 'webcam' ? '真实电脑摄像头源' : '测试视频源'} · ${virtual.data.stage || 'starting'} · 心跳 ${formatTime(virtual.data.last_heartbeat)}` : '停止后等待 45 秒心跳超时，后台才会按真实规则标离线。'}</small>{virtual.data.error && <span className="danger-text">{virtual.data.error}</span>}</div>
        <div className="virtual-device-controls"><select aria-label="虚拟设备视频来源" disabled={virtual.data.running || virtualBusy} value={virtualSource} onChange={(event) => setVirtualSource(event.target.value as 'video' | 'webcam')}><option value="video">测试视频（推荐）</option><option value="webcam">电脑摄像头索引 0</option></select><button className={`button ${virtual.data.running ? 'secondary' : 'primary'}`} disabled={virtualBusy} onClick={() => void toggleVirtual()}>{virtualBusy ? <LoaderCircle className="spin" /> : virtual.data.running ? <Square /> : <Play />}{virtualBusy ? '正在处理…' : virtual.data.running ? '停止虚拟设备' : '启动虚拟设备'}</button>{virtual.data.camera_id && <Link className="button ghost" to={`/live?camera=${virtual.data.camera_id}`}><Camera />查看虚拟视频</Link>}</div>
      </section>}
      <div className="device-install-layout">
        <section className="panel install-card">
          <div className="section-heading"><div><p className="eyebrow">一键安装并绑定</p><h2>{usbBoardLabels[boardModel]}</h2></div><Badge tone={ports.data.platformio.available ? 'success' : 'warning'}>{platformLabel}</Badge></div>
          <Field label="开发板型号" hint="已绑定的板卡自动恢复对应型号；新板请按板上标识选择，USB 串口出现不代表板型已验证。"><select value={boardModel} disabled={Boolean(activeJob)} onChange={(event) => { boardManuallySelected.current = true; setBoardModel(event.target.value as UsbBoardModel) }}>{Object.entries(usbBoardLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
          <FirmwareCompatibility />
          <ol className="install-flow"><li className="active"><Usb /><div><strong>检测 USB 串口</strong><span>只显示系统枚举的端口</span></div></li><li><Download /><div><strong>编译并刷入专用固件</strong><span>板型分别校验，不通刷</span></div></li><li><Wifi /><div><strong>串口安全配网</strong><span>敏感字段不会写入日志</span></div></li><li><ShieldCheck /><div><strong>一次性配对与绑定</strong><span>设备令牌只在签发时返回一次</span></div></li></ol>
          {ports.error ? <ErrorState message={ports.error} onRetry={ports.reload} /> : <form className="install-form" onSubmit={install}>
            {!realHardwareMode && <div className="inline-banner info"><AlertTriangle />当前 {runtime.mode} 模式不允许 USB 刷写、安装或物理设备配网，也不会接触物理串口。请切换到 REAL 模式并重启服务；固件编译仍可独立执行。</div>}
            <Field label="USB 串口" hint={portsPending ? '正在检测这台电脑的 USB 串口…' : eligiblePorts.length ? '请选择当前板卡或下载底板对应的端口。' : '当前没有检测到可刷写硬件；可以先单独编译固件。'}><select value={form.port} onChange={(event) => setForm((value) => ({ ...value, port: event.target.value }))}><option value="">{portsPending ? '检测中…' : eligiblePorts.length ? '请选择 USB 串口' : '未检测到可用端口'}</option>{ports.data.ports.map((port) => <option key={port.device} value={port.device} disabled={port.eligible === false}>{port.device} · {port.description || '串口'}{port.eligible === false ? `（${port.rejection_reason || '不可用'}）` : ''}</option>)}</select></Field>
            <div className="form-grid"><Field label="Wi-Fi 名称"><input autoComplete="off" value={form.ssid} onChange={(event) => setForm((value) => ({ ...value, ssid: event.target.value }))} placeholder="家庭 Wi-Fi" required /></Field><Field label="Wi-Fi 密码" hint="开放网络可留空；提交后立即从页面清除。"><input type="password" autoComplete="new-password" value={form.password} onChange={(event) => setForm((value) => ({ ...value, password: event.target.value }))} /></Field><Field label="设备名称"><input value={form.device_name} onChange={(event) => setForm((value) => ({ ...value, device_name: event.target.value }))} required /></Field><Field label="房间名称"><input value={form.room_name} onChange={(event) => setForm((value) => ({ ...value, room_name: event.target.value }))} required /></Field></div>
            <button className="advanced-toggle" type="button" onClick={() => setAdvanced((value) => !value)}><ChevronDown className={advanced ? 'rotated' : ''} />高级设置</button>{advanced && <Field label="物忆后台地址" hint="ESP32 必须能通过局域网访问这个地址，不能使用 127.0.0.1 或 localhost。"><input type="url" value={form.backend_url} onChange={(event) => { backendManuallyEdited.current = true; setForm((value) => ({ ...value, backend_url: event.target.value })) }} required /></Field>}
            <p className={`lan-address-note ${lanEnabled && form.backend_url ? '' : 'danger-text'}`} role="status"><Router />{lanMessage}</p>
            <DeviceNetworkCheck mode={runtime.mode} ssid={form.ssid} onDetectedSsid={onDetectedSsid} />
            {eligiblePorts.length > 1 && !form.port && <p role="status">检测到多个可用 USB 串口，请选择这块板对应的端口，系统不会替你猜选。</p>}
            {!portsPending && eligiblePorts.length === 0 && <div className="inline-banner info"><Usb />{realHardwareMode ? '未发现可用 USB 串口；可以先编译固件，或检查可传数据的 USB 线和板卡连接。REAL 模式不运行虚拟设备。' : runtime.mode === 'DEMO' ? '当前为 DEMO 模式，可在上方运行虚拟设备；这不代表已连接物理开发板。' : '当前为 TEST 模式，不接触物理串口；固件编译可独立执行。'}</div>}
            {!portsPending && eligiblePorts.length > 0 && !firmware.data.physical_board_detected && <div className="inline-banner info"><AlertTriangle />已发现 USB 串口，但串口枚举不能证明具体板型；还需受控身份核验，摄像头视频另行读取验证。</div>}
            <div className="hardware-truth"><AlertTriangle /><span>刷写过程中请保持 USB 连接和稳定 5V 供电。固件写入校验、串口配网与后台绑定分别显示结果；视频还需读取实际帧验证。</span></div>
            {boardModel === 'ai_thinker_esp32cam' ? <button className="button primary full install-button" disabled={!realHardwareMode || !lanEnabled || !form.port || !form.ssid || !form.backend_url || !form.device_name || !form.room_name || Boolean(activeJob)}><Wrench />{activeJob ? '安装任务进行中…' : '一键安装并绑定'}</button> : <p role="status">XIAO 使用下方“绑定并开始自动安装”：先核对 ESP32-S3、8 MB 闪存与专用固件，再进行写入。不能使用 AI Thinker 固件。</p>}
          </form>}
          <div className="fallback-provision"><button className="fallback-heading" type="button" onClick={() => setFallbackOpen((value) => !value)}><Router /><span><strong>备用热点网络申领</strong><small>仅建立待验证记录；仍需本机 USB 串口 OMREADY 验证</small></span><ChevronDown className={fallbackOpen ? 'rotated' : ''} /></button>{fallbackOpen && <div className="fallback-body"><p>该入口只能建立网络申领记录，不能证明物理硬件，也不能生成 REAL 确认事件。完成网络配置后，仍需在本机通过 USB 串口收到 OMREADY 并完成验证，才能标记为物理设备。</p>{enrollment && enrollment.expires_in > 0 ? <div className="pairing-code"><span>一次性配对码</span><strong>{enrollment.pairing_code}</strong><small>{Math.floor(enrollment.expires_in / 60)}:{String(enrollment.expires_in % 60).padStart(2, '0')} 后过期 · 只可使用一次</small></div> : <button className="button secondary full" type="button" disabled={!realHardwareMode || !lanEnabled || !form.device_name || !form.room_name} onClick={() => void createFallbackEnrollment()}><ShieldCheck />生成 10 分钟配对码</button>}<dl><div><dt>后台地址</dt><dd>{form.backend_url || '尚未检测到局域网地址'}</dd></div><div><dt>申领信息</dt><dd>{form.room_name} · {form.device_name}</dd></div></dl><small>配对码不会写入日志或浏览器持久存储；页面刷新后需重新生成。网络申领不会自动升级为物理设备。</small></div>}</div>
          <AutoUsbSetup mode={runtime.mode} lanEnabled={lanEnabled} busy={Boolean(activeJob)} form={form} boardModel={boardModel} onBoundBoard={onBoundBoard} onJob={setJob} onAccepted={() => setForm((value) => ({ ...value, password: '' }))} />
          <div className="build-only"><div><Code2 /><span><strong>没有开发板？</strong>仍可真实编译所选型号的固件并检查工具链。</span></div><button className="button secondary small" disabled={Boolean(activeJob)} onClick={() => void createJob('/api/firmware/build', boardModel === 'ai_thinker_esp32cam' ? {} : { board_model: boardModel })}>只编译固件</button></div>
        </section>

        <aside className="panel job-panel">
          <div className="section-heading"><div><p className="eyebrow">任务日志</p><h2>{job ? job.job_type === 'install' ? '安装与绑定' : job.job_type || '固件任务' : '等待任务'}</h2></div>{job && <Badge tone={job.status === 'succeeded' ? 'success' : job.status === 'failed' ? 'danger' : 'warning'}>{job.status === 'succeeded' ? '完成' : job.status === 'failed' ? '失败' : job.status === 'running' ? '进行中' : '排队中'}</Badge>}</div>
          {!job ? <div className="job-empty"><TerminalSquare /><strong>任务日志会显示在这里</strong><span>密码、完整配对码和设备令牌会被脱敏。</span></div> : <><div className="job-progress"><div style={{ width: `${Math.max(2, Math.min(100, job.progress || 0))}%` }} /><span>{job.progress || 0}%</span></div><div className="job-stage">{job.status === 'running' && <LoaderCircle className="spin" />}{job.status === 'succeeded' && <CheckCircle2 />}{job.status === 'failed' && <CircleOff />}<strong>{job.stage || (job.status === 'queued' ? '等待执行' : job.status)}</strong></div><div className="terminal" ref={logRef}>{logLines.length ? logLines.map((line, index) => <div key={`${index}-${line}`}><span>{String(index + 1).padStart(2, '0')}</span><code>{line}</code></div>) : <p>等待第一条日志…</p>}</div>{job.error && <div className="inline-banner danger"><AlertTriangle />{job.error}</div>}{job.status === 'succeeded' && job.result && <div className="job-result"><CheckCircle2 /><div><strong>{job.job_type === 'build' ? '固件编译任务已完成' : '设备任务已完成'}</strong><span>{String(job.result.message || (job.job_type === 'build' ? '固件产物已生成；这不代表已刷入物理开发板。' : '设备已经完成日志中列出的受控步骤。'))}</span></div></div>}</>}
        </aside>
      </div>

      <section className="devices-section">
        <div className="section-heading"><div><p className="eyebrow">设备记录</p><h2>摄像头节点与待验证申领</h2></div><span className="muted">45 秒没有心跳会自动标记离线</span></div>
        {devices.loading ? <Loading /> : devices.error ? <ErrorState message={devices.error} onRetry={devices.reload} /> : devices.data.length === 0 ? <EmptyState icon={<RadioTower />} title="还没有设备记录" description="网络申领只建立待验证记录；只有完成本机 USB 串口 OMREADY 验证后才会绑定为物理设备。" /> : <div className="firmware-device-list">{devices.data.map((device) => {
          const key = device.id || device.device_id
          const open = expandedDevice === key
          const hardwareCategory = deviceHardwareCategory(device)
          const isVirtual = hardwareCategory === 'virtual'
          const isPhysical = hardwareCategory === 'physical'
          const hardwareLabel = isVirtual ? '虚拟设备 · 非真实硬件' : isPhysical ? '物理设备' : '设备类型未验证'
          const onlineLabel = isVirtual
            ? (device.online ? '虚拟在线' : '虚拟离线')
            : isPhysical
              ? (device.online ? '物理设备在线' : '物理设备离线')
              : (device.online ? '在线 · 类型未验证' : '离线 · 类型未验证')
          return <article className={`firmware-device panel ${open ? 'open' : ''}`} key={key}>
            <button className="device-summary" onClick={() => setExpandedDevice(open ? null : key)}>
              <div className={`device-icon ${device.online ? 'online' : ''}`}><RadioTower /></div>
              <div className="device-identity"><strong>{device.display_label || device.device_name || device.name || device.device_id}</strong><span>{device.room_name || '未分配房间'} · {device.ip_address || '等待 IP'}</span></div>
              <div className="device-signal"><Signal /><span>{device.wifi_rssi !== undefined ? `${device.wifi_rssi} dBm` : '—'}</span></div>
              {isVirtual && <Badge tone="warning">虚拟设备 · 非真实硬件</Badge>}
              {!isVirtual && !isPhysical && <Badge tone="danger">设备类型未验证</Badge>}
              <Badge tone={isVirtual || !isPhysical ? 'warning' : device.online ? 'success' : 'danger'}>{onlineLabel}</Badge>
              <ChevronRight className={open ? 'rotated' : ''} />
            </button>
            {open && <div className="device-details">
              <dl><div><dt>硬件类型</dt><dd>{hardwareLabel}</dd></div><div><dt>最后心跳</dt><dd>{formatTime(device.last_heartbeat)}</dd></div><div><dt>固件版本</dt><dd>{device.firmware_version || '未知'}</dd></div><div><dt>MAC 地址</dt><dd>{device.mac_address || '未知'}</dd></div><div><dt>Wi-Fi 信号</dt><dd>{device.wifi_rssi !== undefined ? `${device.wifi_rssi} dBm` : '未知'}</dd></div><div><dt>剩余内存</dt><dd>{device.free_heap ? `${Math.round(device.free_heap / 1024)} KB` : '未知'}</dd></div><div><dt>当前 FPS</dt><dd>{device.current_fps ?? device.fps ?? '—'}</dd></div><div><dt>摄像头</dt><dd>{device.camera_status || '未知'}</dd></div><div><dt>视频流</dt><dd>{device.stream_status || '未知'}</dd></div></dl>
              {isVirtual && <div className="inline-banner info"><Cpu />虚拟设备 · 非真实硬件：它验证注册、令牌、心跳和 MJPEG 协议，但不代表 ESP32-CAM 已刷写。</div>}
              {!isVirtual && !isPhysical && <div className="inline-banner danger"><AlertTriangle />后台未同时提供 simulated=false 与明确物理硬件标识，不能把该设备称为物理 ESP32-CAM。</div>}
              {(device.recent_error || device.last_error) && <div className="inline-banner danger"><AlertTriangle />{device.recent_error || device.last_error}</div>}
              <div className="device-actions">
                {device.camera_id && (isVirtual || isPhysical) && <Link className="button primary small" to={`/live?camera=${device.camera_id}`}><Camera />查看{isVirtual ? '虚拟' : '实时'}画面</Link>}
                <button className="button secondary small" onClick={() => command(device, 'reboot')} disabled={!device.online || !isPhysical}><RotateCcw />重启设备</button>
                <button className="button ghost small" onClick={() => { setForm((value) => ({ ...value, device_name: device.device_name || value.device_name, room_name: device.room_name || value.room_name })); window.scrollTo({ top: 0, behavior: 'smooth' }) }} disabled={!isPhysical}><Router />USB 重新配网</button>
                <button className="button ghost small danger-text" onClick={() => command(device, 'factory_reset')} disabled={!device.online || !isPhysical}><HardDrive />恢复出厂</button>
                <button className="button ghost small danger-text" onClick={() => revoke(device)} disabled={device.token_revoked}><Trash2 />{device.token_revoked ? '绑定已撤销' : '撤销绑定'}</button>
              </div>
              <div className="ota-note"><Cpu /><span>首次安装使用 USB。当前固件尚未启用经验证的 OTA，因此这里不会显示虚假的升级成功。</span></div>
            </div>}
          </article>
        })}</div>}
      </section>
    </div>
  )
}
