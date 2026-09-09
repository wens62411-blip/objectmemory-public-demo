import { Camera as CameraIcon, Check, ImagePlus, RefreshCw, ScanSearch, Trash2, Upload, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, mediaUrl } from '../lib/api'
import { formatTime } from '../lib/format'
import { isShapeCandidate, recognitionRejection } from '../lib/recognition'
import type { Camera, ImageRegion, Item, RecognitionProfile, RecognitionTest, ReferenceImage } from '../types'
import { Badge, Field, Loading, Modal } from './UI'
import { SourceBadge } from './ProvenanceBadge'
import { reportPollingError, useVisiblePolling } from '../hooks/useVisiblePolling'
import './PhotoRegistration.css'

const MAX_PHOTOS = 12
const MAX_PHOTO_BYTES = 12 * 1024 * 1024
const MAX_BATCH_BYTES = 64 * 1024 * 1024
const DEFAULT_REGION: ImageRegion = [0.1, 0.1, 0.8, 0.8]

export function profileIsLoaded(profile?: RecognitionProfile | null) {
  return profile?.registration_status === 'ready' && profile.profile_version > 0
    && profile.loaded_profile_version === profile.profile_version && Boolean(profile.loaded_camera_ids?.length)
    && (!profile.model_runtime || (profile.model_runtime.available === true && profile.model_runtime.status === 'ready' && !profile.model_runtime.error))
}

function ModelPreparationStatus({ profile }: { profile: RecognitionProfile | null }) {
  const preparation = profile?.model_preparation
  const runtime = profile?.model_runtime
  const preparationLabels: Record<string, string> = {
    not_prepared: '尚未准备本地模型', downloading: '正在下载权重', verifying: '正在校验模型文件',
    loading: '准备器正在加载模型', warming: '准备器正在预热模型', ready: '本地准备已完成',
    failed: '模型准备失败', unknown: '准备状态暂不可读',
  }
  const runtimeLabels: Record<string, string> = { not_loaded: '当前服务尚未加载', unavailable: '当前服务模型不可用', loading: '当前服务正在加载', warming: '当前服务正在预热', failed: '当前服务加载失败' }
  const runtimeReady = runtime?.available === true && runtime?.status === 'ready' && !runtime?.error
  const received = preparation?.downloaded_bytes
  const total = preparation?.total_bytes
  const byteProgress = typeof received === 'number' && Number.isFinite(received) && received >= 0 && typeof total === 'number' && Number.isFinite(total) && total > 0 && received <= total
  return <div className="model-preparation" aria-label="本地模型准备和服务加载状态">
    <div><h4>本地准备结果</h4><Badge tone={preparation?.status === 'failed' ? 'danger' : 'neutral'}>{preparation ? preparationLabels[preparation.status || 'unknown'] || `准备阶段：${preparation.status || '未报告'}` : '尚未读取准备状态'}</Badge>
      {preparation?.status === 'downloading' && byteProgress && <div className="model-download-progress"><progress aria-label="模型权重下载进度" value={received} max={total} /><span>{(received! / 1024 / 1024).toFixed(1)} / {(total! / 1024 / 1024).toFixed(1)} MB</span></div>}
      {preparation?.error && <p className="inline-error" role="alert">{preparation.error}</p>}
      <p className="photo-help">准备器最近记录{preparation?.updated_at ? `：${formatTime(preparation.updated_at)}` : '：未报告时间'}。完成表示本地文件与准备检查通过，不等于当前摄像头可识别。</p>
    </div>
    <div><h4>当前服务加载</h4><Badge tone={runtimeReady ? 'blue' : runtime?.error ? 'danger' : 'neutral'}>{runtimeReady ? '当前服务模型已加载' : runtime ? runtimeLabels[runtime.status || ''] || '当前服务模型状态待确认' : '尚未读取服务模型状态'}</Badge>
      {runtime?.error && <p className="inline-error" role="alert">{runtime.error}</p>}
      <p className="photo-help">{runtime?.model_id || preparation?.model_id || '模型型号尚未报告'}{runtime?.model_version || preparation?.model_version ? ` · ${runtime?.model_version || preparation?.model_version}` : ''}</p>
      <p className="photo-help">下载、准备器加载、当前服务加载和物品档案是不同步骤。刷新状态只读取结果，不会发起下载。</p>
    </div>
  </div>
}

