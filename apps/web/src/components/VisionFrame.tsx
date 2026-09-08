import { useEffect, useLayoutEffect, useReducer, useRef, useState, type CSSProperties } from 'react'
import { Maximize2, ShieldCheck } from 'lucide-react'
import { api } from '../lib/api'
import { formatTime } from '../lib/format'
import { isShapeCandidate, recognitionRejection } from '../lib/recognition'
import { SourceBadge } from './ProvenanceBadge'
import type { Camera, EventRecord, FramePipelineDiagnostics, HandAction, HandInteraction, Item, RecognitionTest, SourceType, Zone } from '../types'
import { ModeNotice } from './ModeNotice'
import { sourceCategory } from '../lib/provenance'
import { HandActionSummary } from './HandActionsCard'
import { reportPollingError, useVisiblePolling } from '../hooks/useVisiblePolling'
import { useLiveVision } from '../hooks/useLiveVision'
import './VisionFrame.css'

type Candidate = RecognitionTest['candidates'][number]
type Snapshot = Pick<EventRecord, 'runtime_mode' | 'source_type' | 'is_simulated'> & {
  image_data_url: string; width: number; height: number; source_frame: number; source_timestamp: string
  source_session_id: string; candidates: Candidate[]
  hands: { landmarks: number[][]; handedness?: string | null }[]
  hand_status?: { status?: string; error?: string; last_latency_ms?: number }
  hand_actions?: HandAction[]
  interactions?: HandInteraction[]
  display_only?: boolean
  pipeline_diagnostics?: FramePipelineDiagnostics
}
const EDGES = [[0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 6], [6, 7], [7, 8], [5, 9], [9, 10], [10, 11], [11, 12], [9, 13], [13, 14], [14, 15], [15, 16], [13, 17], [0, 17], [17, 18], [18, 19], [19, 20]]
const CATEGORY_NAMES: Record<string, string> = { 'cell phone': '手机', phone: '手机', 'mobile phone': '手机', keys: '钥匙', key: '钥匙', wallet: '钱包', remote: '遥控器', glasses: '眼镜', bed: '床', couch: '沙发', sofa: '沙发', chair: '椅子', person: '人' }
const categoryName = (value: string) => CATEGORY_NAMES[value.toLowerCase()] || value
const pointValid = (point?: number[]) => Boolean(point && point.length >= 2 && point.slice(0, 2).every(value => Number.isFinite(value) && value >= 0 && value <= 1))
const boxValid = (box: number[]) => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) && box[0] >= 0 && box[1] >= 0 && box[2] > 0 && box[3] > 0 && box[0] + box[2] <= 1.00001 && box[1] + box[3] <= 1.00001
const isPerson = (candidate: Candidate) => candidate.entity_type === 'person' && Boolean(candidate.person_track_id)
const candidateLabel = (candidate: Candidate) => isPerson(candidate) ? '匿名人物 · 独立人体检测，不识别人脸身份' : isShapeCandidate(candidate) ? '外形像手机，未确认类别和身份，不更新位置' : candidate.accepted && candidate.best_item_id ? `已匹配 ${candidate.best_item_name || '注册物品'}${candidate.category_evidence === 'registered_reference_patch' ? ' · 注册照片局部特征' : ''}` : `候选${categoryName(candidate.category || '物品')}（身份未确认）`

type FrameBuffers = { slots: [Snapshot | null, Snapshot | null]; visible: 0 | 1 | null }
type BufferAction = { type: 'clear' } | { type: 'queue'; frame: Snapshot } | { type: 'show'; slot: 0 | 1; key: string }
const sourceKey = (frame: Snapshot) => `${frame.source_session_id}:${frame.source_frame}`
const emptyBuffers = (): FrameBuffers => ({ slots: [null, null], visible: null })

