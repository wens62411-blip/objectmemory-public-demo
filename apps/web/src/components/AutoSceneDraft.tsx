import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { canonicalSourceType, recordRuntimeMode } from '../lib/provenance'
import type { Camera, ResolvedRuntimeMode, SceneResponse } from '../types'

const realSources = ['opencv_camera', 'esp32_real', 'authorized_screen_capture', 'browser_camera', 'rtsp', 'onvif', 'mjpeg']
export function hasFreshSceneSource(camera: Camera | undefined, mode: ResolvedRuntimeMode) {
  if (!camera || mode === 'UNKNOWN' || recordRuntimeMode(camera) !== mode || camera.is_simulated !== (mode !== 'REAL')) return false
  const source = canonicalSourceType(camera.source_type_provenance || camera.source_type)
  if (mode === 'REAL' && !realSources.includes(source)) return false
  const health = camera.health
  const sequence = health?.preview_frame_sequence ?? health?.preview_sequence
  return Boolean(health && !health.health_snapshot_stale && health.preview_source_session_id
    && typeof sequence === 'number' && Number.isInteger(sequence) && sequence >= 0
    && typeof health.preview_age_ms === 'number' && Number.isFinite(health.preview_age_ms) && health.preview_age_ms >= 0 && health.preview_age_ms <= 1000
    && !['offline', 'stopped', 'error', 'disconnected'].includes(String(health.status_code || health.status).toLowerCase()))
}