export function profileStatusText(profile?: RecognitionProfile | null) {
  if (!profile) return '识别档案尚未确认'
  if (profileIsLoaded(profile)) return '已启用当前档案 · 识别效果待现场验证'
  switch (profile.registration_status) {
    case 'images_saved': return '照片已保存 · 等待建立档案'
    case 'building': return '正在建立识别档案'
    case 'ready': return '档案已就绪 · 实时摄像头尚未加载当前版本'
    case 'needs_photos': return '需要补充或确认照片'
    case 'failed': return '处理失败 · 请查看原因'
    default: return '识别档案状态待确认'
  }
}

function LocalPhoto({ file, index }: { file: File; index: number }) {
  const [url, setUrl] = useState('')
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const value = URL.createObjectURL(file)
    setUrl(value); setFailed(false)
    return () => URL.revokeObjectURL(value)
  }, [file])
  return failed ? <span className="photo-decode-error">无法预览，请更换照片</span> : <img src={url || undefined} alt={`待上传参考照片 ${index + 1}`} onError={() => setFailed(true)} />
}

export function PhotoFilePicker({ files, onChange, disabled = false, existingCount = 0 }: {
  files: File[]; onChange: (files: File[]) => void; disabled?: boolean; existingCount?: number
}) {
  const [error, setError] = useState('')
  function add(incoming: FileList | null) {
    if (!incoming?.length) return
    const next = [...files]
    const issues: string[] = []
    for (const file of Array.from(incoming)) {
      const validType = ['image/jpeg', 'image/png'].includes(file.type) || (!file.type && /\.(jpe?g|png)$/i.test(file.name))
      if (!validType) { issues.push(`${file.name}：仅支持 JPEG、PNG，请先转换格式。`); continue }
      if (!file.size || file.size > MAX_PHOTO_BYTES) { issues.push(`${file.name}：图片不能为空，且每张不得超过 12 MB。`); continue }
      if (next.some((value) => value.name === file.name && value.size === file.size && value.lastModified === file.lastModified)) { issues.push(`${file.name}：已经选择，不重复添加。`); continue }
      if (existingCount + next.length >= MAX_PHOTOS) { issues.push('每件物品最多保存 12 张参考照片，请先移除不需要的照片。'); continue }
      if (next.reduce((total, value) => total + value.size, file.size) > MAX_BATCH_BYTES) { issues.push('本次上传总量不能超过 64 MB，请分批上传。'); continue }
      next.push(file)
    }
    onChange(next)
    setError([...new Set(issues)].join(' '))
  }
  return <div className="reference-picker">
    <div className="reference-picker-actions">
      <label className={`button secondary photo-file-control ${disabled ? 'is-disabled' : ''}`}><ImagePlus />选择照片<input aria-label="选择参考照片" type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png" multiple disabled={disabled} onChange={(event) => { add(event.target.files); event.target.value = '' }} /></label>
      <label className={`button ghost photo-file-control ${disabled ? 'is-disabled' : ''}`}><CameraIcon />手机拍一张<input aria-label="用手机拍摄参考照片" type="file" accept="image/jpeg,image/png" capture="environment" disabled={disabled} onChange={(event) => { add(event.target.files); event.target.value = '' }} /></label>
      <span>{existingCount + files.length} / 12 张</span>
    </div>
    <p className="photo-help">一张即可开始；之后建议补充 6–12 张正面、背面、侧面和实际摆放视角。每张最多 12 MB，仅 JPEG / PNG；照片保存在这台电脑，不上传云端。</p>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {files.length > 0 && <div className="reference-pending-grid">{files.map((file, index) => <div key={`${file.name}-${file.size}-${file.lastModified}`}><LocalPhoto file={file} index={index} /><button type="button" disabled={disabled} onClick={() => onChange(files.filter((_, current) => current !== index))} aria-label={`移除待上传照片 ${index + 1}`}><X /></button><span>{file.name}</span></div>)}</div>}
  </div>
}

function validRegion(region: ImageRegion) {
  return region.every(Number.isFinite) && region[0] >= 0 && region[1] >= 0 && region[2] > 0 && region[3] > 0
    && region[0] + region[2] <= 1.00001 && region[1] + region[3] <= 1.00001
}

function boxStyle(region: ImageRegion) {
  return { left: `${region[0] * 100}%`, top: `${region[1] * 100}%`, width: `${region[2] * 100}%`, height: `${region[3] * 100}%` }
}

