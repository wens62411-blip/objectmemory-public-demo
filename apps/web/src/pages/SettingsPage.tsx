import { Database, Eye, EyeOff, FlaskConical, Gauge, HardDrive, PlayCircle, RefreshCw, Save, ScanLine, ShieldCheck, Video } from 'lucide-react'
import { useEffect, useLayoutEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Badge, ErrorState, Loading, PageHeader } from '../components/UI'
import { useRuntime } from '../contexts/RuntimeContext'
import { useApi } from '../hooks/useApi'
import { api } from '../lib/api'
import type { Settings } from '../types'
import { useToast } from '../components/Toast'
import { MobileAccess, safeMobileUrl } from '../components/MobileAccess'

const defaults: Settings = { detection_mode: 'aruco', inference_fps: 5, static_seconds: 2, pre_seconds: 5, post_seconds: 5, retention_days: 7, save_clips: true, hand_detection_enabled: true, show_hands: true, privacy_mode: true, record_events: true }
interface SessionInfo { local: boolean; pairing_pin?: string; lan_urls?: string[]; lan_enabled?: boolean; listener_verified?: boolean }

export function SettingsPage() {
  const settings = useApi<Settings>('/api/settings', defaults)
  const session = useApi<SessionInfo>('/api/session', { local: false })
  const [form, setForm] = useState<Settings>(defaults)
  const [busy, setBusy] = useState(false)
  const [phonePurpose, setPhonePurpose] = useState<'/' | '/companion'>('/')
  const [phoneHost, setPhoneHost] = useState('')
  const [showPin, setShowPin] = useState(false)
  const { notify } = useToast()
  const runtime = useRuntime()
  const location = useLocation()
  const lanUrls = !session.error && session.data.listener_verified && session.data.lan_enabled
    ? (session.data.lan_urls || []).map((url) => safeMobileUrl(url)).filter(Boolean) : []
  const activePhoneHost = lanUrls.includes(phoneHost) ? phoneHost : lanUrls[0]
  // Hydrate before the newly loaded form is interactive. A deferred effect
  // could overwrite a user's first fast toggle with the server's old value.
  useLayoutEffect(() => setForm({ ...settings.data, hand_detection_enabled: settings.data.hand_detection_enabled !== false }), [settings.data])
  useEffect(() => {
    if (!settings.loading && location.hash === '#mobile-access') document.getElementById('mobile-access')?.scrollIntoView?.({ block: 'start' })
  }, [settings.loading, location.hash])
  useEffect(() => { if (session.error || !session.data.local) setShowPin(false) }, [session.error, session.data.local])

  async function save() {
    setBusy(true)
    // GET also contains provenance and advanced thresholds. Only send fields
    // owned by this form; never echo server metadata or overwrite hidden values.
    const editable = Object.fromEntries((Object.keys(defaults) as Array<keyof Settings>).map((key) => [key, form[key]]))
    try { const next = await api<Settings>('/api/settings', { method: 'PATCH', json: editable }); settings.setData(next); notify('设置已保存', 'success') }
    catch (value) { notify(value instanceof Error ? value.message : '设置保存失败', 'danger') }
    finally { setBusy(false) }
  }
  async function seed() {
    if (runtime.mode !== 'DEMO') { notify('演示种子只允许在 DEMO 模式生成。', 'danger'); return }
    try { const response = await api<{ message?: string }>('/api/system/demo-seed', { method: 'POST' }); notify(response.message || '演示数据已准备', 'success') }
    catch (value) { notify(value instanceof Error ? value.message : '演示数据生成失败', 'danger') }
  }

  if (settings.loading) return <Loading label="正在读取设置…" />
  if (settings.error) return <ErrorState message={settings.error} onRetry={settings.reload} />
  return (
    <div>
      <PageHeader title="让物忆，更适合你的家" description="连接手机、选择识别方式，也决定哪些记忆值得留下。" actions={<button className="button primary" onClick={save} disabled={busy}><Save />{busy ? '正在保存…' : '保存设置'}</button>} />
      <section id="mobile-access" className="panel settings-phone-section" aria-label="连接手机">
        <div className="phone-purpose-row"><div className="phone-purpose-options" aria-label="手机使用方式"><button type="button" className={phonePurpose === '/' ? 'selected' : ''} aria-pressed={phonePurpose === '/'} onClick={() => setPhonePurpose('/')}>手机查看物品</button><button type="button" className={phonePurpose === '/companion' ? 'selected' : ''} aria-pressed={phonePurpose === '/companion'} onClick={() => setPhonePurpose('/companion')}>让这部手机可响铃</button></div><button type="button" className="button ghost small" onClick={() => void session.reload()} disabled={session.loading}><RefreshCw />刷新地址</button></div>
        {session.error && <p className="inline-error" role="alert">未能更新主机连接信息，请确认电脑仍在线后重试。旧地址和访问码已隐藏。</p>}
        {lanUrls.length > 1 && <label className="field phone-network-select"><span className="field-label">电脑连接了多个网络，请选择手机所在网络</span><select value={activePhoneHost} onChange={(event) => setPhoneHost(event.target.value)}>{lanUrls.map((url) => <option key={url} value={url}>{new URL(url).host}</option>)}</select></label>}
        <MobileAccess url={safeMobileUrl(activePhoneHost, phonePurpose)} />
        {!session.error && session.data.local && session.data.pairing_pin && <div className="phone-access-code"><div><strong>电脑上的 8 位访问码</strong><span>只分享给信任的家庭成员，不会写进二维码。</span></div><output aria-label="主机访问码">{showPin ? session.data.pairing_pin : '•••• ••••'}</output><button type="button" className="button secondary small" aria-pressed={showPin} onClick={() => setShowPin((value) => !value)}>{showPin ? <EyeOff /> : <Eye />}{showPin ? '隐藏访问码' : '查看访问码'}</button></div>}
        {!session.error && !session.data.local && <p className="phone-code-location"><ShieldCheck />访问码仅在运行物忆的电脑上显示。如果已连接，这部手机不需要重复输入。</p>}
        {!session.loading && !session.error && !lanUrls.length && <p className="phone-launch-help">在电脑项目目录运行 <code>powershell -ExecutionPolicy Bypass -File scripts/start.ps1 -Lan</code> 开启带访问码的局域网模式；先正常停止旧服务，避免端口占用。</p>}
      </section>
      <div className="settings-layout">
        <section className="panel settings-section"><div className="settings-title"><div className="metric-icon green"><ScanLine /></div><div><h2>选择现场策略</h2></div></div><div className="mode-options" role="group" aria-label="识别方式"><button aria-pressed={form.detection_mode === 'aruco'} className={form.detection_mode === 'aruco' ? 'selected' : ''} onClick={() => setForm((value) => ({ ...value, detection_mode: 'aruco' }))}><ScanLine /><div><strong>稳定标签模式</strong><span>使用 OpenCV ArUco，适合早期验证与现场稳定演示。</span></div><Badge tone="success">推荐</Badge></button><button aria-pressed={form.detection_mode === 'experimental'} className={form.detection_mode === 'experimental' ? 'selected' : ''} onClick={() => setForm((value) => ({ ...value, detection_mode: 'experimental' }))}><FlaskConical /><div><strong>无标签 AI 实验模式</strong><span>相似物品、遮挡和低光可能误判；不会阻塞标签模式。</span></div><Badge tone="warning">实验</Badge></button></div></section>

        <section className="panel settings-section"><div className="settings-title"><div className="metric-icon blue"><Gauge /></div><div><h2>日常运行参数</h2></div></div><div className="setting-rows"><label><div><strong>物品识别频率</strong><span>每秒分析几次物品，不是画面帧率；预览独立更新，实测帧率请在摄像头页面查看。</span></div><div className="range-control"><input type="range" aria-label="物品识别频率" min={1} max={15} value={form.inference_fps} onChange={(event) => setForm((value) => ({ ...value, inference_fps: Number(event.target.value) }))} /><output>{form.inference_fps} 次/秒</output></div></label><label><div><strong>确认放下等待</strong><span>物品稳定后仍需满足完整移动与证据条件，才会保存确认事件。</span></div><div className="range-control"><input type="range" aria-label="确认放下等待" min={1.5} max={5} step={0.1} value={form.static_seconds} onChange={(event) => setForm((value) => ({ ...value, static_seconds: Number(event.target.value) }))} /><output>{form.static_seconds} 秒</output></div></label><label className="toggle-row"><div><strong>启用本地手部分析</strong><span>分析几何手势和手物共同运动；没有注册照片也可查看手部反馈。不是物品拿放结论。</span></div><input type="checkbox" aria-label="启用本地手部分析" checked={form.hand_detection_enabled !== false} onChange={(event) => setForm((value) => ({ ...value, hand_detection_enabled: event.target.checked }))} /></label><label className="toggle-row"><div><strong>显示手部关键点</strong><span>只控制识别帧上的关键点显示，关闭后仍可继续分析手部动作。</span></div><input type="checkbox" aria-label="显示手部关键点" checked={form.show_hands} onChange={(event) => setForm((value) => ({ ...value, show_hands: event.target.checked }))} /></label></div></section>

        <section className="panel settings-section"><div className="settings-title"><div className="metric-icon coral"><Video /></div><div><h2>只保存关键前后片段</h2></div></div><div className="setting-rows"><label className="toggle-row"><div><strong>保存新的事件证据</strong><span>关闭后仍做实时检测与轨迹，但不新增截图、短片和事件记录</span></div><input type="checkbox" checked={form.record_events} onChange={(event) => setForm((value) => ({ ...value, record_events: event.target.checked }))} /></label><label className="toggle-row"><div><strong>保存事件短片</strong><span>普通连续录像不会长期保存；仅在启用事件证据时生效</span></div><input type="checkbox" checked={form.save_clips} disabled={!form.record_events} onChange={(event) => setForm((value) => ({ ...value, save_clips: event.target.checked }))} /></label><label><div><strong>事件前缓存</strong><span>事件发生前保留几秒</span></div><select value={form.pre_seconds} disabled={!form.record_events || !form.save_clips} onChange={(event) => setForm((value) => ({ ...value, pre_seconds: Number(event.target.value) }))}><option value={5}>5 秒</option><option value={10}>10 秒</option></select></label><label><div><strong>事件后缓存</strong><span>事件发生后继续记录几秒</span></div><select value={form.post_seconds} disabled={!form.record_events || !form.save_clips} onChange={(event) => setForm((value) => ({ ...value, post_seconds: Number(event.target.value) }))}><option value={5}>5 秒</option><option value={10}>10 秒</option></select></label></div></section>

        <section className="panel settings-section"><div className="settings-title"><div className="metric-icon yellow"><HardDrive /></div><div><h2>保留与隐私</h2></div></div><div className="setting-rows"><div className="static-setting"><div><strong>统一保留策略</strong><span>默认 MINIMAL：每件物品仅保留最近 2 次未固定的确认变化；清理前可先预览。</span></div><Badge tone="success">MINIMAL</Badge></div><div className="static-setting"><div><strong>匿名轨迹，无人脸身份识别</strong><span>这是固定隐私边界，系统不提供关闭选项</span></div><Badge tone="success">始终启用</Badge></div></div><div className="privacy-summary"><ShieldCheck /><div><strong>完全本地处理</strong><span>家庭视频不上传；摄像头凭据不会写入日志；数据库只保存媒体路径和元数据。</span></div></div><div className="cleanup-actions"><Link className="button secondary" to="/storage"><Database />打开存储管理</Link></div></section>

        {runtime.mode === 'DEMO' && <section className="panel settings-section demo-tools"><div className="settings-title"><div className="metric-icon blue"><PlayCircle /></div><div><h2>准备模拟数据和测试素材</h2></div></div><p>创建的物品、视频和事件均属于 DEMO 数据库，必须标记为模拟来源，不代表真实家庭摄像头识别结果。</p><button className="button secondary" onClick={() => void seed()}><Database />生成演示数据与素材</button></section>}
      </div>
    </div>
  )
}