export function acceptsSceneResponse(value: SceneResponse, cameraId: string, mode: ResolvedRuntimeMode) {
  if (!value || mode === 'UNKNOWN' || value.runtime_mode !== mode || value.is_simulated !== (mode !== 'REAL') || typeof value.source_type !== 'string') return false
  const source = canonicalSourceType(value.source_type)
  if (mode === 'REAL' && !realSources.includes(source)) return false
  const scene = value.scene
  if (!scene) return scene === null
  const geometry = scene.geometry
  // A valid historical scene remains historical; this check never promotes it
  // to a live frame or calibrated support plane.
  return Boolean(scene.camera_id === cameraId && scene.runtime_mode === mode && scene.is_simulated === value.is_simulated
    && canonicalSourceType(scene.source_type) === source && scene.source_session_id && scene.snapshot_id
    && Number.isInteger(scene.source_frame) && scene.source_frame >= 0
    && Number.isFinite(Date.parse(scene.source_timestamp)) && /^[a-f0-9]{64}$/.test(scene.screenshot_sha256)
    && /^\/media\/scene-images\/[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.jpg$/.test(scene.screenshot_path)
    && Number.isInteger(scene.scene_version) && scene.scene_version > 0
    && geometry?.coordinate_space === 'source_normalized' && Number.isInteger(geometry.source_width) && geometry.source_width > 0
    && Number.isInteger(geometry.source_height) && geometry.source_height > 0
    && Array.isArray(scene.proposals) && scene.proposals.length <= 32 && Array.isArray(scene.surfaces) && scene.surfaces.length <= 32)
}

const memoryAttempts = new Set<string>()
function stored(key: string) { try { return localStorage.getItem(key) === '1' } catch { return false } }
function remember(key: string, value: boolean) { try { if (value) localStorage.setItem(key, '1'); else localStorage.removeItem(key) } catch { /* Private mode: backend still protects existing scenes. */ } }
function preference(key: string) { try { return localStorage.getItem(key) !== '0' } catch { return true } }
function setPreference(key: string, value: boolean) { try { localStorage.setItem(key, value ? '1' : '0') } catch { /* An unavailable preference store does not bypass the server gate. */ } }

/** One optional empty-scene job, not a frame-by-frame inference subscriber. */
export function AutoSceneDraft({ cameraId, mode, hasScene, blocked, onCreated }: { cameraId: string; mode: ResolvedRuntimeMode; hasScene: boolean; blocked: boolean; onCreated: (response: SceneResponse) => void }) {
  const key = `objectmemory:auto-scene:${mode}:${cameraId}`
  const attemptKey = `${key}:attempted`
  const [enabled, setEnabled] = useState(() => preference(key))
  const [attempted, setAttempted] = useState(() => stored(attemptKey) || memoryAttempts.has(attemptKey))
  const [notice, setNotice] = useState('')
  const callback = useRef(onCreated); callback.current = onCreated
  useEffect(() => {
    if (!enabled || attempted || hasScene || blocked || mode === 'UNKNOWN') return
    let stopped = false, pending = false, submitted = false
    let timer: number | undefined
    let controller: AbortController | null = null
    const started = performance.now()
    const schedule = (delay: number) => { window.clearTimeout(timer); if (!stopped && !submitted && !document.hidden) timer = window.setTimeout(() => void check(), delay) }
    async function check() {
      if (stopped || submitted || pending || document.hidden) return
      if (performance.now() - started > 60_000) { setNotice('一分钟内没有等到有效新画面，已暂停自动草稿。启动相机后可关闭再开启此选项。'); setEnabled(false); setPreference(key, false); return }
      pending = true; controller = new AbortController()
      const signal = controller.signal
      const timeout = window.setTimeout(() => controller?.abort(), 8000)
      try {
        setNotice('等待这个摄像头的新鲜画面；不会为场景扫描重新开启摄像头。')
        const cameras = await api<Camera[]>('/api/cameras', { signal })
        if (signal.aborted || stopped || !hasFreshSceneSource(cameras.find(camera => camera.id === cameraId), mode)) return
        // Re-read just before creation; server only_if_empty guards the remaining race.
        const latest = await api<SceneResponse>(`/api/cameras/${encodeURIComponent(cameraId)}/scene`, { signal })
        if (signal.aborted || stopped) return
        if (!acceptsSceneResponse(latest, cameraId, mode)) { setNotice('场景来源信息未通过校验，没有采用记录或生成草稿。'); return }
        if (latest.scene) { callback.current(latest); setNotice('场景已经存在，保留原有模型和人工区域，没有重新识别。'); return }
        // Mark BEFORE mutation. A refresh or an uncertain response cannot repeat the job.
        submitted = true; memoryAttempts.add(attemptKey); remember(attemptKey, true)
        setNotice('正在识别一次当前场景并保存未确认的三维草稿…')
        const response = await api<SceneResponse>(`/api/cameras/${encodeURIComponent(cameraId)}/scene/propose?only_if_empty=true`, { method: 'POST', signal })
        if (signal.aborted || stopped) return
        if (!acceptsSceneResponse(response, cameraId, mode) || !response.scene) throw new Error('返回的场景来源不匹配，未在页面采用。')
        if (response.scene.calibration_status !== 'needs_confirmation' || response.scene.surfaces.length
          || response.scene.world_geometry?.objects.some(object => object.confirmed_by_user || object.source !== 'model_proposal_estimate')) throw new Error('自动生成的场景不是未确认草稿，未采用其定位信息。')
        callback.current(response)
        setNotice(response.scene.proposals.length ? '自动草稿已存入场景库。家具来自模型候选，尺寸和深度只是估计；需核对后才能用于定位。' : '已保存来源画面，但模型没有识别出家具候选，没有编造家具或房间。可手动补充区域。')
      } catch (value) {
        if (!stopped) setNotice(submitted ? `本次自动草稿未完成，不会自动重试或覆盖已有场景。${value instanceof Error ? value.message : '请刷新记录后检查。'}` : '暂时无法获取摄像头状态，等待下一次检查；没有生成场景。')
      } finally {
        window.clearTimeout(timeout); pending = false; controller = null
        if (!stopped && submitted) setAttempted(true)
        schedule(1500)
      }
    }
    const visible = () => { if (document.hidden) { window.clearTimeout(timer); controller?.abort() } else schedule(0) }
    document.addEventListener('visibilitychange', visible); schedule(0)
    return () => { stopped = true; window.clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visible) }
  }, [enabled, attempted, hasScene, blocked, mode, cameraId, attemptKey, key])

  return <section className="scene-auto-draft" aria-label="自动场景草稿">
    <label className="scene-confirm"><input type="checkbox" checked={enabled} disabled={mode === 'UNKNOWN' || blocked} onChange={event => {
      const next = event.target.checked; setEnabled(next); setPreference(key, next)
      if (next) { memoryAttempts.delete(attemptKey); remember(attemptKey, false); setAttempted(false) }
      setNotice(next ? '已允许空场景在取得新画面后生成一次草稿。' : '自动场景草稿已关闭。')
    }} /><span>首次在线画面自动生成一次近似场景草稿</span></label>
    <p className="scene-caption">仅在这个场景库尚无记录时运行。只保存当前运行模式下的家具候选及近似三维草稿，不自动确认区域、标定距离或生成物品位置。已有场景永不自动覆盖。</p>
    {hasScene && <p className="scene-caption">已有场景已保留；需要更新时请明确点击“重新识别场景”。</p>}
    {!hasScene && attempted && !notice && <p className="scene-caption">此前已尝试一次自动草稿。刷新不会重复运行；先检查记录，需要重试时关闭再开启此选项。</p>}
    {notice && <p className="scene-message" role="status">{notice}</p>}
  </section>
}