function RegionEditor({ reference, disabled, onConfirm, onDirtyChange }: { reference: ReferenceImage; disabled: boolean; onConfirm: (region: ImageRegion) => void; onDirtyChange: (dirty: boolean) => void }) {
  const [region, setRegion] = useState<ImageRegion>(reference.region || reference.suggested_regions?.[0]?.bbox || DEFAULT_REGION)
  const [dirty, setDirty] = useState(false)
  const [imageFailed, setImageFailed] = useState(false)
  const [imageReady, setImageReady] = useState(false)
  const origin = useRef<{ pointerId: number; point: [number, number] } | null>(null)
  function update(value: ImageRegion) { setRegion(value); setDirty(true); onDirtyChange(true) }
  function point(event: React.PointerEvent<HTMLDivElement>): [number, number] {
    const rect = event.currentTarget.getBoundingClientRect()
    return [Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))]
  }
  function drag(event: React.PointerEvent<HTMLDivElement>) {
    if (!origin.current || origin.current.pointerId !== event.pointerId) return
    const current = point(event), start = origin.current.point
    update([Math.min(start[0], current[0]), Math.min(start[1], current[1]), Math.abs(start[0] - current[0]), Math.abs(start[1] - current[1])])
  }
  return <div className="reference-editor">
    <div className="reference-target-image" aria-label="拖动框选照片中的注册物品" onPointerDown={(event) => {
      if (disabled || !imageReady || imageFailed || event.button !== 0) return
      origin.current = { pointerId: event.pointerId, point: point(event) }
      event.currentTarget.setPointerCapture(event.pointerId)
    }} onPointerMove={drag} onPointerUp={(event) => { drag(event); origin.current = null }} onPointerCancel={() => { origin.current = null }}>
      <img src={mediaUrl(reference.url || reference.path)} alt="待确认的原始参考照片" draggable={false} onLoad={() => setImageReady(true)} onError={() => { setImageFailed(true); setImageReady(false) }} />
      {imageReady && !imageFailed && validRegion(region) && <span className="reference-region-box" style={boxStyle(region)}><span>注册物品</span></span>}
    </div>
    {imageFailed && <p className="inline-error" role="alert">原始照片无法读取。请刷新或重新上传，不能确认无法查看的图片。</p>}
    <p className="photo-help">拖动框选，或使用下方数值调整。只保留目标本身，尽量避开手和背景；多个物体时，请明确选择正在注册的这一件。</p>
    {Boolean(reference.suggested_regions?.length) && <div className="reference-suggestions" aria-label="自动建议目标"><span>建议框（仍需确认）</span>{reference.suggested_regions!.map((suggestion, index) => <button type="button" className="button secondary small" key={index} disabled={disabled} onClick={() => update(suggestion.bbox)}>{isShapeCandidate(suggestion) ? `外形建议 ${index + 1}（需手动确认）` : `${suggestion.label || '候选物体'} ${index + 1}`}</button>)}</div>}
    {reference.suggested_regions?.some(isShapeCandidate) && <p className="photo-help">外形建议仅表示轮廓像手机，未确认类别和身份。请手动框选并确认照片中的目标；选择建议不会自动注册或更新位置。</p>}
    {reference.quality?.suggestion_error && <p className="photo-help">自动建议暂不可用：{reference.quality.suggestion_error}。仍可手动框选目标，不会假装已自动找到物品。</p>}
    <div className="reference-region-fields">{['左边距', '上边距', '宽度', '高度'].map((label, index) => <Field key={label} label={`${label}（%）`}><input type="number" min={0} max={100} step={0.1} value={Math.round(region[index] * 1000) / 10} disabled={disabled} onChange={(event) => { const next: ImageRegion = [...region]; next[index] = Number(event.target.value) / 100; update(next) }} /></Field>)}</div>
    {!validRegion(region) && <p className="inline-error" role="alert">框选不能超出照片，宽度和高度必须大于 0。</p>}
    <button type="button" className="button primary" disabled={disabled || !imageReady || imageFailed || !validRegion(region) || (reference.region_confirmed && !dirty)} onClick={() => onConfirm(region)}><Check />{reference.region_confirmed && !dirty ? '目标区域已确认' : '确认这是我的物品'}</button>
    {dirty && <button type="button" className="button ghost" disabled={disabled} onClick={() => { setRegion(reference.region || reference.suggested_regions?.[0]?.bbox || DEFAULT_REGION); setDirty(false); onDirtyChange(false) }}>放弃本次框选修改</button>}
    <p className="photo-help">确认只保存目标区域，不代表已经建立或加载识别档案。</p>
  </div>
}

function modelDescription(model: RecognitionTest['model']) {
  return typeof model === 'string' ? model : [model.model_id || model.id || model.name || '未报告模型', model.model_version || model.version].filter(Boolean).join(' · ')
}

function score(value?: number | null) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : '未报告' }

