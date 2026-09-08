import { Camera as CameraIcon, Check, ImageOff, Layers3, MapPin, PencilLine, Plus, RefreshCw, Save, Trash2, Undo2 } from 'lucide-react'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Badge, EmptyState, ErrorState, Field, Loading, PageHeader } from '../components/UI'
import { SourceBadge } from '../components/ProvenanceBadge'
import { SearchResultCard } from '../components/SearchResultCard'
import { VisionFrame } from '../components/VisionFrame'
import { ScenePhysicalEditor, validScenePhysical } from '../components/ScenePhysicalEditor'
import { AutoSceneDraft } from '../components/AutoSceneDraft'
import { useRuntime } from '../contexts/RuntimeContext'
import { useApi } from '../hooks/useApi'
import { useVisiblePolling } from '../hooks/useVisiblePolling'
import { api, mediaUrl } from '../lib/api'
import { formatTime } from '../lib/format'
import { acceptsLocationEvidence, isLocationUnavailable, isObservationEvidence, recordRuntimeMode, visibleLocationHypotheses } from '../lib/provenance'
import type { Camera, EventRecord, ResolvedRuntimeMode, SearchResult, ScenePoint as Point, SceneSurfaceType as SurfaceType, SceneSurface as Surface, SceneDocument as Scene, SceneResponse, SceneMarker } from '../types'
import './ScenePage.css'

const Scene3D = lazy(() => import('../components/Scene3D'))
interface DraftSurface extends Surface { key: string }
const surfaceNames: Record<SurfaceType, string> = { table: '桌面', sofa: '沙发', bed: '床', shelf: '搁架 / 柜面', floor: '地面', other: '其他 / 盲区' }
const polygonText = (points: Point[]) => points.map(([x, y]) => `${x * 1000},${y * 1000}`).join(' ')
const pointValid = (value: unknown): value is Point => Array.isArray(value) && value.length === 2 && value.every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1)

// Match the server's bounded convex image-coordinate contract before submission.
export function validScenePolygon(points: Point[]) {
  if (points.length < 3 || points.length > 32 || !points.every(pointValid) || new Set(points.map(String)).size !== points.length) return false
  let sign = 0
  let area = 0
  for (let i = 0; i < points.length; i++) {
    const a = points[i], b = points[(i + 1) % points.length], c = points[(i + 2) % points.length]
    const cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
    if (!cross || (sign && Math.sign(cross) !== sign)) return false
    sign = Math.sign(cross)
    area += a[0] * b[1] - b[0] * a[1]
    // A convex corner test alone also accepts some star polygons.
    for (let j = 0; j < points.length; j++) {
      if (j === i || j === (i + 1) % points.length) continue
      const side = (b[0] - a[0]) * (points[j][1] - a[1]) - (b[1] - a[1]) * (points[j][0] - a[0])
      if (side && Math.sign(side) !== sign) return false
    }
  }
  return Math.abs(area) / 2 >= .0001
}

function sceneCanDisplay(scene: Scene | null, cameraId: string, mode: ResolvedRuntimeMode) {
  return Boolean(scene && mode !== 'UNKNOWN' && scene.camera_id === cameraId && recordRuntimeMode(scene) === mode
    && scene.is_simulated === (mode !== 'REAL') && scene.source_session_id && scene.snapshot_id
    && scene.geometry?.coordinate_space === 'source_normalized' && Number.isFinite(scene.geometry.source_width) && Number.isFinite(scene.geometry.source_height)
    && scene.geometry.source_width > 0 && scene.geometry.source_height > 0)
}

function observationForMap(result: SearchResult, cameraId: string, mode: ResolvedRuntimeMode) {
  const value = result.last_observed
  return value && value.item_id === result.item.id && value.camera_id === cameraId && value.source_session_id
    && isObservationEvidence(value) && acceptsLocationEvidence(value, mode) && Number.isFinite(Date.parse(value.timestamp_end || value.timestamp_start)) ? value : null
}