function PipelineReadout({ frame, items }: { frame: Snapshot; items: Item[] }) {
  const diagnostic = frame.pipeline_diagnostics
  if (!diagnostic) return <p className="photo-help">后端尚未回报物品管线计数；不能根据“看见手”推断物品模型已经运行。</p>
  if (diagnostic.source_session_id !== frame.source_session_id || diagnostic.source_frame !== frame.source_frame) {
    return <p role="alert">诊断计数与当前模型帧来源不一致，未使用这些数据判定识别状态。</p>
  }
  const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : '未回报'
  const metrics = [
    ['本会话接收帧', diagnostic.capture_frames_received], ['已处理帧', diagnostic.processed_frames],
    ['物品模型帧', diagnostic.object_model_frames], ['手部模型帧', diagnostic.hand_model_frames],
    ['已加载物品档案', diagnostic.loaded_profile_count], ['本帧原始检测', diagnostic.raw_detection_count],
    ['其中物品候选（不含人）', diagnostic.raw_object_count], ['本帧参与匹配', diagnostic.candidate_count],
    ['本帧身份接受', diagnostic.identity_accepted_count], ['本帧身份拒绝', diagnostic.identity_rejected_count],
  ] as const
  const versions = Object.entries(diagnostic.loaded_profile_versions || {})
  return <section className="vision-pipeline-diagnostics" aria-label="物品识别链路诊断">
    <h4>这一帧处理到哪一步</h4>
    <dl>{metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{count(value)}</dd></div>)}</dl>
    <p>手和物品使用不同模型。“手部模型帧”增加不代表物品已检出；只有身份接受后才进入跟踪与位置证据判断。</p>
    {versions.length > 0 ? <p>已加载版本：{versions.map(([id, version]) => `${items.find(item => item.id === id)?.name || '注册物品'} v${count(version)}`).join('、')}</p> : <p>档案加载代次：{count(diagnostic.profile_generation)}（不是单件物品档案版本）</p>}
    {Object.entries(diagnostic.stage_errors || {}).filter(([, error]) => error).map(([stage, error]) => <p role="alert" key={stage}>{stage}：{error}</p>)}
    <p className="photo-help">模型帧 {frame.source_frame} · 累计计数只属于当前来源会话，换源或重连后重建；不是准确率或确认移动数。</p>
  </section>
}
function frameBuffers(state: FrameBuffers, action: BufferAction): FrameBuffers {
  if (action.type === 'clear') return state.slots.some(Boolean) ? emptyBuffers() : state
  if (action.type === 'show') {
    const frame = state.slots[action.slot]
    return frame && sourceKey(frame) === action.key ? { ...state, visible: action.slot } : state
  }
  // Repeated diagnostic responses must neither reload nor hide an existing frame.
  if (state.slots.some(frame => frame && sourceKey(frame) === sourceKey(action.frame) && frame.image_data_url === action.frame.image_data_url)) return state
  const displayed = state.visible === null ? null : state.slots[state.visible]
  if (displayed && displayed.source_session_id !== action.frame.source_session_id) {
    return { slots: [action.frame, null], visible: null }
  }
  const target = state.visible === 0 ? 1 : 0
  const slots: FrameBuffers['slots'] = [...state.slots]
  slots[target] = action.frame
  return { ...state, slots }
}