function recognitionVerdict(item: Item, result: RecognitionTest) {
  const semantic = result.candidates.filter(candidate => !isShapeCandidate(candidate))
  if (semantic.some((candidate) => candidate.accepted && candidate.best_item_id === item.id)) return `本帧接受了“${item.name}”的身份；这不是连续观察或位置更新的确认。`
  if (semantic.some((candidate) => candidate.best_item_id === item.id)) return `发现与“${item.name}”相似的候选，但身份尚未通过确认。请查看拒绝原因。`
  const phone = /手机|电话|phone/i.test(`${item.type} ${item.name}`)
  const phoneCandidate = semantic.some((candidate) => /^(cell phone|mobile phone|phone|手机)$/i.test(candidate.category.trim()))
  if (!phoneCandidate && result.candidates.some(isShapeCandidate)) return '外形像手机，未确认类别和身份，不更新位置'
  if (phone && !phoneCandidate) return '本帧没有手机类别候选，尚未进入手机身份匹配；其他家具候选不代表手机已被识别。'
  return `本帧尚未接受“${item.name}”的身份${result.candidates.length ? '；检测到了其他候选，请查看下方明细' : '；没有检测到候选物体'}。`
}

function RecognitionComparison({ item, reference, result, camera }: { item: Item; reference?: ReferenceImage; result: RecognitionTest; camera?: Camera }) {
  const [imageFailed, setImageFailed] = useState(false)
  const loadedCount = Array.isArray(result.loaded_profiles) ? result.loaded_profiles.length : result.loaded_profiles
  const validImage = /^data:image\/(jpeg|png);base64,/.test(result.image_data_url)
  return <section className="recognition-result" aria-label="现场识别测试结果">
    <div className="recognition-result-header"><Badge tone="blue">一次现场帧测试 · 不是准确率验收</Badge><SourceBadge record={result} /></div>
    <div className="recognition-verdict" role="status"><strong>{recognitionVerdict(item, result)}</strong><p>档案启用只表示已经参与匹配。只有连续真实观察通过后台证据门，物品页的位置才会更新；本次测试不直接创建位置记录。</p></div>
    <div className="recognition-compare">
      <figure><figcaption>{item.name} · 参考照片</figcaption>{reference ? <div className="reference-target-image"><img src={mediaUrl(reference.url || reference.path)} alt={`${item.name} 的已保存参考照片`} />{reference.region && <span className="reference-region-box" style={boxStyle(reference.region)} />}</div> : <p>尚无参考照片</p>}</figure>
      <figure><figcaption>{camera?.name || '摄像头'}本次快照 · 框与图来自同一帧</figcaption>{validImage && !imageFailed ? <div className="reference-target-image"><img src={result.image_data_url} alt="现场测试的摄像头原始快照" onError={() => setImageFailed(true)} />{result.candidates.map((candidate, index) => validRegion(candidate.bbox) && <span className={`reference-region-box ${candidate.accepted && !isShapeCandidate(candidate) ? '' : 'candidate-rejected'}`} key={index} style={boxStyle(candidate.bbox)}><span>候选 {index + 1}{isShapeCandidate(candidate) ? ' · 外形建议，未确认' : candidate.accepted ? ' · 身份接受' : ' · 未确认'}</span></span>)}</div> : <p className="inline-error" role="alert">本次快照无法读取，不能把候选结果当成可查看的图像证据。</p>}</figure>
    </div>
    <dl className="recognition-metrics"><div><dt>实际模型</dt><dd>{modelDescription(result.model)}</dd></div><div><dt>推理耗时</dt><dd>{Number.isFinite(result.latency_ms) ? `${result.latency_ms.toFixed(0)} ms` : '未报告'}</dd></div><div><dt>已加载物品档案</dt><dd>{loadedCount}</dd></div><div><dt>本帧候选</dt><dd>{result.candidates.length}</dd></div></dl>
    <p className="photo-help">匹配分数是外观相似度，不是正确率。身份被接受仍需不同视角、相似物品和遮挡场景验证。</p>
    {typeof result.model !== 'string' && result.model.status && result.model.status !== 'ready' && <p className="inline-error" role="alert">模型状态：{result.model.status}。{result.model.error || '请查看模型准备情况，不能把模型异常当成画面中没有物品。'}</p>}
    {!result.candidates.length && <p className="recognition-no-match">本帧没有检测到候选。请确认物品进入画面、细节清楚，再换一个与注册照片不同的视角测试。</p>}
    <div className="recognition-candidates">{result.candidates.map((candidate, index) => {
      const geometryOnly = isShapeCandidate(candidate)
      const accepted = !geometryOnly && candidate.accepted
      return <article key={index}><header><strong>候选 {index + 1} · {geometryOnly ? '外形像手机（非类别识别）' : candidate.category}</strong><Badge tone={accepted ? 'success' : 'warning'}>{geometryOnly ? '外形建议，身份未确认' : accepted ? '身份接受' : '身份未确认'}</Badge></header>
        {geometryOnly ? <p>外形像手机，未确认类别和身份，不更新位置。轮廓分数不是物品匹配分数，也不是正确率。</p> : <dl><div><dt>最佳匹配</dt><dd>{candidate.best_item_name || '无匹配物品'} · {score(candidate.best_score)}</dd></div><div><dt>第二候选</dt><dd>{candidate.second_item_name || '无第二候选'} · {score(candidate.second_score)}</dd></div><div><dt>分数差距</dt><dd>{score(candidate.margin)}</dd></div><div><dt>使用档案版本</dt><dd>{candidate.profile_version ?? '未报告'}</dd></div></dl>}
        {!accepted && !geometryOnly && <p>拒绝原因：{recognitionRejection(candidate.rejection_reason)}</p>}</article>
    })}</div>
    <details className="photo-source-details"><summary>查看来源帧信息</summary><p>会话：{result.source_session_id || '未报告'}<br />帧编号：{result.source_frame}<br />拍摄时间：{formatTime(result.source_timestamp)}</p></details>
  </section>
}