function observationLabel(value: EventRecord, status: string) {
  if (isLocationUnavailable(value)) return value.event_type === 'occluded' ? '被遮挡 · 最后可见点' : '当前位置未知 · 最后可见点'
  if (value.holding_status === 'possibly_held') return '可能持有 · 仅附近位置，未确认放下'
  if (value.holding_status === 'co_moving') return '连续共同移动 · 可能携带，未确认放下'
  if (value.holding_status === 'holding_uncertain') return '持有状态不确定 · 当前位置未确认'
  if (value.holding_status === 'nearby') return '手在附近 · 未建立持有关系'
  if (value.holding_status === 'release_candidate') return '释放候选 · 等待分离后稳定，未确认放下'
  if (value.holding_status === 'released') return '已观测可见分离 · 仍未确认放下'
  if (['carried', 'holding', 'picked_up', 'pickup_candidate'].includes(status.toLowerCase())) return '持有中 · 仅附近位置，未确认放下'
  return '最后看到 · 未确认放下'
}

export function ScenePage() {
  const cameras = useApi<Camera[]>('/api/cameras', [])
  const [params] = useSearchParams()
  const [cameraId, setCameraId] = useState(params.get('camera') || '')
  const [dirty, setDirty] = useState(false)
  const activeId = cameraId || cameras.data[0]?.id || ''
  const camera = cameras.data.find((value) => value.id === activeId)
  return <div className="scene-page">
    <PageHeader title="从看见，到知道在哪里" description="视频中的同一件物品，连接它的最后观察与三维位置。家具由你确认尺寸；位置来自服务端证据，不会凭空推测。" />
    {cameras.loading ? <Loading /> : cameras.error ? <ErrorState message={cameras.error} onRetry={cameras.reload} /> : !camera ? <EmptyState icon={<CameraIcon />} title="先连接一个摄像头" description="没有视频源时，不会生成场景、家具或物品位置。" action={<Link className="button primary" to="/cameras">连接摄像头</Link>} /> : <>
      <div className="scene-camera-picker"><Field label="选择场景摄像头"><select value={activeId} onChange={(event) => {
        if (dirty && !window.confirm('尚有未保存的区域修改。切换摄像头并放弃这些修改？')) return
        setDirty(false); setCameraId(event.target.value)
      }}>{cameras.data.map((entry) => <option key={entry.id} value={entry.id}>{entry.room_name} · {entry.name}</option>)}</select></Field><SourceBadge record={camera} /></div>
      <SceneWorkspace key={activeId} camera={camera} onDirty={setDirty} />
    </>}
  </div>
}

