import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, RotateCcw, ShieldCheck, Usb, Wifi } from 'lucide-react'
import { api } from '../lib/api'
import { useVisiblePolling } from '../hooks/useVisiblePolling'
import type { FirmwareJob } from '../types'

export type UsbBoardModel = 'ai_thinker_esp32cam' | 'xiao_esp32s3_sense'
export const usbBoardLabels: Record<UsbBoardModel, string> = {
  ai_thinker_esp32cam: 'AI Thinker ESP32-CAM',
  xiao_esp32s3_sense: 'Seeed XIAO ESP32S3 Sense',
}
interface InstallDetails { port: string; ssid: string; password: string; backend_url: string; device_name: string; room_name: string }
interface UsbStatus {
  runtime_mode: string; enabled: boolean; message: string; credentials_retained?: boolean
  binding?: { board_model: string; firmware_version: string; manifest_sha256: string; mac_address?: string; authorized_at?: string; bridge: { serial_number?: string; location?: string } } | null
  receipt?: { status: string; flashed_at?: string | null; linked_at?: string | null; error?: string | null; job_id?: string | null } | null
  recovery?: { state: string; allowed_actions: string[]; binding_authorized_at?: string | null; message: string }
}
type RecoveryAction = 'reprovision' | 'retry_install'

export function AutoUsbSetup({ mode, lanEnabled, busy, form, onJob, onAccepted, onBoundBoard, boardModel = 'ai_thinker_esp32cam' }: {
  mode: string; lanEnabled: boolean; busy: boolean; form: InstallDetails; onJob: (job: FirmwareJob) => void; onAccepted?: () => void; onBoundBoard?: (board: UsbBoardModel) => void; boardModel?: UsbBoardModel
}) {
  const [status, setStatus] = useState<UsbStatus | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false)
  const [overwriteConfirmed, setOverwriteConfirmed] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(true)
  const generation = useRef(0)
  const requestPending = useRef(false)
  const consumedRevision = useRef<string | null>(null)
  const lastJob = useRef<string | null>(null)
  const currentMode = useRef(mode)
  currentMode.current = mode
  useEffect(() => {
    alive.current = true
    generation.current++
    setStatus(null); setConfirmed(false); setRecoveryConfirmed(false); setOverwriteConfirmed(false); setError(''); setPending(requestPending.current)
    lastJob.current = null
    return () => { alive.current = false; generation.current++ }
  }, [mode])
  useEffect(() => { setConfirmed(false); setRecoveryConfirmed(false); setOverwriteConfirmed(false); generation.current++ }, [form.port, boardModel])
  useEffect(() => { setRecoveryConfirmed(false); setOverwriteConfirmed(false) }, [form.ssid, form.password, form.backend_url, status?.binding?.authorized_at, status?.binding?.manifest_sha256, status?.binding?.mac_address])
  useVisiblePolling(async (signal) => {
    if (requestPending.current) return
    const pollGeneration = generation.current
    try {
      const next = await api<UsbStatus>('/api/firmware/auto-usb', { signal })
      if (signal.aborted || !alive.current || pollGeneration !== generation.current || next.runtime_mode !== currentMode.current) return
      setStatus(next); setError('')
      if (next.enabled && (next.binding?.board_model === 'ai_thinker_esp32cam' || next.binding?.board_model === 'xiao_esp32s3_sense')) onBoundBoard?.(next.binding.board_model)
      if (next.receipt?.job_id && next.receipt.job_id !== lastJob.current) {
        const job = await api<FirmwareJob>(`/api/firmware/jobs/${encodeURIComponent(next.receipt.job_id)}`, { signal })
        if (signal.aborted || !alive.current || pollGeneration !== generation.current) return
        lastJob.current = next.receipt.job_id
        onJob(job)
      }
    } catch (value) { if (!signal.aborted) setError(value instanceof Error ? value.message : '自动接板状态读取失败') }
  }, { enabled: mode === 'REAL', immediate: true, intervalMs: 3000, resetKey: mode })
  const matchesMode = status?.runtime_mode === mode
  const eligible = mode === 'REAL' && lanEnabled && confirmed && form.port && form.ssid && form.backend_url && form.device_name && form.room_name && !busy && !pending
  const binding = matchesMode ? status.binding : null
  const recovery = matchesMode && status.enabled && binding?.board_model === boardModel ? status.recovery : null
  const revision = binding ? `${binding.board_model}:${binding.mac_address}:${binding.manifest_sha256}:${binding.authorized_at}` : ''
  const recoveryFieldsReady = Boolean(mode === 'REAL' && lanEnabled && !busy && !pending && form.port && form.ssid && form.backend_url
    && binding?.mac_address && binding.authorized_at && recovery?.binding_authorized_at === binding.authorized_at
    && consumedRevision.current !== revision && recoveryConfirmed)
  const canRecover = (action: RecoveryAction) => Boolean(recoveryFieldsReady && recovery?.allowed_actions.includes(action)
    && (action !== 'retry_install' || overwriteConfirmed))
  async function bind() {
    if (!eligible || requestPending.current) return
    requestPending.current = true
    const requestGeneration = ++generation.current
    setPending(true); setError('')
    try {
      const job = await api<FirmwareJob>('/api/firmware/auto-usb/bind', { method: 'POST', json: {
        ...form, board_model: boardModel, authorize_fixed_firmware: true,
      } })
      if (alive.current) onAccepted?.()
      if (!alive.current || requestGeneration !== generation.current) return
      setConfirmed(false); onJob(job)
    } catch (value) { if (alive.current && requestGeneration === generation.current) setError(value instanceof Error ? value.message : '绑定失败；没有自动刷写') }
    finally { requestPending.current = false; if (alive.current) setPending(false) }
  }
  async function recover(action: RecoveryAction) {
    if (!canRecover(action) || requestPending.current || !binding) return
    requestPending.current = true
    const requestGeneration = ++generation.current
    setPending(true); setError('')
    try {
      const job = await api<FirmwareJob>('/api/firmware/auto-usb/recover', { method: 'POST', json: {
        action, port: form.port, board_model: boardModel, mac_address: binding.mac_address,
        manifest_sha256: binding.manifest_sha256, binding_authorized_at: binding.authorized_at,
        ssid: form.ssid, password: form.password, backend_url: form.backend_url,
        authorize_recovery: true, authorize_overwrite: action === 'retry_install',
      } })
      consumedRevision.current = revision
      if (alive.current) onAccepted?.()
      if (!alive.current || requestGeneration !== generation.current) return
      setRecoveryConfirmed(false); setOverwriteConfirmed(false); onJob(job)
    } catch (value) {
      if (alive.current && requestGeneration === generation.current) {
        setRecoveryConfirmed(false); setOverwriteConfirmed(false)
        setError(value instanceof Error ? value.message : '恢复请求未接受；不会自动重试')
      }
    } finally { requestPending.current = false; if (alive.current) setPending(false) }
  }
  async function disable() {
    if (requestPending.current || mode !== 'REAL') return
    requestPending.current = true
    const requestGeneration = ++generation.current
    setPending(true)
    try {
      const next = await api<UsbStatus>('/api/firmware/auto-usb/disable', { method: 'POST' })
      if (alive.current && requestGeneration === generation.current && next.runtime_mode === mode) setStatus(next)
    } catch (value) { if (alive.current && requestGeneration === generation.current) setError(value instanceof Error ? value.message : '停用失败') }
    finally { requestPending.current = false; if (alive.current) setPending(false) }
  }
  return <section className="fallback-provision" aria-label="绑定板卡自动接入">
    <h3><Usb size={18} /> 绑定这块板，以后插入自动接入</h3>
    <p>默认关闭。沿用上方网络与端口设置，点击授权后先只读核验 ESP32 芯片，再自动安装本次确认的固定固件并配网，无需再次拔插。</p>
    <p>当前目标：{usbBoardLabels[boardModel]}。两种板卡使用独立固件与芯片校验；未知板型、换板、接入多个串口均不自动擦写。无序列号的下载底板需保持同一 USB 插口。</p>
    {mode !== 'REAL' ? <p role="status">当前模式不允许接触物理串口。</p> : <>
      <label className="checkbox-row"><input type="checkbox" checked={confirmed} disabled={pending || busy} onChange={(event) => setConfirmed(event.target.checked)} />
        我已确认所选 {form.port || 'USB 端口'} 后是 {usbBoardLabels[boardModel]}，同意只为这一块板绑定并启用固定固件自动刷写。将覆盖这块板原有固件，请确认没有需要保留的程序；安装后会启动物忆摄像头应用。
      </label>
      <p><small>Wi-Fi 信息仅暂存于本次后台内存；刷新页面不重复刷写，后台重启后未完成配网需重新输入。该流程尚需真实开发板验收。</small></p>
      <div className="button-row"><button type="button" className="button secondary" disabled={!eligible} onClick={() => void bind()}><ShieldCheck size={16} />{pending ? '正在处理…' : '绑定并开始自动安装'}</button>
        {matchesMode && status.enabled && <button type="button" className="button ghost" disabled={pending} onClick={() => void disable()}>停用自动接板</button>}</div>
      {matchesMode && <p role="status">{status.message}</p>}
      {matchesMode && status.enabled && status.credentials_retained === false && !status.receipt?.linked_at && <p role="status">后台未保留 Wi-Fi 密码。请重新填写上方密码，{status.receipt ? '再使用“恢复这块已绑定设备”；已有刷写收据时只重新配网。' : '再使用首次绑定安装，当前尚无安装收据。'}</p>}
      {matchesMode && status.binding && <small>已锁定固件 {status.binding.firmware_version} · SHA-256 {status.binding.manifest_sha256.slice(0, 12)}… · {status.receipt?.flashed_at ? '有该板该版本刷写收据' : '尚无该板真机刷写收据'}{status.receipt?.linked_at ? ' · 串口绑定已有证据；视频以实际帧为准' : ''}</small>}
      {binding && binding.board_model !== boardModel && <p role="status">恢复前请将开发板型号切换为已绑定的 {usbBoardLabels[binding.board_model as UsbBoardModel] || '已登记板型'}；不能用另一板型的固件恢复。</p>}
      {recovery && <div className="fallback-body" role="group" aria-label="恢复已绑定设备">
        <h4><RotateCcw size={16} /> 恢复这块已绑定设备</h4>
        <p role="status">{recovery.message}</p>
        {recovery.allowed_actions.some((action) => action === 'reprovision' || action === 'retry_install') && <>
          <p>使用上方填写的 Wi-Fi 和后台地址。仅作用于已绑定板卡及锁定的固件；需要换板或换固件时，请重新绑定。</p>
          <label className="checkbox-row"><input type="checkbox" checked={recoveryConfirmed} disabled={pending || busy} onChange={(event) => { setRecoveryConfirmed(event.target.checked); setOverwriteConfirmed(false) }} />
            我确认恢复的是已绑定的 {usbBoardLabels[boardModel]}，并授权使用上方网络设置恢复一次。
          </label>
          {recovery.allowed_actions.includes('retry_install') && <>
            <p className="hardware-truth"><AlertTriangle size={16} /><span>上次写入结果不确定。覆盖重试可能替换板上现有程序；失败后仍不会自动重复刷写。</span></p>
            <label className="checkbox-row"><input type="checkbox" checked={overwriteConfirmed} disabled={!recoveryConfirmed || pending || busy} onChange={(event) => setOverwriteConfirmed(event.target.checked)} />
              我明确同意覆盖当前板上的程序，只重试这一个固定固件版本一次。
            </label>
          </>}
          <div className="button-row">
            {recovery.allowed_actions.includes('reprovision') && <button type="button" className="button secondary" disabled={!canRecover('reprovision')} onClick={() => void recover('reprovision')}><Wifi size={16} />只重新配网，不重刷</button>}
            {recovery.allowed_actions.includes('retry_install') && <button type="button" className="button secondary danger-text" disabled={!canRecover('retry_install')} onClick={() => void recover('retry_install')}><RotateCcw size={16} />授权覆盖并重试一次</button>}
          </div>
        </>}
      </div>}
      {matchesMode && status.receipt?.error && <p role="alert" className="danger-text">{status.receipt.error}</p>}
      {error && <p role="alert" className="danger-text">{error}</p>}
    </>}
  </section>
}