export function PhotoRegistration({ item, onClose, onChanged }: { item: Item; onClose: () => void; onChanged: () => void }) {
  const [references, setReferences] = useState(item.reference_images || [])
  const [profile, setProfile] = useState<RecognitionProfile | null>(null)
  const [cameras, setCameras] = useState<Camera[]>([])
  const [cameraId, setCameraId] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState(item.reference_images?.length ? `已保存 ${item.reference_images.length} 张参考照片。请确认目标区域；保存照片不等于识别已经验证。` : '')
  const [syncError, setSyncError] = useState('')
  const [result, setResult] = useState<RecognitionTest | null>(null)
  const [regionDirty, setRegionDirty] = useState(false)
  const alive = useRef(true)
  const operation = useRef(false)
  const revision = useRef(0)
  const base = `/api/items/${encodeURIComponent(item.id)}`
  const selected = references.find((reference) => reference.id === selectedId) || references[0]
  const camera = cameras.find((value) => value.id === cameraId)
  const confirmedCount = references.filter((reference) => reference.region_confirmed).length
  const registrationStep = !references.length ? 0 : confirmedCount !== references.length || regionDirty ? 1 : profile?.registration_status !== 'ready' ? 2 : 3
  const nextAction = [
    '还没有参考照片。先选择照片并保存；手机拍一张是上传文件，不会开启持续摄像头采集。',
    '照片已经保存。请逐张框选并确认目标；完成后点击“建立 / 更新识别档案”。',
    '目标区域已确认。下一步建立识别档案，照片才会参与当前模型匹配。',
    '注册档案已建立。下一步在选定相机前换个视角测试；实时加载与物品身份接受仍以后台结果为准。',
  ][registrationStep]

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const controller = signal ? null : new AbortController()
    const activeSignal = signal || controller!.signal
    const timer = controller ? window.setTimeout(() => controller.abort('read_timeout'), 8000) : undefined
    try {
      const [savedItem, currentProfile] = await Promise.all([api<Item>(base, { signal: activeSignal }), api<RecognitionProfile>(`${base}/profile`, { signal: activeSignal })])
      if (!alive.current || activeSignal.aborted) return
      setReferences(savedItem.reference_images || [])
      setProfile(currentProfile)
      setSyncError('')
      setRegionDirty(false)
      setSelectedId((current) => savedItem.reference_images?.some((reference) => reference.id === current) ? current : savedItem.reference_images?.[0]?.id || '')
    } finally { window.clearTimeout(timer) }
  }, [base])

  useEffect(() => {
    alive.current = true
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort('initial_timeout'), 8000)
    void Promise.all([refresh(controller.signal), api<Camera[]>('/api/cameras', { signal: controller.signal }).then((values) => {
      if (!controller.signal.aborted) { setCameras(values); setCameraId(values.find((value) => value.enabled)?.id || values[0]?.id || '') }
    })]).catch((value: unknown) => { if (!controller.signal.aborted || controller.signal.reason === 'initial_timeout') { setProfile(null); setError(controller.signal.reason === 'initial_timeout' ? '读取注册资料超时，请重试读取。' : value instanceof Error ? value.message : '读取注册资料失败') } }).finally(() => { window.clearTimeout(timeout); if (!controller.signal.aborted || controller.signal.reason === 'initial_timeout') setLoading(false) })
    return () => { alive.current = false; window.clearTimeout(timeout); controller.abort() }
  }, [refresh])

  useVisiblePolling(async (signal) => {
    const started = revision.current
    try {
      const [value, currentCameras] = await Promise.all([api<RecognitionProfile>(`${base}/profile`, { signal }), api<Camera[]>('/api/cameras', { signal })])
      if (signal.aborted || !alive.current || operation.current || started !== revision.current) return
      setProfile(value); setCameras(currentCameras); setSyncError('')
    } catch (value) {
      if (alive.current && !operation.current && started === revision.current && reportPollingError(signal)) {
        setProfile(null)
        setSyncError(signal.reason === 'poll_timeout' ? '状态读取超时；已保存资料不受影响。' : `状态同步失败：${value instanceof Error ? value.message : '暂时无法读取'}。已保存资料不受影响。`)
      }
    }
  }, { enabled: !busy && !loading, resetKey: base })

  async function run(label: string, action: () => Promise<void>, changesProfile = false) {
    if (operation.current) return
    operation.current = true; revision.current += 1; setBusy(label); setError('')
    if (changesProfile) { setProfile(null); setResult(null) }
    try { await action() }
    catch (value) { if (alive.current) setError(value instanceof Error ? value.message : '操作失败，请重试') }
    finally { operation.current = false; if (alive.current) setBusy('') }
  }

  function changed() { if (alive.current) onChanged() }

  async function syncSaved() {
    try { await refresh() }
    catch (value) { if (alive.current) { setProfile(null); setSyncError(`保存已完成，但状态同步失败：${value instanceof Error ? value.message : '读取失败'}。请重试读取，不必再次上传或确认。`) } }
  }

  function upload() {
    void run('正在保存照片…', async () => {
      const body = new FormData(); files.forEach((file) => body.append('files', file))
      const saved = await api<ReferenceImage[]>(`${base}/reference-images`, { method: 'POST', body })
      if (!alive.current) return
      setFiles([]); setSelectedId(saved[0]?.id || '')
      setReferences((current) => [...current.filter((reference) => !saved.some((value) => value.id === reference.id)), ...saved])
      setNotice(`本次 ${saved.length} 张照片已保存。请逐张确认目标区域，再建立识别档案。`)
      changed(); await syncSaved()
    }, true)
  }

  function capture() {
    void run('正在读取当前相机帧…', async () => {
      const saved = await api<ReferenceImage>(`${base}/reference-capture`, { method: 'POST', json: { camera_id: cameraId } })
      if (!alive.current) return
      setSelectedId(saved.id); setReferences((current) => [...current.filter((reference) => reference.id !== saved.id), saved])
      setNotice('当前帧已保存为参考照片，仍需确认目标区域。现场测试请换一个视角。')
      changed(); await syncSaved()
    }, true)
  }

  function confirm(region: ImageRegion) {
    if (!selected) return
    void run('正在保存目标区域…', async () => {
      const saved = await api<ReferenceImage>(`${base}/reference-images/${encodeURIComponent(selected.id)}`, { method: 'PATCH', json: { region, confirmed: true } })
      if (!alive.current) return
      setReferences((current) => current.map((reference) => reference.id === saved.id ? saved : reference)); setRegionDirty(false)
      setNotice('目标区域已确认。请建立或更新识别档案，让新照片参与匹配。')
      changed(); await syncSaved()
    }, true)
  }

  function remove(reference: ReferenceImage) {
    if (!window.confirm('删除这张参考照片？识别档案需要重新建立，其他照片和位置记录不受此操作影响。')) return
    void run('正在删除参考照片…', async () => {
      await api(`${base}/reference-images/${encodeURIComponent(reference.id)}`, { method: 'DELETE' })
      if (!alive.current) return
      setReferences((current) => current.filter((value) => value.id !== reference.id))
      setNotice('参考照片已删除，请重新建立识别档案。')
      changed(); await syncSaved()
    }, true)
  }

  function build() {
    void run('正在建立识别档案…', async () => {
      let value: RecognitionProfile
      try { value = await api<RecognitionProfile>(`${base}/profile/build`, { method: 'POST' }) }
      catch (buildError) {
        // A rejected build may still have persisted a failed profile and model
        // preparation detail. Read that state; never replace the original error.
        try { await refresh(); changed() } catch { /* Keep the original build failure. */ }
        throw buildError
      }
      if (!alive.current) return
      setProfile(value); changed()
      if (value.registration_status === 'failed') setError(value.error || '特征处理失败，档案尚不可用于识别。')
      else setNotice(value.registration_status === 'ready' ? `注册档案 v${value.profile_version} 已建立。${profileIsLoaded(value) ? '已启用匹配，识别效果仍待现场验证。' : '等待运行中的摄像头加载；这不代表已经认出物品。'}` : '后台正在处理档案，页面会继续读取实际结果。')
    }, true)
  }

  function test() {
    setResult(null)
    void run('正在对当前帧进行识别…', async () => {
      const value = await api<RecognitionTest>(`${base}/recognition-test`, { method: 'POST', json: { camera_id: cameraId } })
      if (!alive.current) return
      setResult(value)
      const currentProfile = await api<RecognitionProfile>(`${base}/profile`)
      if (alive.current) { setProfile(currentProfile); changed() }
    })
  }

  const steps = ['添加照片', '确认目标', '建立档案', '现场测试']
  return <Modal title={`${item.name} · 照片与识别`} description="让照片真正参与本地识别；先确认目标，再用不同视角测试。" wide onClose={() => { if (!operation.current) onClose() }}>
    <div className="photo-registration" aria-busy={Boolean(busy)}>
      <ol className="photo-registration-steps" aria-label="照片注册进度">{steps.map((label, index) => <li key={label} aria-current={registrationStep === index ? 'step' : undefined}><a href={`#registration-step-${index}`}>{index + 1} {label}</a></li>)}</ol>
      {!loading && <p className="photo-notice" role="status" aria-label="下一步注册操作">{nextAction} <a href={`#registration-step-${registrationStep}`}>前往：{steps[registrationStep]}</a></p>}
      {loading && <Loading label="正在读取已保存的照片和识别档案…" />}
      {error && <p className="inline-error" role="alert">{error}</p>}
      {notice && <p className="photo-notice" role="status">{notice}</p>}
      {syncError && <div className="photo-sync-warning" role="alert"><p>{syncError}</p><button className="button secondary small" type="button" disabled={Boolean(busy)} onClick={() => { void run('正在重试读取…', syncSaved) }}>重试读取已保存资料</button></div>}
      {busy && <p className="photo-progress" role="status">{busy}完成前请保持此窗口打开。</p>}
      <section id="registration-step-0" className="registration-section"><header><div><p className="eyebrow">01 · 参考照片</p><h3>先给它一个清楚的近照</h3></div><Badge>{references.length} 张已保存 · {references.length - confirmedCount} 张待确认</Badge></header>
        <PhotoFilePicker files={files} onChange={setFiles} disabled={Boolean(busy) || loading} existingCount={references.length} />
        {files.length > 0 && <button type="button" className="button primary" disabled={Boolean(busy)} onClick={upload}><Upload />上传 {files.length} 张照片</button>}
        <div className="reference-camera-controls"><Field label="用于抓拍和测试的相机"><select value={cameraId} disabled={Boolean(busy) || loading} onChange={(event) => { setCameraId(event.target.value); setResult(null) }}><option value="">请选择相机</option>{cameras.map((value) => <option key={value.id} value={value.id}>{value.name} · {value.room_name || '未设房间'}</option>)}</select></Field><button type="button" className="button secondary" disabled={!cameraId || Boolean(busy) || loading || references.length >= MAX_PHOTOS} onClick={capture}><CameraIcon />从当前相机抓拍</button></div>
        <p className="photo-help">只读取已启动相机的当前帧，不会擅自打开摄像头。{camera && <SourceBadge record={camera} />} <Link to="/cameras" onClick={() => { if (!operation.current) onClose() }}>去摄像头页面检查连接</Link></p>
      </section>
      <section id="registration-step-1" className="registration-section"><header><div><p className="eyebrow">02 · 目标确认</p><h3>告诉物忆，照片里要认的是哪一件</h3></div><Badge tone={confirmedCount ? 'blue' : 'neutral'}>{confirmedCount} / {references.length} 张已确认</Badge></header>
        {!references.length ? <p className="photo-empty">照片保存后，会在这里显示原图与建议框。上传不等于识别成功。</p> : <>
          <div className="reference-library" aria-label="已保存参考照片">{references.map((reference, index) => <div key={reference.id} className={reference.id === selected?.id ? 'selected' : ''}><button className="reference-select" type="button" aria-label={`选择参考照片 ${index + 1}`} aria-pressed={reference.id === selected?.id} disabled={Boolean(busy)} onClick={() => { if (reference.id === selected?.id) return; if (regionDirty && !window.confirm('当前框选尚未保存，放弃修改并切换照片？')) return; setRegionDirty(false); setSelectedId(reference.id) }}><img src={mediaUrl(reference.url || reference.path)} alt={`参考照片 ${index + 1}`} /><span>{reference.region_confirmed ? '已确认目标' : '待确认目标'}</span></button><button type="button" className="icon-button danger-icon" aria-label={`删除参考照片 ${index + 1}`} disabled={Boolean(busy)} onClick={() => remove(reference)}><Trash2 /></button></div>)}</div>
          {selected && <RegionEditor key={`${selected.id}:${JSON.stringify(selected.region)}:${selected.region_confirmed}`} reference={selected} disabled={Boolean(busy)} onConfirm={confirm} onDirtyChange={(dirty) => { setRegionDirty(dirty); if (dirty) setResult(null) }} />}
        </>}
      </section>
      <section id="registration-step-2" className="registration-section registration-profile"><header><div><p className="eyebrow">03 · 识别档案</p><h3>已保存 → 已建立 → 已启用，效果待验证</h3></div><Badge tone={profileIsLoaded(profile) ? 'blue' : profile?.registration_status === 'failed' ? 'danger' : 'neutral'}>{busy === '正在建立识别档案…' ? busy : profileStatusText(profile)}</Badge></header>
        <ModelPreparationStatus profile={profile} />
        <dl className="recognition-metrics"><div><dt>档案版本</dt><dd>{profile?.profile_version ?? '待确认'}</dd></div><div><dt>后台加载版本</dt><dd>{profile?.loaded_profile_version ?? '尚未加载'}</dd></div><div><dt>有效参考照片</dt><dd>{profile?.ready_reference_count ?? '待确认'}</dd></div><div><dt>实时加载相机数</dt><dd>{profile?.loaded_camera_ids?.length ?? 0}</dd></div></dl>
        <p className="photo-help">模型：{profile?.model_id || '尚未就绪'} {profile?.model_version || ''}。新添、删图或重新框选后，需要更新档案；仅保存图片不会自动开启识别。</p>
        {profile?.error && profile.error !== error && <p className="inline-error" role="alert">{profile.error}</p>}
        {Boolean(profile?.quality_warnings?.length) && <div className="photo-sync-warning" aria-label="参考照片质量提示"><strong>档案已保存，不代表候选已检出</strong>{profile!.quality_warnings!.filter(value => value.trim()).map((warning, index) => <p key={index}>{warning}</p>)}<p>这是参考照片的检测提示，不判定照片上传错误；请在下方查看真实现场帧。</p></div>}
        <div className="reference-picker-actions"><button type="button" className="button primary" disabled={Boolean(busy) || loading || regionDirty || !confirmedCount || confirmedCount !== references.length} onClick={build}><ScanSearch />建立 / 更新识别档案</button><button type="button" className="button secondary" disabled={Boolean(busy) || loading || regionDirty} onClick={() => { void run('正在刷新档案…', async () => { setProfile(null); await refresh() }) }}><RefreshCw />刷新状态</button></div>
        <p className="photo-help">建立成功后启用照片识别，标签仅用于诊断。后台会将新档案热加载到已运行的相机，不会重新打开镜头。</p>
        {regionDirty && <p className="photo-help">目标框有未保存的修改。请先确认目标或放弃本次修改，再建立档案或测试。</p>}
        {confirmedCount !== references.length && <p className="photo-help">请先逐张确认目标区域，避免把背景或另一件物品加入档案。</p>}
        {profile?.registration_status === 'ready' && !profileIsLoaded(profile) && <p className="photo-help">档案已就绪，可现场测试；实时摄像头尚未加载当前档案，不表示已开始持续识别。</p>}
        {camera && <p className="photo-help" role="status">当前选择：{camera.name} · {profileIsLoaded(profile) && profile?.loaded_camera_ids?.includes(camera.id) ? '已启用当前档案，等待现场验证' : '尚未确认此相机启用当前档案'}。相机状态：{camera.health?.status || '暂未报告'}。档案状态会在页面可见时自动刷新。</p>}
      </section>
      <section id="registration-step-3" className="registration-section"><header><div><p className="eyebrow">04 · 现场测试</p><h3>换个视角，看看它能否认出来</h3></div></header><p className="photo-help">把物品放到选定相机前，与参考照片保持不同角度或拍摄时间。测试直接读取当前帧，不会把上传照片作为测试画面。本面板不将一次匹配直接写成位置事件，持续识别仍遵循后台证据规则。</p><button type="button" className="button primary" disabled={Boolean(busy) || loading || regionDirty || !cameraId || profile?.registration_status !== 'ready'} onClick={test}><ScanSearch />测试当前画面识别</button>{!cameraId && <p className="photo-help">请先选择一个已启动的相机。</p>}{result && <RecognitionComparison key={`${result.source_session_id}:${result.source_frame}`} item={item} reference={selected} result={result} camera={camera} />}</section>
    </div>
  </Modal>
}