function SceneWorkspace({ camera, onDirty }: { camera: Camera; onDirty: (value: boolean) => void }) {
  const { mode } = useRuntime()
  const [params, setParams] = useSearchParams()
  const [response, setResponse] = useState<SceneResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [drafts, setDrafts] = useState<DraftSurface[]>([])
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [drawing, setDrawing] = useState(false)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [imageState, setImageState] = useState<'loading' | 'ready' | 'failed'>('loading')
  const [imageRevision, setImageRevision] = useState(0)
  const [selectedItem, setSelectedItem] = useState<string | null>(params.get('item'))
  const svgRef = useRef<SVGSVGElement>(null)
  const alive = useRef(true)
  const keyCounter = useRef(0)
  const endpoint = `/api/cameras/${encodeURIComponent(camera.id)}/scene`
  const scene = response?.scene || null
  const valid = sceneCanDisplay(scene, camera.id, mode)
  const editable = Boolean(valid && scene && !scene.invalid_reason && imageState === 'ready' && !busy)
  const active = drafts.find((value) => value.key === activeKey)
  const currentItems = (response?.items || []).flatMap((result) => {
    const observation = observationForMap(result, camera.id, mode)
    return observation ? [{ result, observation, point: pointValid(observation.final_position) ? observation.final_position : null }] : []
  })
  const selected = currentItems.find(({ result }) => result.item.id === selectedItem)
  const sceneMarkers = (response?.markers || []).filter(marker => marker.camera_id === camera.id && marker.runtime_mode === mode && marker.is_simulated === (mode !== 'REAL') && (marker.scene_version === scene?.scene_version || marker.position_status === 'unknown'))
  const selectedMarker = sceneMarkers.find(marker => marker.item_id === selectedItem)
  const positionsEnabled = valid && !scene?.invalid_reason && response?.scene_stability?.status !== 'changed'
  const items = (response?.items || []).map(result => result.item)
  const hasWorld = Boolean(valid && scene?.world_geometry?.objects.length)
  const selectItem = (id: string | null) => {
    setSelectedItem(id)
    setParams(previous => { const next = new URLSearchParams(previous); next.set('camera', camera.id); if (id) next.set('item', id); else next.delete('item'); return next }, { replace: true })
  }
  const mappingLabel = (marker?: SceneMarker) => !marker ? '没有可用三维位置' : ({ mapped: '已标定位置', mapped_unvalidated: '映射位置 · 未独立验证', region_only: '仅能确定区域', held: '持有中 · 不投影到家具', unknown: '当前位置未知', stale: '过时位置 · 非实时' })[marker.position_status]

  function applyResponse(value: SceneResponse) {
    setResponse(value); setImageState('loading'); setImageRevision((revision) => revision + 1); setActiveKey(null); setDrawing(false)
    const proposals = new Set((value.scene?.proposals || []).map((proposal) => proposal.proposal_id))
    setDrafts((value.scene?.surfaces || []).map((surface) => ({ ...surface, key: `saved-${++keyCounter.current}`,
      // Old proposals are not valid evidence for a newly captured scene.
      proposal_id: surface.proposal_id && proposals.has(surface.proposal_id) ? surface.proposal_id : undefined,
      confirmed_by_user: surface.confirmed_by_user === true && !value.scene?.invalid_reason && !['needs_confirmation', 'needs_review', 'pending_delete'].includes(value.scene?.calibration_status || ''),
      image_polygon: surface.image_polygon.map(([x, y]) => [x, y]),
    })))
    setDirty(false); onDirty(false)
  }
  useEffect(() => {
    alive.current = true
    const controller = new AbortController()
    api<SceneResponse>(endpoint, { signal: controller.signal }).then((value) => { if (alive.current && !controller.signal.aborted) applyResponse(value) })
      .catch((value) => { if (alive.current && !controller.signal.aborted) setError(value instanceof Error ? value.message : '场景读取失败') })
      .finally(() => { if (alive.current && !controller.signal.aborted) setLoading(false) })
    return () => { alive.current = false; controller.abort() }
    // Camera changes remount this workspace; never consume a previous camera response.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpoint])
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirty])
  useVisiblePolling(async signal => {
    try {
      const value = await api<SceneResponse>(endpoint, { signal })
      if (signal.aborted || !alive.current) return
      if (value.scene?.scene_version === scene?.scene_version && value.scene?.snapshot_id === scene?.snapshot_id) setResponse(value)
      else if (!dirty) applyResponse(value)
      else setNotice('服务端场景版本已变化。请先保留或放弃当前修改，再刷新；旧版本的保存会被拒绝。')
    } catch { if (!signal.aborted) setNotice('位置更新暂时断开，保留最后观察时间；人物标记到期后会自动消失。') }
  }, { enabled: !loading && !busy, intervalMs: 1500, resetKey: camera.id })

  function changed(next: DraftSurface[]) { setDrafts(next); setDirty(true); onDirty(true); setNotice('') }
  function updateActive(patch: Partial<Surface>) {
    changed(drafts.map((value) => value.key === activeKey ? { ...value, ...patch, ...(patch.image_polygon ? { calibration: null } : {}), confirmed_by_user: false } : value))
  }
  function newSurface(proposal?: Surface) {
    if (!editable || drafts.length >= 32) return
    const value: DraftSurface = { name: '', surface_type: 'other', image_polygon: [], ...proposal, surface_id: undefined,
      physical: proposal?.physical_suggestion || proposal?.physical,
      confirmed_by_user: false, key: `draft-${++keyCounter.current}` }
    changed([...drafts, value]); setActiveKey(value.key); setDrawing(!proposal)
  }
  function pointAt(clientX: number, clientY: number): Point | null {
    const rect = svgRef.current?.getBoundingClientRect()
    return rect && rect.width > 0 && rect.height > 0 ? [Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)), Math.max(0, Math.min(1, (clientY - rect.top) / rect.height))] : null
  }
  async function request(kind: 'refresh' | 'propose' | 'save') {
    if (busy) return
    if (kind !== 'save' && (dirty || kind === 'propose' && drafts.length > 0)
      && !window.confirm(kind === 'propose' ? '获取新画面后，旧区域将停用并需要重新确认。继续？' : '重新读取将放弃未保存的修改。继续？')) return
    if (kind === 'save' && (!editable || !scene || drafts.some((value) => !value.confirmed_by_user || !validScenePolygon(value.image_polygon) || !validScenePhysical(value) || !value.name.trim()))) return
    setBusy(true); setError(''); setNotice('')
    try {
      const value = await api<SceneResponse>(`${endpoint}${kind === 'propose' ? '/propose' : ''}`, kind === 'refresh' ? {} : kind === 'propose' ? { method: 'POST' } : {
        method: 'PUT', json: { scene_version: scene!.scene_version, snapshot_id: scene!.snapshot_id, source_session_id: scene!.source_session_id, source_frame: scene!.source_frame,
          surfaces: drafts.map((surface) => ({ surface_id: surface.surface_id, proposal_id: surface.proposal_id || undefined, name: surface.name.trim(), surface_type: surface.surface_type,
            image_polygon: surface.image_polygon, map_polygon: surface.image_polygon, height_optional: surface.height_optional, physical: surface.physical, calibration: surface.calibration, confirmed_by_user: true })) },
      })
      if (!alive.current) return
      applyResponse(value)
      setNotice(kind === 'save' ? '区域已由服务端保存。家具尺寸来自人工确认，物品位置仍需独立视觉证据。' : kind === 'propose' ? (value.scene?.proposals.length || value.scene?.surfaces.length ? '已获取新的来源画面。请逐一检查并确认区域。' : '已获取当前真实画面，但模型没有找出可用家具候选。可手动画区域并填写相对尺寸；没有生成虚构房间。') : '已读取服务端最新场景和观察。')
    } catch (value) {
      if (alive.current) setError(value instanceof Error ? value.message : '场景操作失败')
    } finally { if (alive.current) setBusy(false) }
  }

  if (loading) return <Loading label="正在读取已保存场景…" />
  return <>
    <AutoSceneDraft key={`${mode}:${camera.id}`} cameraId={camera.id} mode={mode} hasScene={Boolean(scene)} blocked={busy || dirty} onCreated={applyResponse} />
    <div className="scene-linked-layout">
      <section className="scene-live panel" aria-label="摄像头视频与物品选择"><header><span className="scene-step">01 / 看见</span><h2>当前视频</h2><SourceBadge record={camera} /></header>
        <VisionFrame cameraId={camera.id} registeredItems={items} selectedItemId={selectedItem} onSelectItem={selectItem} />
        <p className="scene-caption">图像解码后才显示同帧框。点选已匹配的注册物品，可联动右侧证据；跟踪不是逐帧重新确认身份。</p>
        <Link className="button ghost small" to={`/live?camera=${encodeURIComponent(camera.id)}`}>打开完整实时画面</Link>
      </section>
      <section className="scene-world panel" aria-label="三维位置联动"><header><span className="scene-step">02 / 定位</span><h2>空间里的记忆</h2><Badge tone={scene?.calibration_status === 'calibrated' ? 'blue' : 'warning'}>{scene?.calibration_status === 'calibrated' ? '独立点检查通过' : scene?.world_geometry?.status === 'draft' ? '模型候选草稿 · 未确认' : hasWorld ? '用户确认几何 · 留意标定状态' : '尚无三维几何'}</Badge></header>
        {valid && scene ? <Suspense fallback={<Loading label="正在加载三维视图…" />}><Scene3D scene={scene} markers={positionsEnabled ? sceneMarkers : []} persons={positionsEnabled ? response?.persons || [] : []} items={items} selectedItemId={selectedItem} onSelectItem={selectItem} /></Suspense> : <div className="scene-world-empty"><Layers3 /><strong>等待你确认场景</strong><p>没有已保存场景时，不显示家具或示例手机。下方可获取画面并设置区域。</p></div>}
        {(scene?.invalid_reason || scene?.calibration_status === 'validation_failed') && <p className="scene-message inline-error">场景需要重新核对。保留家具几何供参考，不显示未经验证的物品投影。</p>}
      </section>
      <aside className="scene-items panel" aria-label="物品与位置详情"><header><span className="scene-step">03 / 找回</span><h2>物品在哪里</h2></header>
        <section className="scene-observations" aria-label="这个摄像头的最后观察"><p>只有后端有证据的位置才上图。持有中、未知或无坐标时，不默认放在原点。</p>
          {!currentItems.length ? <p className="muted">暂无这个摄像头产生的可靠物品观察，不显示示例位置。</p> : <div className="scene-item-list">{currentItems.map(({ result, observation, point }) => <button key={result.item.id} className={`scene-item ${selectedItem === result.item.id ? 'selected' : ''}`} aria-pressed={selectedItem === result.item.id} onClick={() => selectItem(result.item.id)}><strong>{result.item.name}</strong><span>{observationLabel(observation, result.status)}</span><span>{positionsEnabled ? mappingLabel(sceneMarkers.find(marker => marker.item_id === result.item.id)) : '场景待复核 · 位置不投影'}</span><small>{formatTime(observation.timestamp_end || observation.timestamp_start)}{!point ? ' · 坐标未记录' : scene && observation.source_session_id !== scene.source_session_id ? ' · 与场景不同来源会话，未叠加位置' : ''}</small></button>)}</div>}
        </section>
        {selected && <section className="scene-selected" aria-label="选中物品的观察证据"><header><h2>{selected.result.item.name} · 观察详情</h2><button className="button ghost small" onClick={() => selectItem(null)}>收起详情</button></header>
          <p>{observationLabel(selected.observation, selected.result.status)}。截图和时间来自这条已保存观察。</p>
          <p>{mappingLabel(selectedMarker)}{selectedMarker?.reason ? `：${selectedMarker.reason}` : ''}</p>
          {selectedMarker && <details className="scene-marker-proof"><summary>位置来源与帧信息</summary><dl><dt>场景版本</dt><dd>{selectedMarker.scene_version}</dd><dt>位置帧</dt><dd>{selectedMarker.frame_id}</dd><dt>来源会话</dt><dd>{selectedMarker.source_session_id}</dd><dt>位置计算</dt><dd>{selectedMarker.mapping_method}</dd><dt>观察时间</dt><dd>{formatTime(selectedMarker.observation_timestamp)}</dd></dl></details>}
          {visibleLocationHypotheses(selected.result, selected.observation).length > 0 && <p className="scene-message">另有推测位置，详见下方原因；不在图上画成确定位置。</p>}
          <SearchResultCard result={{ ...selected.result, evidence: selected.observation }} runtimeMode={mode} />
        </section>}
        {selectedItem && !selected && <p className="scene-message">这个物品尚无本摄像头的可靠观察证据，不能显示确认位置。</p>}
      </aside>
    </div>
    <details className="scene-calibration panel" open={!hasWorld || undefined}><summary>场景校准与尺寸设置 <span>照片只作参考，不是三维模型</span></summary>
    <div className="scene-layout">
      <section className="scene-main panel" aria-label="场景画面与位置">
        <header className="scene-toolbar"><div><Layers3 /><strong>校准参考画面 · 二维</strong><Badge tone={scene && !scene.invalid_reason && drafts.some(surface => surface.confirmed_by_user) ? 'blue' : 'warning'}>{scene && !scene.invalid_reason && drafts.some(surface => surface.confirmed_by_user) ? '用户确认区域' : '尚未确认场景'}</Badge></div><div>
          <button className="button secondary small" disabled={busy} onClick={() => void request('refresh')}><RefreshCw />刷新记录</button>
          <button className="button primary small" disabled={busy || mode === 'UNKNOWN'} onClick={() => void request('propose')}><CameraIcon />{busy ? '正在处理…' : scene ? '重新识别场景' : '识别当前场景'}</button>
        </div></header>
        {error && <p className="scene-message inline-error" role="alert">{error}{scene ? '（保留上次读取的记录，不表示当前画面。）' : ''}</p>}
        {notice && <p className="scene-message" role="status">{notice}</p>}
        {scene?.invalid_reason && <p className="scene-message inline-error" role="alert">场景已失效：{scene.invalid_reason}。请重新识别当前场景，旧区域不会生效。</p>}
        {scene && !valid && <p className="scene-message inline-error" role="alert">场景来源、运行模式或坐标信息不匹配，已隐藏画面和位置。</p>}
        {(response?.scene_stability?.status.startsWith('insufficient_') || response?.scene_stability?.status === 'failed') && <p className="scene-message" role="status">画面纹理不足或稳定性检查未完成，位置图仅供人工参考，不据此确认放置。仍可手动绘制并保存区域。</p>}
        {response?.scene_stability?.status === 'changed' && <p className="scene-message inline-error">画面位置发生变化，请重新获取并确认场景；旧坐标不作为当前位置。</p>}
        {!scene ? <EmptyState icon={<Layers3 />} title="还没有这个房间的场景" description="先启动摄像头，再点击“识别当前场景”，也可开启上方的一次自动草稿。未开启时页面不会自动拍摄或生成家具。" action={<Link className="button secondary" to="/live">去启动摄像头</Link>} /> : valid && <>
          <div className="scene-canvas" style={{ aspectRatio: scene.geometry.source_width / scene.geometry.source_height }}>
            <img key={`${scene.snapshot_id}:${imageRevision}`} src={mediaUrl(scene.screenshot_path)} alt="用于确认区域的服务端场景截图" onLoad={(event) => {
              const image = event.currentTarget
              setImageState(image.naturalWidth === scene.geometry.source_width && image.naturalHeight === scene.geometry.source_height ? 'ready' : 'failed')
            }} onError={() => setImageState('failed')} />
            {imageState !== 'ready' && <div className="scene-image-state"><ImageOff /><span>{imageState === 'failed' ? '场景截图无法读取或尺寸不符，已禁止绘制和确认。' : '正在读取场景截图…'}</span></div>}
            {imageState === 'ready' && <>
              <svg ref={svgRef} viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-label="场景区域编辑画布" className={drawing ? 'drawing' : ''}
                onPointerDown={(event) => { if (!editable || !drawing || !active || active.image_polygon.length >= 32) return; const point = pointAt(event.clientX, event.clientY); if (point) updateActive({ image_polygon: [...active.image_polygon, point] }) }}
                onPointerMove={(event) => { if (!editable || !active || dragIndex === null) return; const point = pointAt(event.clientX, event.clientY); if (point) updateActive({ image_polygon: active.image_polygon.map((value, index) => index === dragIndex ? point : value) }) }}
                onPointerUp={() => setDragIndex(null)} onPointerCancel={() => setDragIndex(null)} onLostPointerCapture={() => setDragIndex(null)}>
                {drafts.map((surface) => <g key={surface.key} className={`scene-surface ${surface.confirmed_by_user ? 'confirmed' : 'unconfirmed'} ${surface.key === activeKey ? 'selected' : ''}`}>
                  {surface.image_polygon.length >= 3 ? <polygon points={polygonText(surface.image_polygon)} /> : <polyline points={polygonText(surface.image_polygon)} />}
                  {surface.image_polygon[0] && <text x={surface.image_polygon[0][0] * 1000 + 8} y={surface.image_polygon[0][1] * 1000 + 24}>{surface.name || '未命名区域'}{surface.confirmed_by_user ? '' : ' · 待确认'}</text>}
                </g>)}
                {active?.image_polygon.map(([x, y], index) => <circle className="scene-handle" key={index} cx={x * 1000} cy={y * 1000} r="11" onPointerDown={(event) => {
                  event.stopPropagation(); if (!editable) return; setDragIndex(index); event.currentTarget.setPointerCapture(event.pointerId)
                }} />)}
              </svg>
              {!drawing && !scene.invalid_reason && scene.calibration_status === 'schematic' && response?.scene_stability?.status !== 'changed' && currentItems.filter(({ observation, point }) => point && observation.source_session_id === scene.source_session_id).map(({ result, observation, point }) => <button key={result.item.id} className={`scene-marker ${isLocationUnavailable(observation) ? 'uncertain' : ''}`} style={{ left: `${point![0] * 100}%`, top: `${point![1] * 100}%` }} aria-label={`查看${result.item.name}的最后观察`} onClick={() => selectItem(result.item.id)}><MapPin /><span>{result.item.name}</span></button>)}
            </>}
          </div>
          <div className="scene-caption"><span>场景截图：{formatTime(scene.source_timestamp)} · 帧 {scene.source_frame}</span><span>此处为二维参考；位置点来自各自的最后观察，不代表场景截图同帧。三维高度与映射须在右侧单独确认。</span></div>
          {scene.proposal_error && <p className="scene-message" role="status">{scene.proposal_error}</p>}
        </>}
      </section>
      <aside className="scene-sidebar">
        <section className="scene-editor panel" aria-label="确认场景区域"><header><h2>确认区域</h2><button className="button secondary small" disabled={!editable || drafts.length >= 32} onClick={() => newSurface()}><Plus />手动画区域</button></header>
          <p>家具框只是模型建议。圈定真正的支撑范围并确认；盲区可选“其他”自行命名，不代表识别到物体。</p>
          {scene?.proposals?.filter((proposal) => !drafts.some((value) => value.proposal_id === proposal.proposal_id)).map((proposal) => <button className="scene-proposal" key={proposal.proposal_id} disabled={!editable || drafts.length >= 32} onClick={() => newSurface(proposal)}><Plus /><span>采用建议：{proposal.name}<small>先检查边界，尚未确认</small></span></button>)}
          <div className="scene-surface-list">{drafts.map((surface) => <div key={surface.key}><button className={surface.key === activeKey ? 'active' : ''} onClick={() => { setActiveKey(surface.key); setDrawing(false) }}><span>{surface.name || '未命名区域'}</span><small>{surface.confirmed_by_user ? '已核对' : '待确认'}</small></button><button className="icon-button danger-icon" aria-label={`删除区域${surface.name || '未命名区域'}`} disabled={!editable} onClick={() => { changed(drafts.filter((value) => value.key !== surface.key)); if (activeKey === surface.key) { setActiveKey(null); setDrawing(false) } }}><Trash2 /></button></div>)}</div>
          {active && <fieldset disabled={!editable} className="scene-surface-form"><legend>编辑选中区域</legend>
            <Field label="场景区域名称"><input value={active.name} maxLength={80} onChange={(event) => updateActive({ name: event.target.value })} placeholder="例如：工作桌面、暂未覆盖区" /></Field>
            <Field label="区域类型"><select value={active.surface_type} onChange={(event) => updateActive({ surface_type: event.target.value as SurfaceType })}>{Object.entries(surfaceNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
            <div className="scene-draw-actions"><button type="button" className="button secondary small" aria-pressed={drawing} onClick={() => setDrawing(!drawing)}><PencilLine />{drawing ? '完成描边' : '继续添加顶点'}</button><button type="button" className="button ghost small" disabled={!active.image_polygon.length} onClick={() => updateActive({ image_polygon: active.image_polygon.slice(0, -1) })}><Undo2 />撤销顶点</button></div>
            <p className="muted">沿边缘依次点击 3–32 个点，拖动圆点微调。仅支持凸多边形。</p>
            <details className="scene-point-editor"><summary>用数值编辑顶点（百分比）</summary>{active.image_polygon.map(([x, y], index) => <div key={index}><span>{index + 1}</span>{([x, y] as Point).map((value, axis) => <input key={axis} type="number" aria-label={`顶点${index + 1}${axis ? '纵' : '横'}坐标百分比`} min="0" max="100" step="0.1" value={Number((value * 100).toFixed(2))} onChange={(event) => { const number = event.target.valueAsNumber; if (Number.isFinite(number)) updateActive({ image_polygon: active.image_polygon.map((point, i) => i === index ? point.map((v, a) => a === axis ? Math.max(0, Math.min(100, number)) / 100 : v) as Point : point) }) }} />)}<button type="button" className="icon-button" aria-label={`删除顶点${index + 1}`} onClick={() => updateActive({ image_polygon: active.image_polygon.filter((_, i) => i !== index) })}><Trash2 /></button></div>)}<button type="button" className="button secondary small" disabled={active.image_polygon.length >= 32} onClick={() => updateActive({ image_polygon: [...active.image_polygon, [0, 0]] })}>添加数值顶点</button></details>
            {!validScenePolygon(active.image_polygon) && <p className="inline-error">请围成不相交的凸多边形，至少 3 个不同顶点，且不能只有一条线。</p>}
            <ScenePhysicalEditor surface={active} onChange={updateActive} />
            <label className="scene-confirm"><input type="checkbox" checked={active.confirmed_by_user} disabled={!active.name.trim() || !validScenePolygon(active.image_polygon) || !validScenePhysical(active)} onChange={(event) => { changed(drafts.map((value) => value.key === activeKey ? { ...value, confirmed_by_user: event.target.checked } : value)); setDrawing(false) }} /><span>我已核对名称、类型与区域边界</span></label>
            {active.physical && <p className="muted">确认同时表示已检查上方尺寸、坐标和标定点；没有验证点不会被标为精确位置。</p>}
          </fieldset>}
          <button className="button primary full" disabled={!editable || !dirty || drafts.some((value) => !value.confirmed_by_user || !validScenePolygon(value.image_polygon) || !validScenePhysical(value) || !value.name.trim())} onClick={() => void request('save')}><Save />保存已确认区域</button>
          {dirty && <small className="scene-unsaved">有未保存修改。所有保留区域需确认后才能保存；删除将在保存后生效。</small>}
        </section>
        <section className="scene-guide panel"><h2>第一次使用，按这四步</h2><ol><li><Link to="/items">注册物品照片</Link><span>至少一张，框出目标后建立识别档案。</span></li><li><Link to="/live">启动摄像头</Link><span>保持物品清晰可见；照片识别须在服务端加载。</span></li><li><strong>确认房间区域</strong><span>获取当前场景，调整并保存桌面等边界。</span></li><li><Link to="/search">移动后再寻找物品</Link><span>移到另一处并停稳后查询，检查观察时间和截图。</span></li></ol><p><Check />上述是操作指南，不是物理移动验收已通过。真实物品测试仍需实际执行。</p></section>
      </aside>
    </div>
    </details>
  </>
}