export function VisionFrame({ cameraId, showHands = true, active = true, registeredItems = [], selectedItemId, onSelectItem, continuous = true, primary = false, camera, mode = 'unknown', zones = [], showZones = true }: { cameraId: string; showHands?: boolean; active?: boolean; registeredItems?: Item[]; selectedItemId?: string | null; onSelectItem?: (itemId: string) => void; continuous?: boolean; primary?: boolean; camera?: Camera; mode?: string; zones?: Zone[]; showZones?: boolean }) {
  const stream = useLiveVision(cameraId, active && continuous)
  const streamActive = useRef(false)
  streamActive.current = stream.connected
  const [incomingFrame, setFrame] = useState<Snapshot | null>(null)
  const [buffers, buffer] = useReducer(frameBuffers, undefined, emptyBuffers)
  const [error, setError] = useState('')
  const [overlay, setOverlay] = useState(true)
  const [showAll, setShowAll] = useState(false)
  const [modelDetails, setModelDetails] = useState(false)
  const [fullscreenError, setFullscreenError] = useState('')
  const stageRef = useRef<HTMLDivElement>(null)
  const seen = useRef({ session: '', sequence: -1, expired: '' })
  const expiry = useRef<ReturnType<typeof setTimeout>>()
  const lastAcknowledged = useRef('')
  const displayed = buffers.visible === null ? null : buffers.slots[buffers.visible]
  // Never carry a displayed image across an explicit clear or source restart.
  const presented = incomingFrame && displayed?.source_session_id === incomingFrame.source_session_id ? displayed : null
  const frame = presented || incomingFrame
  const decoded = Boolean(presented)

  useLayoutEffect(() => {
    buffer(incomingFrame ? { type: 'queue', frame: incomingFrame } : { type: 'clear' })
    if (!incomingFrame) lastAcknowledged.current = ''
  }, [incomingFrame])

  useLayoutEffect(() => {
    if (!presented?.display_only) return
    const key = sourceKey(presented)
    if (lastAcknowledged.current === key || `${seen.current.session}:${seen.current.sequence}` !== key) return
    // Both the visible image and its metadata/overlay have committed before
    // releasing the old Blob URL and allowing the next in-flight frame.
    lastAcknowledged.current = key
    stream.acknowledge(presented.source_frame, presented.source_session_id)
  }, [presented, stream.acknowledge])

  useEffect(() => {
    if (!stream.connected) {
      setFrame(previous => primary || previous?.display_only ? null : previous)
      if (primary) setError(stream.error)
      return
    }
    clearTimeout(expiry.current)
    if (stream.frame) {
      seen.current = { session: stream.frame.source_session_id, sequence: stream.frame.source_frame, expired: '' }
      setFrame(stream.frame as Snapshot); setError('')
    } else { setFrame(null); setError(stream.error) }
  }, [stream.frame, stream.connected, stream.error, primary])

  useEffect(() => {
    seen.current = { session: '', sequence: -1, expired: '' }
    setFrame(null); setError(''); setShowAll(false); setModelDetails(false); setFullscreenError('')
    const hide = () => {
      if (document.visibilityState === 'hidden') {
        seen.current.expired = `${seen.current.session}:${seen.current.sequence}`
        clearTimeout(expiry.current); setFrame(null)
      }
    }
    document.addEventListener('visibilitychange', hide)
    return () => { clearTimeout(expiry.current); document.removeEventListener('visibilitychange', hide) }
  }, [cameraId, active])

  useVisiblePolling(async (signal) => {
    try {
      const snapshot = await api<Snapshot>(`/api/cameras/${encodeURIComponent(cameraId)}/vision-snapshot`, { signal })
      if (signal.aborted || streamActive.current) return
      if (!snapshot.source_session_id || !Number.isInteger(snapshot.source_frame) || !Number.isFinite(snapshot.width) || !Number.isFinite(snapshot.height) || snapshot.width <= 0 || snapshot.height <= 0 || !/^data:image\/(jpeg|png);base64,/.test(snapshot.image_data_url) || !Array.isArray(snapshot.candidates) || !Array.isArray(snapshot.hands)) throw new Error('识别帧数据不完整，未显示图像或识别框。')
      const key = `${snapshot.source_session_id}:${snapshot.source_frame}`
      if (key === seen.current.expired || (snapshot.source_session_id === seen.current.session && snapshot.source_frame < seen.current.sequence)) {
        setFrame(null); setError('识别帧已过期，等待新鲜画面'); return
      }
      if (snapshot.source_session_id !== seen.current.session || snapshot.source_frame !== seen.current.sequence) {
        seen.current.session = snapshot.source_session_id; seen.current.sequence = snapshot.source_frame
        clearTimeout(expiry.current)
        expiry.current = setTimeout(() => { seen.current.expired = key; setFrame(null); setError('识别帧已过期，等待新鲜画面') }, 2500)
      }
      setFrame(snapshot); setError('')
    } catch (value) {
      if (reportPollingError(signal)) { setFrame(null); setError(signal.reason === 'poll_timeout' ? '识别帧读取超时，旧框已撤下。' : value instanceof Error ? value.message : '识别帧暂时不可用') }
    }
  }, { enabled: active && !stream.connected && (!continuous || !primary), intervalMs: 500, timeoutMs: 2500, immediate: true, resetKey: cameraId })

  async function enterFullscreen() {
    try {
      if (!stageRef.current?.requestFullscreen) throw new Error('unsupported')
      await stageRef.current.requestFullscreen()
      setFullscreenError('')
    } catch { setFullscreenError('当前浏览器未能进入全屏，画面仍可正常查看。') }
  }

  const preferredCategories = new Set(['手机', ...registeredItems.map(item => categoryName(item.type))])
  const validCandidates = (frame?.candidates || []).filter(candidate => boxValid(candidate.bbox))
  // Both primary and diagnostic views keep geometry-only proposals opt-in.
  // A bright window/paper outline is not a model-confirmed phone candidate,
  // even if an inconsistent payload also supplies accepted/item-id fields.
  const priority = (candidate: Candidate) => isShapeCandidate(candidate) ? 4 : candidate.accepted && candidate.best_item_id ? 0 : preferredCategories.has(categoryName(candidate.category || '')) ? 1 : isPerson(candidate) || candidate.best_item_id ? 2 : 3
  const prioritized = [...validCandidates].sort((a, b) => priority(a) - priority(b))
  const visibleCandidates = (showAll ? prioritized : prioritized.filter(candidate => priority(candidate) < 3)).slice(0, showAll ? 16 : 4)
  const hiddenShapeSuggestions = !showAll && validCandidates.some(isShapeCandidate)
  const sourceRecord = frame ? { ...frame, source_type: frame.source_type as SourceType } : camera
  const sourceNotices = <><SourceBadge record={sourceRecord} /><ModeNotice mode={mode} replay={sourceCategory(sourceRecord) === 'video'} compact />{['screen', 'authorized_screen_capture'].includes(sourceRecord?.source_type || '') && <span className="capture-notice"><ShieldCheck />授权区域采集</span>}</>
  const labels: Record<string, string> = { disabled: '未启用', loading: '加载中', ready: '当前帧已分析', no_hands: '当前未检测到手', failed: '处理失败', unavailable: '模型不可用' }
  return <section aria-label="同帧物品和手部关键点" className={`panel vision-frame${primary ? ' vision-frame-primary' : ''}`}>
    <div className="section-heading"><strong>{primary ? '实时画面' : continuous ? '物品识别 · 连续跟踪' : '模型原帧 · 物品与手部关键点'}</strong><label><input type="checkbox" checked={overlay} onChange={event => setOverlay(event.target.checked)} />显示识别框</label></div>
    <p className="photo-help">{stream.connected ? `${stream.fps > 0 ? `解码更新 ${stream.fps.toFixed(1)} FPS` : '正在测量解码速度'} · 每帧测量跟踪，模型周期校验身份。跟踪框不作为新识别或放置证据。` : primary ? '等待连续画面；模型原帧仅在下方展开后读取，不冒充实时播放。' : continuous ? '连续视频连接中；当前为低频模型诊断帧，不是 30 FPS 跟踪。' : '这里单独显示实际模型帧和同帧手部关键点；低频更新，不影响上方连续画面。'}</p>
    <p className="photo-help">身份匹配不等于连续观察或确认放下；只有后台证据门通过才保存位置证据。</p>
    {!active ? <p role="status">摄像头尚未运行，当前没有物品识别帧。</p> : error ? <p role="alert">{error}</p> : !frame && <p>等待当前摄像头的新鲜识别帧…</p>}
    {primary && !frame && <div className="vision-source-notices">{sourceNotices}</div>}
    {fullscreenError && <p role="alert">{fullscreenError}</p>}
    {active && frame && <>
      <div ref={stageRef} className="vision-frame-stage" style={{ '--vision-aspect': frame.width / frame.height } as CSSProperties}>
      <div className="vision-frame-image" style={{ aspectRatio: `${frame.width}/${frame.height}`, maxWidth: primary ? undefined : Math.min(640, 360 * frame.width / frame.height) }}>
        {buffers.slots.map((image, index) => image && <img key={index} src={image.image_data_url}
          alt={decoded ? buffers.visible === index ? '同帧识别原图' : '等待解码的新帧' : '同帧识别原图'}
          aria-hidden={decoded && buffers.visible !== index ? true : undefined}
          data-source-frame={image.source_frame} data-source-session={image.source_session_id}
          style={{ visibility: decoded && buffers.visible === index ? 'visible' : 'hidden' }}
          onLoad={event => {
            const key = sourceKey(image)
            if (`${seen.current.session}:${seen.current.sequence}` !== key) return
            const decodedImage = event.currentTarget
            if (decodedImage.naturalWidth > 0 && (decodedImage.naturalWidth !== image.width || decodedImage.naturalHeight !== image.height)) {
              seen.current.expired = key; setFrame(null); setError('原图实际尺寸与识别坐标不一致，已撤下画面和识别框。'); return
            }
            if (`${seen.current.session}:${seen.current.sequence}` === key) buffer({ type: 'show', slot: index as 0 | 1, key })
          }}
          onError={() => {
            const key = sourceKey(image)
            if (`${seen.current.session}:${seen.current.sequence}` !== key) return
            seen.current.expired = key; setFrame(null); setError('识别图像无法解码，未保留识别框。')
          }} />)}
        {!decoded && <span className="vision-image-loading" role="status">正在解码本帧，暂不显示识别框…</span>}
        {decoded && showZones && zones.some(zone => zone.enabled) && <svg className="zone-overlay" viewBox={`0 0 ${frame.width} ${frame.height}`} aria-label="当前摄像头区域">
          {zones.filter(zone => zone.enabled && zone.points.length >= 3 && zone.points.every(pointValid)).map(zone => <g key={zone.id}><polygon points={zone.points.map(([x, y]) => `${x * frame.width},${y * frame.height}`).join(' ')} /><text x={zone.points[0][0] * frame.width + 8} y={zone.points[0][1] * frame.height + 22}>{zone.name}</text></g>)}
        </svg>}
        {decoded && (overlay || showHands) && <svg aria-label="真实关键点叠加" viewBox={`0 0 ${frame.width} ${frame.height}`}>
          {overlay && visibleCandidates.map((candidate, index) => <g key={index} aria-label={candidateLabel(candidate)} role={candidate.accepted && candidate.best_item_id && onSelectItem ? 'button' : undefined} tabIndex={candidate.accepted && candidate.best_item_id && onSelectItem ? 0 : undefined} onClick={() => { if (!isShapeCandidate(candidate) && candidate.accepted && candidate.best_item_id) onSelectItem?.(candidate.best_item_id) }} onKeyDown={event => { if ((event.key === 'Enter' || event.key === ' ') && !isShapeCandidate(candidate) && candidate.accepted && candidate.best_item_id) { event.preventDefault(); onSelectItem?.(candidate.best_item_id) } }} style={{ filter: selectedItemId && candidate.best_item_id === selectedItemId ? 'drop-shadow(0 0 4px #fff)' : undefined }}>
            <rect x={candidate.bbox[0] * frame.width} y={candidate.bbox[1] * frame.height} width={candidate.bbox[2] * frame.width} height={candidate.bbox[3] * frame.height} fill="none" stroke={isPerson(candidate) ? '#42a5f5' : !isShapeCandidate(candidate) && candidate.accepted && candidate.best_item_id ? '#2bce95' : '#f4b74a'} strokeWidth={3} />
            <text x={candidate.bbox[0] * frame.width + 3} y={Math.max(18, candidate.bbox[1] * frame.height + 18)} fontSize={16} fill="white" stroke="#172b24" strokeWidth={3} paintOrder="stroke">{index + 1} · {isShapeCandidate(candidate) ? '外形像手机 · 未确认' : candidateLabel(candidate)}</text>
          </g>)}
          {showHands && frame.hands.map((hand, index) => <g key={index} stroke="#21d8df" fill="#f5c65b">{EDGES.map(([a, b]) => pointValid(hand.landmarks[a]) && pointValid(hand.landmarks[b]) && <line key={`${a}-${b}`} x1={hand.landmarks[a][0] * frame.width} y1={hand.landmarks[a][1] * frame.height} x2={hand.landmarks[b][0] * frame.width} y2={hand.landmarks[b][1] * frame.height} strokeWidth={2} />)}{hand.landmarks.map((point, p) => pointValid(point) && <circle key={p} cx={point[0] * frame.width} cy={point[1] * frame.height} r={3} />)}</g>)}
        </svg>}
        {primary && <div className="vision-frame-topline"><div className="vision-source-notices">{sourceNotices}</div><button className="feed-control" type="button" onClick={() => void enterFullscreen()} aria-label="全屏"><Maximize2 /></button></div>}
      </div>
      </div>
      <div className="vision-object-summary" aria-label="当前物品候选">
        {visibleCandidates.length ? visibleCandidates.map((candidate, index) => <p key={index} className={isShapeCandidate(candidate) ? 'vision-shape-candidate' : undefined}><strong>{index + 1} · {candidateLabel(candidate)}</strong>{isPerson(candidate) ? <span>只在短期内存中跟踪；是否能映射到地面另由同帧脚部证据和标定决定。</span> : isShapeCandidate(candidate) ? <span>仅为轮廓外形建议；黑色长方形也可能是其他物品，不作为可靠位置记录。</span> : !candidate.accepted && <span>{recognitionRejection(candidate.rejection_reason)}</span>}</p>) : <p>本帧没有模型确认的相关候选；这不代表物品一定不在画面中。</p>}
        {hiddenShapeSuggestions && <p className="photo-help">另有轮廓建议，未通过物品检测；仅在“查看其他候选”中展示，不更新位置。</p>}
      </div>
      {validCandidates.length > 0 && <button className="button ghost small" type="button" aria-expanded={showAll} onClick={() => setShowAll(value => !value)}>{showAll ? '收起其他候选' : `查看其他候选（本帧共 ${validCandidates.length} 个）`}</button>}
      <p className="photo-help">{!primary && <SourceBadge record={{ ...frame, source_type: frame.source_type as SourceType }} />} · 帧 {frame.source_frame} · {formatTime(frame.source_timestamp)} · 框与原图同帧</p>
      {!frame.display_only && <PipelineReadout frame={frame} items={registeredItems} />}
      {!frame.display_only && <details className="vision-hand-details"><summary>同帧手部分析 · {labels[frame.hand_status?.status || ''] || '状态待报告'}</summary>
        <p>本帧 {frame.hands.length} 只手 · 每只最多 21 个模型关键点</p>
        {(frame.hand_actions || frame.interactions) && ['ready', 'no_hands'].includes(frame.hand_status?.status || '') && <><HandActionSummary hands={frame.hand_status?.status === 'no_hands' ? [] : frame.hand_actions || []} interactions={frame.interactions || []} /><p className="photo-help">手势来自同帧关键点几何解释，不证明触觉抓握或确认放下。</p></>}
        {frame.hand_status?.error && <p role="alert">{frame.hand_status.error}</p>}
      </details>}
    </>}
    {continuous && active && <details className="vision-hand-details" open={modelDetails} onToggle={event => setModelDetails(event.currentTarget.open)}><summary>查看模型原帧与手部关键点</summary><p>手势在独立模型帧上分析，不把过期手部关键点贴到新视频上。</p>{modelDetails && <VisionFrame cameraId={cameraId} active={active} continuous={false} showHands={showHands} registeredItems={registeredItems} />}</details>}
  </section>
}
