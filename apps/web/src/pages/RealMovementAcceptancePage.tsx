import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Camera,
  CheckCircle2,
  Circle,
  Clock3,
  ExternalLink,
  ImageOff,
  LoaderCircle,
  MoveRight,
  Play,
  RefreshCw,
  ShieldAlert,
  Smartphone,
  XCircle,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Badge, PageHeader } from '../components/UI'
import { MobileAccess, safeMobileUrl } from '../components/MobileAccess'
import { acceptanceApi, api, mediaUrl } from '../lib/api'
import type {
  AcceptanceCamera,
  AcceptanceMarkerStatus,
  AcceptancePreflight,
  AcceptanceRegion,
  AcceptanceRegionKey,
  AcceptanceRun,
  AcceptanceScenarioKind,
  AcceptanceSuite,
  Camera as CameraRecord,
  Item,
  Zone,
} from '../types'

interface AcceptanceScenario {
  kind: AcceptanceScenarioKind
  label: string
  shortLabel: string
  instruction: string
  expectedFrom?: AcceptanceRegionKey
  expectedTo?: AcceptanceRegionKey
  minimumDurationSeconds?: number
  crossZone: boolean
}

export const ACCEPTANCE_SCENARIOS: AcceptanceScenario[] = [
  { kind: 'stationary', label: '静止 5 分钟', shortLabel: '静止', instruction: '手机保持在 A 区不动。可以让手从旁边经过，但不要碰手机。', expectedFrom: 'A', expectedTo: 'A', minimumDurationSeconds: 300, crossZone: false },
  { kind: 'minor_adjustment', label: '同区轻微调整', shortLabel: '轻调', instruction: '只在 A 区内小幅转动或挪动手机，不要跨出 A 区。', expectedFrom: 'A', expectedTo: 'A', minimumDurationSeconds: 15, crossZone: false },
  { kind: 'movement', label: '跨区 1 · A → B', shortLabel: 'A→B ①', instruction: '从 A 区拿起手机，移动到 B 区并放稳。', expectedFrom: 'A', expectedTo: 'B', crossZone: true },
  { kind: 'movement', label: '跨区 2 · B → A', shortLabel: 'B→A ②', instruction: '从 B 区拿起手机，移回 A 区并放稳。', expectedFrom: 'B', expectedTo: 'A', crossZone: true },
  { kind: 'movement', label: '跨区 3 · A → B', shortLabel: 'A→B ③', instruction: '再次从 A 区移动到 B 区，放稳后等待后端判断。', expectedFrom: 'A', expectedTo: 'B', crossZone: true },
  { kind: 'movement', label: '跨区 4 · B → A', shortLabel: 'B→A ④', instruction: '再次从 B 区移动到 A 区，放稳后等待后端判断。', expectedFrom: 'B', expectedTo: 'A', crossZone: true },
  { kind: 'occlusion', label: '移动中遮挡', shortLabel: '遮挡', instruction: '从 A 区拿起并完全遮住手机，不要让它在新位置重新出现。后端不应猜测“确认放下”。', expectedFrom: 'A', minimumDurationSeconds: 8, crossZone: false },
  { kind: 'disconnect_reconnect', label: '受控断开与重连', shortLabel: '重连', instruction: '保持手机不动，让系统执行受控断开与重连。重连后需要重新建立稳定基线，不能生成移动事件。', expectedFrom: 'A', expectedTo: 'A', crossZone: false },
  { kind: 'movement', label: '跨区 5 · A → B', shortLabel: 'A→B ⑤', instruction: '从 A 区移动到 B 区并放稳。', expectedFrom: 'A', expectedTo: 'B', crossZone: true },
  { kind: 'movement', label: '跨区 6 · B → A', shortLabel: 'B→A ⑥', instruction: '从 B 区移动到 A 区并放稳。', expectedFrom: 'B', expectedTo: 'A', crossZone: true },
  { kind: 'movement', label: '跨区 7 · A → B', shortLabel: 'A→B ⑦', instruction: '从 A 区移动到 B 区并放稳。', expectedFrom: 'A', expectedTo: 'B', crossZone: true },
  { kind: 'movement', label: '跨区 8 · B → A', shortLabel: 'B→A ⑧', instruction: '从 B 区移动到 A 区并放稳。', expectedFrom: 'B', expectedTo: 'A', crossZone: true },
  { kind: 'movement', label: '跨区 9 · A → B', shortLabel: 'A→B ⑨', instruction: '从 A 区移动到 B 区并放稳。', expectedFrom: 'A', expectedTo: 'B', crossZone: true },
  { kind: 'movement', label: '跨区 10 · B → A', shortLabel: 'B→A ⑩', instruction: '最后一次从 B 区移动到 A 区并放稳。', expectedFrom: 'B', expectedTo: 'A', crossZone: true },
]

export function runMatchesScenarioContract(
  run: AcceptanceRun,
  scenario: AcceptanceScenario,
  scenarioIndex: number,
  itemId: string,
  cameraId: string,
  zoneIds: { A: string; B: string },
  suiteId: string,
  contractVersion: string,
) {
  const expectedZone = (key?: AcceptanceRegionKey) => key === 'B' ? zoneIds.B : zoneIds.A
  const id = String(run.id || '')
  return Boolean(
    id
    && run.validation_run_id === id
    && run.suite_id === suiteId
    && run.contract_version === contractVersion
    && Number(run.scenario_index) === scenarioIndex + 1
    && String(run.trial_kind || '').toLowerCase() === scenario.kind
    && Number(run.minimum_duration_seconds) === (scenario.minimumDurationSeconds || 0)
    && run.item_id === itemId
    && run.camera_id === cameraId
    && String(run.runtime_mode || '').toUpperCase() === 'REAL'
    && String(run.source_type || '').toLowerCase() === 'opencv_camera'
    && run.is_simulated === false
    && run.origin_zone_id === expectedZone(scenario.expectedFrom)
    && run.destination_zone_id === expectedZone(scenario.expectedTo || scenario.expectedFrom),
  )
}

function suiteMatchesContract(suite: AcceptanceSuite | null) {
  if (!suite || !Array.isArray(suite.contract) || !Array.isArray(suite.runs)) return false
  const runs = [...suite.runs].sort((left, right) => Number(left.scenario_index) - Number(right.scenario_index))
  return Boolean(
    suite.id && suite.id === suite.suite_id
    && suite.contract_version === 'p0-real-movement-v1'
    && String(suite.runtime_mode).toUpperCase() === 'REAL'
    && String(suite.source_type).toLowerCase() === 'opencv_camera'
    && suite.is_simulated === false
    && suite.zone_a?.id === suite.zone_a_id && suite.zone_b?.id === suite.zone_b_id
    && suite.zone_a_id !== suite.zone_b_id
    && suite.contract.length === ACCEPTANCE_SCENARIOS.length
    && suite.contract.every((entry, index) => (
      Number(entry.scenario_index) === index + 1
      && entry.trial_kind === ACCEPTANCE_SCENARIOS[index].kind
      && entry.from_zone === ACCEPTANCE_SCENARIOS[index].expectedFrom
      && entry.to_zone === (ACCEPTANCE_SCENARIOS[index].expectedTo || ACCEPTANCE_SCENARIOS[index].expectedFrom)
      && Number(entry.minimum_duration_seconds) === (ACCEPTANCE_SCENARIOS[index].minimumDurationSeconds || 0)
    ))
    && runs.length <= ACCEPTANCE_SCENARIOS.length
    && new Set(runs.map(runId)).size === runs.length
    && runs.every((run, index) => (
      runMatchesScenarioContract(run, ACCEPTANCE_SCENARIOS[index], index, suite.item_id, suite.camera_id,
        { A: suite.zone_a_id, B: suite.zone_b_id }, suite.id, suite.contract_version)
      && (index === runs.length - 1 || terminal(run))
    ))
  )
}

export const DEFAULT_ACCEPTANCE_REGIONS: AcceptanceRegion[] = [
  { key: 'A', name: '桌面左侧', x: 0.07, y: 0.14, width: 0.37, height: 0.72 },
  { key: 'B', name: '桌面右侧', x: 0.56, y: 0.14, width: 0.37, height: 0.72 },
]

export function regionsOverlap(a: AcceptanceRegion, b: AcceptanceRegion) {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
}

export function normalizedRegion(key: AcceptanceRegionKey, name: string, startX: number, startY: number, endX: number, endY: number): AcceptanceRegion {
  const clamp = (value: number) => Math.max(0, Math.min(1, value))
  const left = clamp(Math.min(startX, endX)); const top = clamp(Math.min(startY, endY))
  const right = clamp(Math.max(startX, endX)); const bottom = clamp(Math.max(startY, endY))
  return { key, name, x: left, y: top, width: right - left, height: bottom - top }
}

function regionFromZone(key: AcceptanceRegionKey, zone: { name: string; points: number[][] }): AcceptanceRegion | null {
  if (!zone.points?.length || zone.points.some((point) => point.length !== 2)) return null
  const xs = zone.points.map((point) => Number(point[0])); const ys = zone.points.map((point) => Number(point[1]))
  if ([...xs, ...ys].some((value) => !Number.isFinite(value))) return null
  return normalizedRegion(key, zone.name, Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys))
}

function runId(run: AcceptanceRun | null | undefined) {
  return String(run?.id || run?.validation_run_id || run?.run_id || '')
}

function runPhase(run: AcceptanceRun | null | undefined) {
  const status = String(run?.status || '').toLowerCase()
  if (['passed', 'failed', 'cancelled', 'camera_interrupted'].includes(status)) return status
  const engineState = String(run?.engine_status?.state || '').toUpperCase()
  const enginePhase: Record<string, string> = {
    STABLE_AT_ORIGIN: 'stable_a',
    MOVEMENT_STARTED: 'moving',
    IN_TRANSIT: 'moving',
    ENTERED_DESTINATION: 'stable_b',
    STABILIZING: 'stable_b',
    MOVEMENT_CONFIRMED: 'evaluating',
    OCCLUDED: 'occluded',
    ABORTED: 'failed',
    CAMERA_INTERRUPTED: 'camera_interrupted',
  }
  const phase = String(run?.phase || run?.current_phase || enginePhase[engineState] || run?.progress?.phase || status || 'created').toLowerCase()
  return status === 'active' && phase === 'created' ? 'calibrating' : phase
}

function terminal(run: AcceptanceRun | null | undefined) {
  const status = String(run?.status || '').toLowerCase()
  return ['passed', 'failed', 'cancelled', 'camera_interrupted'].includes(status)
}

function verdict(run: AcceptanceRun | null | undefined): boolean | null {
  const outcome = String(run?.outcome || '').toUpperCase()
  const kind = String(run?.trial_kind || '').toLowerCase()
  if (!terminal(run)) return null
  if (run?.score_eligible === false) return false
  if (outcome === 'PASSED' && String(run?.status).toUpperCase() === 'PASSED') {
    if (kind === 'movement') {
      const evidenceId = run?.event_id || run?.result?.evidence?.event_id || run?.result?.event?.event_id
      return evidenceId ? true : null
    }
    return true
  }
  if (['FAILED', 'INTERRUPTED'].includes(outcome)) return false
  return null
}

function phaseCopy(run: AcceptanceRun | null | undefined) {
  const copy: Record<string, { title: string; detail: string }> = {
    created: { title: '本轮已建立，尚未开始', detail: '需要你明确点击开始；刷新页面不会自动启动。' },
    calibrating: { title: '正在确认识别稳定', detail: '让手机和 Marker 完整出现在画面中。' },
    stable_a: { title: '正在观察起点是否稳定', detail: '先别移动，等后端取得连续稳定帧。' },
    moving: { title: '现在执行本轮动作', detail: '按下方说明移动、遮挡或保持静止。' },
    stable_b: { title: '正在确认最终状态', detail: '保持不动，系统正在等待新位置稳定。' },
    occluded: { title: '目标当前被遮挡', detail: '后端不会把遮挡猜成确认放下。按本轮说明等待真实结论。' },
    evaluating: { title: '后端正在核对证据', detail: '数据库、截图和短视频必须来自同一视频会话。' },
    passed: { title: '本轮后端判定已返回', detail: '结果来自后端验收记录，不是页面自行判断。' },
    camera_interrupted: { title: '摄像头链路被中断', detail: '本轮已如实占用当前序号且不通过；不能挑选另一条历史结果替换。' },
    failed: { title: '本轮未通过', detail: '失败已写入当前套件并占用本轮；本套验收不能重试这一序号。' },
    cancelled: { title: '本轮已取消', detail: '取消已写入当前套件并占用本轮；本套验收不能重试这一序号。' },
  }
  return copy[runPhase(run)] || { title: '后端正在处理本轮', detail: '等待后端返回可验证状态。' }
}

function scenarioExpectation(scenario: AcceptanceScenario, regions: AcceptanceRegion[]) {
  const name = (key?: AcceptanceRegionKey) => regions.find((region) => region.key === key)?.name || key || '—'
  if (scenario.kind === 'occlusion') return `${name(scenario.expectedFrom)} → 遮挡，不能确认放下`
  if (scenario.kind === 'disconnect_reconnect') return '位置不变，重连后重新建立基线'
  if (!scenario.crossZone) return `${name(scenario.expectedFrom)} 内，不应创建移动事件`
  return `${name(scenario.expectedFrom)} → ${name(scenario.expectedTo)}`
}

function companionMarkerUrl(base: string | null | undefined, itemId: string, cameraId: string, validationRunId: string) {
  try {
    if (!base) return ''
    const value = new URL(base)
    value.pathname = '/companion/validation-marker'
    value.search = ''
    if (itemId) value.searchParams.set('item', itemId)
    if (cameraId) value.searchParams.set('camera', cameraId)
    if (validationRunId) value.searchParams.set('run', validationRunId)
    return safeMobileUrl(value.toString())
  } catch {
    return ''
  }
}

function backendCameras(preflight: AcceptancePreflight | null): AcceptanceCamera[] {
  if (!preflight) return []
  if (preflight.cameras?.length) return preflight.cameras
  if (preflight.camera) return [preflight.camera]
  if (preflight.camera_id) return [{
    id: preflight.camera_id,
    name: preflight.camera_name || preflight.camera_id,
    source_type: preflight.source_type,
    runtime_mode: preflight.runtime_mode,
    is_simulated: preflight.is_simulated,
    detector_backend: preflight.detector_backend,
    status: preflight.status,
  }]
  return []
}

export function RealMovementAcceptancePage() {
  const [params, setParams] = useSearchParams()
  const initialSuiteId = useMemo(() => (params.get('suite') || '').trim(), [])
  const [preflight, setPreflight] = useState<AcceptancePreflight | null>(null)
  const [preflightError, setPreflightError] = useState('')
  const [cameraInventory, setCameraInventory] = useState<CameraRecord[]>([])
  const [items, setItems] = useState<Item[]>([])
  const [itemId, setItemId] = useState(params.get('item') || '')
  const [cameraId, setCameraId] = useState(params.get('camera') || '')
  const [zoneIds, setZoneIds] = useState<{ A: string; B: string }>({ A: params.get('zoneA') || '', B: params.get('zoneB') || '' })
  const [regions, setRegions] = useState<AcceptanceRegion[]>(DEFAULT_ACCEPTANCE_REGIONS)
  const [activeRegion, setActiveRegion] = useState<AcceptanceRegionKey>('A')
  const [draft, setDraft] = useState<AcceptanceRegion | null>(null)
  const [regionError, setRegionError] = useState('')
  const [setupStep, setSetupStep] = useState(0)
  const [markerStatus, setMarkerStatus] = useState<AcceptanceMarkerStatus | null>(null)
  const [markerError, setMarkerError] = useState('')
  const [suiteId, setSuiteId] = useState(initialSuiteId)
  const [suite, setSuite] = useState<AcceptanceSuite | null>(null)
  const [suiteLoading, setSuiteLoading] = useState(Boolean(initialSuiteId))
  const [runError, setRunError] = useState('')
  const [busy, setBusy] = useState(false)
  const [frameNonce, setFrameNonce] = useState(Date.now())
  const [frameFailed, setFrameFailed] = useState(false)
  const canvasRef = useRef<SVGSVGElement>(null)
  const drawStart = useRef<{ x: number; y: number } | null>(null)
  const orderedRuns = useMemo(
    () => [...(suite?.runs || [])].sort((left, right) => Number(left.scenario_index || 0) - Number(right.scenario_index || 0)),
    [suite],
  )
  const currentRun = orderedRuns.at(-1) || null
  const currentRunId = runId(currentRun)

  useEffect(() => {
    const next = suiteId ? new URLSearchParams({ suite: suiteId }) : new URLSearchParams(params)
    next.delete('runs'); next.delete('current')
    if (next.toString() !== params.toString()) setParams(next, { replace: true })
  }, [params, setParams, suiteId])

  const refreshPreflight = useCallback(async () => {
    if (!cameraId) { setPreflight(null); return }
    try {
      const value = await acceptanceApi.preflight(cameraId)
      setPreflight(value); setPreflightError('')
    } catch (error) {
      setPreflight(null)
      setPreflightError(error instanceof Error ? error.message : '无法读取真实模式预检')
    }
  }, [cameraId])

  useEffect(() => {
    void refreshPreflight()
    const timer = window.setInterval(() => void refreshPreflight(), 5000)
    return () => window.clearInterval(timer)
  }, [refreshPreflight])

  useEffect(() => {
    let cancelled = false
    api<CameraRecord[]>('/api/cameras').then((value) => {
      if (cancelled) return
      setCameraInventory(value)
      setCameraId((current) => current || value.find((camera) => camera.source_type_provenance === 'opencv_camera' && camera.is_simulated === false)?.id || '')
    }).catch((error) => { if (!cancelled) setPreflightError(error instanceof Error ? error.message : '无法读取摄像头列表') })
    return () => { cancelled = true }
  }, [])

  const refreshItems = useCallback(async () => {
    try {
      const value = await api<Item[]>('/api/items')
      setItems(value)
      setItemId((current) => current || (value.find((item) => /手机|phone/i.test(`${item.name} ${item.type} ${item.aliases?.join(' ') || ''}`)) || value[0])?.id || '')
    } catch (error) {
      setRunError(error instanceof Error ? error.message : '无法读取物品')
    }
  }, [])

  useEffect(() => { void refreshItems() }, [refreshItems]) // Initial load remains GET-only.

  const cameras = useMemo(() => {
    const combined = new Map<string, AcceptanceCamera>()
    cameraInventory.forEach((camera) => combined.set(camera.id, {
      id: camera.id,
      name: camera.name,
      room_name: camera.room_name,
      source_type: camera.source_type_provenance || camera.source_type,
      runtime_mode: camera.runtime_mode,
      is_simulated: camera.is_simulated,
      status: camera.health?.status,
    }))
    backendCameras(preflight).forEach((camera) => {
      const inventoryCamera = combined.get(camera.id)
      combined.set(camera.id, { ...inventoryCamera, ...camera, source_type: inventoryCamera?.source_type || camera.source_type })
    })
    return [...combined.values()]
  }, [cameraInventory, preflight])
  useEffect(() => {
    if (!cameraId && cameras[0]) setCameraId(cameras[0].id)
  }, [cameraId, cameras])
  const selectedCamera = useMemo(() => cameras.find((camera) => camera.id === cameraId) || null, [cameraId, cameras])
  const selectedItem = useMemo(() => items.find((item) => item.id === itemId), [itemId, items])
  const markerAssigned = selectedItem?.aruco_id !== null && selectedItem?.aruco_id !== undefined

  useEffect(() => {
    if (!cameraId || !zoneIds.A || !zoneIds.B) return
    let cancelled = false
    api<Zone[]>(`/api/cameras/${encodeURIComponent(cameraId)}/zones`).then((value) => {
      if (cancelled) return
      const zoneA = value.find((zone) => zone.id === zoneIds.A); const zoneB = value.find((zone) => zone.id === zoneIds.B)
      const regionA = zoneA ? regionFromZone('A', zoneA) : null; const regionB = zoneB ? regionFromZone('B', zoneB) : null
      if (regionA && regionB && !regionsOverlap(regionA, regionB)) setRegions([regionA, regionB])
    }).catch(() => { /* The preflight and run endpoints remain the authority for missing zones. */ })
    return () => { cancelled = true }
  }, [cameraId, zoneIds.A, zoneIds.B])

  useEffect(() => {
    setMarkerStatus(null)
    setMarkerError('')
    if (!itemId || !cameraId) return
    let cancelled = false
    let request = 0
    const load = () => {
      const currentRequest = ++request
      return acceptanceApi.markerStatus({ validationRunId: currentRunId || undefined, itemId, cameraId }).then((value) => {
        if (!cancelled && currentRequest === request) { setMarkerStatus(value); setMarkerError('') }
      }).catch((error) => {
        if (!cancelled && currentRequest === request) {
          setMarkerStatus(null)
          setMarkerError(error instanceof Error ? error.message : '识别状态暂时不可用')
        }
      })
    }
    void load(); const timer = window.setInterval(() => void load(), 1500)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [cameraId, currentRunId, itemId])

  const applySuite = useCallback((value: AcceptanceSuite) => {
    const id = String(value?.id || '')
    if (!id || value.suite_id !== id || !Array.isArray(value.runs) || !Array.isArray(value.contract) || !value.zone_a || !value.zone_b || !value.summary) throw new Error('后端验收套件身份或结构不一致，页面已拒绝加载。')
    setSuite(value)
    setSuiteId(id)
    setItemId(value.item_id)
    setCameraId(value.camera_id)
    setZoneIds({ A: value.zone_a_id, B: value.zone_b_id })
    const regionA = regionFromZone('A', value.zone_a)
    const regionB = regionFromZone('B', value.zone_b)
    if (regionA && regionB && !regionsOverlap(regionA, regionB)) setRegions([regionA, regionB])
  }, [])

  const mergeRun = useCallback((value: AcceptanceRun) => {
    const id = runId(value)
    if (!id) return
    setSuite((current) => current ? {
      ...current,
      runs: [...current.runs.filter((run) => runId(run) !== id), value]
        .sort((left, right) => Number(left.scenario_index || 0) - Number(right.scenario_index || 0)),
    } : current)
  }, [])

  useEffect(() => {
    if (!suiteId) { setSuiteLoading(false); return }
    let cancelled = false
    const load = () => acceptanceApi.suite(suiteId).then((value) => {
      if (cancelled) return
      if (value.id !== suiteId) throw new Error('后端返回了其他验收套件，页面已拒绝加载。')
      applySuite(value)
      setRunError('')
      setSuiteLoading(false)
    }).catch((error) => {
      if (cancelled) return
      setRunError(error instanceof Error ? error.message : '验收套件暂时不可用')
      setSuiteLoading(false)
    })
    void load()
    const shouldPoll = !suite || Boolean(currentRun && !terminal(currentRun))
    const timer = shouldPoll ? window.setInterval(() => void load(), 1000) : undefined
    return () => { cancelled = true; if (timer !== undefined) window.clearInterval(timer) }
  }, [applySuite, currentRun?.status, suite?.id, suiteId]) // Restore and live state both come from one server-owned suite.

  useEffect(() => {
    if (!cameraId) return
    const timer = window.setInterval(() => setFrameNonce(Date.now()), 1600)
    return () => window.clearInterval(timer)
  }, [cameraId])

  const preflightReady = preflight?.ready === true
    && String(preflight.runtime_mode || selectedCamera?.runtime_mode || '').toUpperCase() === 'REAL'
    && String(selectedCamera?.source_type || preflight.source_type || '').toLowerCase() === 'opencv_camera'
    && (selectedCamera?.is_simulated ?? preflight.is_simulated) === false
    && Boolean(cameraId)
  const markerRecognized = Boolean(
    markerStatus?.server_observed === true
    && markerStatus.item_id === itemId
    && (!markerStatus.validation_run_id || markerStatus.validation_run_id === currentRunId)
    && (!markerStatus.camera_id || markerStatus.camera_id === cameraId)
    && String(markerStatus.runtime_mode || '').toUpperCase() === 'REAL'
    && String(markerStatus.source_type || '').toLowerCase() === 'opencv_camera'
    && markerStatus.is_simulated === false
    && markerStatus.recognized === true,
  )
  const markerStableState = String(markerStatus?.stability?.diagnostics?.state || markerStatus?.stability?.state || '').toUpperCase()
  const markerStable = markerRecognized
    && Boolean(markerStatus?.stability?.diagnostics?.baseline)
    && ['VISIBLE_STATIC', 'PLACED_CONFIRMED'].includes(markerStableState)
  const regionsValid = regions.length === 2 && regions.every((region) => region.width >= 0.08 && region.height >= 0.08 && Boolean(region.name.trim())) && !regionsOverlap(regions[0], regions[1])
  const runScenarioIndexes = new Map<string, number>()
  orderedRuns.forEach((entry) => {
    const supplied = Number(entry.scenario_index)
    if (Number.isInteger(supplied) && supplied >= 1 && supplied <= ACCEPTANCE_SCENARIOS.length) {
      runScenarioIndexes.set(runId(entry), supplied - 1)
    }
  })
  const contractValidRuns = orderedRuns.filter((run) => {
    const index = runScenarioIndexes.get(runId(run))
    return index !== undefined && suite !== null && runMatchesScenarioContract(
      run,
      ACCEPTANCE_SCENARIOS[index],
      index,
      suite.item_id,
      suite.camera_id,
      { A: suite.zone_a_id, B: suite.zone_b_id },
      suite.id,
      suite.contract_version,
    )
  })
  const suiteContractValid = suiteMatchesContract(suite)
  const countedRuns = suiteContractValid ? contractValidRuns : []
  const finishedRuns = countedRuns.filter(terminal)
  const serverNextScenario = Number(suite?.summary.next_scenario_index)
  const nextScenarioIndex = Number.isInteger(serverNextScenario) && serverNextScenario >= 1 && serverNextScenario <= ACCEPTANCE_SCENARIOS.length
    ? serverNextScenario - 1
    : -1
  const currentScenarioIndex = Math.min(
    runScenarioIndexes.get(currentRunId) ?? Math.max(0, nextScenarioIndex),
    ACCEPTANCE_SCENARIOS.length - 1,
  )
  const currentScenario = ACCEPTANCE_SCENARIOS[currentScenarioIndex]
  const crossCorrect = Number(suite?.summary.movement_passed || 0)
  const crossTotal = Number(suite?.summary.movement_total || 0)
  const crossRequired = Number(suite?.summary.movement_required || 8)
  const negativeCorrect = Number(suite?.summary.negative_passed || 0)
  const negativeTotal = Number(suite?.summary.negative_total || 0)
  const negativeRequired = Number(suite?.summary.negative_required || 4)
  const allScenariosComplete = suiteContractValid && suite?.summary.contract_complete === true && finishedRuns.length === ACCEPTANCE_SCENARIOS.length
  const accuracyComplete = allScenariosComplete
  const overallPassed = allScenariosComplete && suite?.overall_passed === true
  const companionUrl = companionMarkerUrl(currentRun?.companion_url || currentRun?.lan_url || preflight?.companion_url || preflight?.lan_url, itemId, cameraId, currentRunId)

  function canvasPoint(event: React.PointerEvent<SVGSVGElement>) {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect?.width || !rect.height) return null
    return { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) }
  }

  function startDrawing(event: React.PointerEvent<SVGSVGElement>) {
    const point = canvasPoint(event); if (!point) return
    event.currentTarget.setPointerCapture?.(event.pointerId)
    drawStart.current = point
    const current = regions.find((region) => region.key === activeRegion)!
    setDraft(normalizedRegion(activeRegion, current.name, point.x, point.y, point.x, point.y))
    setRegionError('')
  }

  function moveDrawing(event: React.PointerEvent<SVGSVGElement>) {
    const point = canvasPoint(event); const start = drawStart.current
    if (!point || !start) return
    const current = regions.find((region) => region.key === activeRegion)!
    setDraft(normalizedRegion(activeRegion, current.name, start.x, start.y, point.x, point.y))
  }

  function finishDrawing(event: React.PointerEvent<SVGSVGElement>) {
    const point = canvasPoint(event); const start = drawStart.current
    drawStart.current = null; setDraft(null)
    if (!point || !start) return
    const existing = regions.find((region) => region.key === activeRegion)!
    const candidate = normalizedRegion(activeRegion, existing.name, start.x, start.y, point.x, point.y)
    const other = regions.find((region) => region.key !== activeRegion)!
    if (candidate.width < 0.08 || candidate.height < 0.08) { setRegionError('区域太小，请拖出至少占画面宽高 8% 的范围。'); return }
    if (regionsOverlap(candidate, other)) { setRegionError('A、B 区不能重叠，请留出清楚的间隔。'); return }
    setRegions((value) => value.map((region) => region.key === activeRegion ? candidate : region))
  }

  function updateRegionName(key: AcceptanceRegionKey, name: string) {
    setRegions((value) => value.map((region) => region.key === key ? { ...region, name } : region))
  }

  function updateSetupUrl(savedZones = zoneIds) {
    const next = new URLSearchParams()
    if (itemId) next.set('item', itemId)
    if (cameraId) next.set('camera', cameraId)
    if (savedZones.A) next.set('zoneA', savedZones.A)
    if (savedZones.B) next.set('zoneB', savedZones.B)
    setParams(next, { replace: true })
  }

  function updateSuiteUrl(id: string) {
    setParams(new URLSearchParams({ suite: id }), { replace: true })
  }

  async function saveAcceptanceZones() {
    if (!cameraId || !regionsValid) return
    setBusy(true); setRegionError('')
    try {
      const saved = { ...zoneIds }
      for (const region of regions) {
        const points: [number, number][] = [
          [region.x, region.y],
          [region.x + region.width, region.y],
          [region.x + region.width, region.y + region.height],
          [region.x, region.y + region.height],
        ].map(([x, y]) => [Number(x.toFixed(4)), Number(y.toFixed(4))])
        const body = { name: region.name.trim(), points, priority: region.key === 'A' ? 100 : 90, enabled: true }
        const existingId = saved[region.key]
        const value = existingId
          ? await api<Zone>(`/api/zones/${encodeURIComponent(existingId)}`, { method: 'PATCH', json: body })
          : await api<Zone>(`/api/cameras/${encodeURIComponent(cameraId)}/zones`, { method: 'POST', json: body })
        saved[region.key] = value.id
      }
      setZoneIds(saved)
      updateSetupUrl(saved)
      setSetupStep(3)
      await refreshPreflight()
    } catch (error) {
      setRegionError(error instanceof Error ? error.message : '保存 A / B 区域失败')
    } finally {
      setBusy(false)
    }
  }

  async function beginScenario(index: number) {
    const scenario = ACCEPTANCE_SCENARIOS[index]
    if (!scenario || !preflightReady || !markerAssigned || !markerStable || !regionsValid || !itemId || !cameraId || !zoneIds.A || !zoneIds.B) return
    setBusy(true); setRunError('')
    try {
      let activeSuite = suite
      if (!activeSuite) {
        activeSuite = await acceptanceApi.createSuite({
          item_id: itemId,
          camera_id: cameraId,
          zone_a_id: zoneIds.A,
          zone_b_id: zoneIds.B,
        })
        applySuite(activeSuite)
        updateSuiteUrl(activeSuite.id)
      }
      if (!suiteMatchesContract(activeSuite)) throw new Error('后端验收套件合同不一致，页面已拒绝创建下一轮。')
      const expectedIndex = Number(activeSuite.summary.next_scenario_index)
      if (expectedIndex !== index + 1) {
        throw new Error(`服务端固定的下一轮是第 ${Number.isFinite(expectedIndex) ? expectedIndex : '—'} 轮，页面不会跳轮或重试。`)
      }
      const created = await acceptanceApi.createRun({
        suite_id: activeSuite.id,
        scenario_index: index + 1,
      })
      const id = runId(created)
      if (!id) throw new Error('后端没有返回验收 run_id，本轮未启动。')
      if (!runMatchesScenarioContract(
        created,
        scenario,
        index,
        activeSuite.item_id,
        activeSuite.camera_id,
        { A: activeSuite.zone_a_id, B: activeSuite.zone_b_id },
        activeSuite.id,
        activeSuite.contract_version,
      )) {
        throw new Error('后端返回的本轮记录与验收套件合同不一致，页面已拒绝启动。')
      }
      mergeRun(created)
      const started = await acceptanceApi.startRun(id)
      mergeRun({ ...created, ...started })
    } catch (error) {
      setRunError(error instanceof Error ? error.message : '本轮启动失败')
    } finally {
      setBusy(false)
    }
  }

  async function startExisting() {
    if (!currentRunId || !markerStable || !suiteContractValid) return
    setBusy(true)
    try {
      const started = await acceptanceApi.startRun(currentRunId)
      mergeRun({ ...currentRun, ...started }); setRunError('')
    }
    catch (error) { setRunError(error instanceof Error ? error.message : '本轮启动失败') }
    finally { setBusy(false) }
  }

  async function cancelCurrent() {
    if (!currentRunId || terminal(currentRun)) return
    setBusy(true)
    try { mergeRun({ ...currentRun, ...await acceptanceApi.cancelRun(currentRunId) }); setRunError('') }
    catch (error) { setRunError(error instanceof Error ? error.message : '取消失败') }
    finally { setBusy(false) }
  }

  async function triggerDisconnectReconnect() {
    if (!currentRunId || !markerStable || !suiteContractValid || String(currentRun?.trial_kind).toLowerCase() !== 'disconnect_reconnect' || terminal(currentRun)) return
    setBusy(true)
    try { mergeRun({ ...currentRun, ...await acceptanceApi.disconnectReconnect(currentRunId) }); setRunError('') }
    catch (error) { setRunError(error instanceof Error ? error.message : '受控断开与重连没有成功启动') }
    finally { setBusy(false) }
  }

  function setupPanel() {
    if (setupStep === 0) return <section className="acceptance-setup-panel panel">
      <div className="section-heading"><div><p className="eyebrow">第一步</p><h2>先确认本机 OpenCV 正在连续取流</h2><p>后端会校验 REAL、opencv_camera、非模拟和连续帧；数字索引本身不能硬件证明它不是系统虚拟摄像头，请同时亲眼确认画面来自当前物理镜头。</p></div><button type="button" className="button secondary small" onClick={() => void refreshPreflight()}><RefreshCw />重新预检</button></div>
      <div className="acceptance-preflight-grid">
        <div className={String(preflight?.runtime_mode).toUpperCase() === 'REAL' ? 'passed' : 'blocked'}><span>运行模式</span><strong>{preflight?.runtime_mode || '未回报'}</strong></div>
        <div className={String(selectedCamera?.source_type || preflight?.source_type).toLowerCase() === 'opencv_camera' ? 'passed' : 'blocked'}><span>视频来源</span><strong>{selectedCamera?.source_type || preflight?.source_type || '未回报'}</strong></div>
        <div className={(selectedCamera?.is_simulated ?? preflight?.is_simulated) === false ? 'passed' : 'blocked'}><span>模拟数据</span><strong>{(selectedCamera?.is_simulated ?? preflight?.is_simulated) === false ? '未使用' : '未排除'}</strong></div>
        <div className={preflight?.ready === true ? 'passed' : 'blocked'}><span>后端预检</span><strong>{preflight?.ready === true ? '可以开始' : '尚未通过'}</strong></div>
      </div>
      {preflightError && <div className="acceptance-callout danger"><AlertTriangle /><span>{preflightError}</span></div>}
      {!preflightReady && <div className="acceptance-callout warning"><ShieldAlert /><div><strong>当前不能作为真实验收</strong><span>{preflight?.message || '请切换 REAL 模式，并在摄像头页启动 source_type=opencv_camera 的本机摄像头。'}</span>{preflight?.blockers?.map((blocker) => <small key={blocker}>• {blocker}</small>)}</div></div>}
      <label className="field"><span className="field-label">用于验收的摄像头</span><select aria-label="用于验收的摄像头" value={cameraId} onChange={(event) => { setCameraId(event.target.value); setZoneIds({ A: '', B: '' }) }}><option value="">选择后端确认的摄像头</option>{cameras.map((camera) => <option key={camera.id} value={camera.id}>{camera.name || camera.id} · {camera.source_type || '来源未知'}</option>)}</select></label>
      <div className="acceptance-setup-footer"><Link className="button secondary" to="/cameras">去摄像头页</Link><button type="button" className="button primary" disabled={!preflightReady} onClick={() => setSetupStep(1)}>下一步：选择物品<ArrowRight /></button></div>
    </section>

    if (setupStep === 1) return <section className="acceptance-setup-panel panel">
      <div className="section-heading"><div><p className="eyebrow">第二步</p><h2>选择真实要移动的物品</h2><p>建议使用“我的手机”，并在这台真实手机的屏幕上全屏显示对应 ArUco Marker。</p></div></div>
      <div className="acceptance-item-grid">{items.map((item) => <button type="button" key={item.id} className={item.id === itemId ? 'selected' : ''} onClick={() => setItemId(item.id)}><Smartphone /><span><strong>{item.name}</strong><small>{item.aruco_id === null ? 'Marker 尚未分配' : `Marker ID ${item.aruco_id}`}</small></span>{item.id === itemId ? <CheckCircle2 /> : <Circle />}</button>)}</div>
      {!items.length && <div className="acceptance-callout warning"><AlertTriangle /><span>后端没有返回可选物品，请先在物品管理中注册手机。</span></div>}
      <div className="acceptance-setup-footer"><button type="button" className="button secondary" onClick={() => setSetupStep(0)}><ArrowLeft />上一步</button><button type="button" className="button primary" disabled={!selectedItem} onClick={() => setSetupStep(2)}>下一步：划分 A / B<ArrowRight /></button></div>
    </section>

    if (setupStep === 2) {
      const visibleRegions = regions.map((region) => draft?.key === region.key ? draft : region)
      const frameUrl = selectedCamera?.frame_url || `/api/cameras/${encodeURIComponent(cameraId)}/frame?t=${frameNonce}`
      return <section className="acceptance-zone-panel panel">
        <div className="section-heading"><div><p className="eyebrow">第三步</p><h2>在画面上划出不重叠的 A、B 区</h2><p>默认是桌面左侧和右侧。先选 A 或 B，再在画面中拖动重画；坐标按 0–1 保存。</p></div><div className="acceptance-region-switch"><button type="button" className={activeRegion === 'A' ? 'active a' : ''} onClick={() => setActiveRegion('A')}>编辑 A</button><button type="button" className={activeRegion === 'B' ? 'active b' : ''} onClick={() => setActiveRegion('B')}>编辑 B</button></div></div>
        <div className="acceptance-camera-canvas">
          {!frameFailed ? <img src={frameUrl} alt="本机 OpenCV 当前画面，用于划分验收区域" onLoad={() => setFrameFailed(false)} onError={() => setFrameFailed(true)} /> : <div className="acceptance-no-frame"><ImageOff /><strong>暂时没有实时帧</strong><span>区域仍可编辑，但开始验收前必须恢复 opencv 摄像头。</span></div>}
          <svg ref={canvasRef} viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-label="拖动画出 A 或 B 区域" onPointerDown={startDrawing} onPointerMove={moveDrawing} onPointerUp={finishDrawing} onPointerCancel={() => { drawStart.current = null; setDraft(null) }}>
            {visibleRegions.map((region) => <g key={region.key} className={`acceptance-region-shape region-${region.key.toLowerCase()} ${activeRegion === region.key ? 'active' : ''}`}><rect x={region.x * 1000} y={region.y * 1000} width={region.width * 1000} height={region.height * 1000} /><text x={(region.x + 0.02) * 1000} y={(region.y + 0.07) * 1000}>{region.key} · {region.name}</text></g>)}
          </svg>
        </div>
        {regionError && <p className="inline-error acceptance-region-error">{regionError}</p>}
        <div className="acceptance-region-fields">{regions.map((region) => <label key={region.key}><span><b>{region.key}</b> 区名称</span><input aria-label={`${region.key} 区名称`} value={region.name} onChange={(event) => updateRegionName(region.key, event.target.value)} /><small>x {region.x.toFixed(3)} · y {region.y.toFixed(3)} · 宽 {region.width.toFixed(3)} · 高 {region.height.toFixed(3)}</small></label>)}</div>
        <div className="acceptance-setup-footer"><button type="button" className="button secondary" onClick={() => setSetupStep(1)}><ArrowLeft />上一步</button><button type="button" className="button primary" disabled={!regionsValid || busy} onClick={() => void saveAcceptanceZones()}>{busy ? <LoaderCircle className="spin" /> : <ArrowRight />}{busy ? '正在保存并等待摄像头重启…' : zoneIds.A && zoneIds.B ? '更新区域并校准' : '保存区域并校准'}</button></div>
      </section>
    }

    return <section className="acceptance-setup-panel panel">
      <div className="section-heading"><div><p className="eyebrow">第四步</p><h2>让后端稳定识别手机 Marker</h2><p>先在真实手机上全屏打开 Marker 并放入 A 区。本页会用 item_id 和 camera_id 轮询当前 OpenCV 管线；连续稳定前不能开始。</p></div><Badge tone={markerStable ? 'success' : 'warning'}>{markerStable ? '后端已稳定识别' : markerAssigned ? '等待稳定识别' : '尚未绑定'}</Badge></div>
      <div className="acceptance-calibration-layout">
        <div className={`acceptance-marker-readout ${markerStable ? 'recognized' : ''}`}>{markerStable ? <CheckCircle2 /> : <Camera />}<div><strong>{markerStable ? `后端已稳定识别 ${selectedItem?.name || '目标物品'}` : markerAssigned ? '已绑定，但后端还没有稳定识别' : '先在手机页绑定 Marker'}</strong><span>{markerStatus?.message || markerError || '识别、连续帧和稳定基线都以后端摄像头结果为准。'}</span><small>Marker {selectedItem?.aruco_id ?? '未分配'} · 状态 {markerStableState || '未回报'} · 页面不会自行判定。</small></div></div>
        <CompanionAccess url={companionUrl} />
      </div>
      <div className="acceptance-run-actions"><Link className="button secondary" to={`/companion/validation-marker?item=${encodeURIComponent(itemId)}&camera=${encodeURIComponent(cameraId)}`} target="_blank">在本机预览标签页<ExternalLink /></Link><button type="button" className="button secondary" onClick={() => void refreshItems()}><RefreshCw />刷新绑定状态</button></div>
      <div className="acceptance-callout info"><Clock3 /><div><strong>开始后会连续完成 14 轮</strong><span>包含 5 分钟静止、同区轻调、10 次交替跨区、遮挡和受控断开重连。每轮都等后端给出真实结果。</span></div></div>
      <div className="acceptance-setup-footer"><button type="button" className="button secondary" onClick={() => setSetupStep(2)}><ArrowLeft />上一步</button><button type="button" className="button primary" disabled={!markerAssigned || !markerStable || busy || !preflightReady || !zoneIds.A || !zoneIds.B} onClick={() => void beginScenario(0)}>{busy ? <LoaderCircle className="spin" /> : <Play />}{busy ? '正在建立第一轮…' : '开始第一轮真实验收'}</button></div>
    </section>
  }

  function runPanel() {
    const copy = phaseCopy(currentRun)
    const scenario = currentScenario || ACCEPTANCE_SCENARIOS[Math.min(nextScenarioIndex, ACCEPTANCE_SCENARIOS.length - 1)]
    const currentRunContractValid = Boolean(
      currentRun
      && suite
      && scenario
      && runMatchesScenarioContract(
        currentRun,
        scenario,
        currentScenarioIndex,
        suite.item_id,
        suite.camera_id,
        { A: suite.zone_a_id, B: suite.zone_b_id },
        suite.id,
        suite.contract_version,
      ),
    )
    const currentVerdict = currentRunContractValid ? verdict(currentRun) : null
    const evidence = currentRunContractValid && currentRun?.evidence_retained !== false ? currentRun?.result?.evidence || currentRun?.result?.event || currentRun?.evidence : null
    const eventCount = currentRun?.result?.movement_event_count ?? currentRun?.result?.event_count
    const stable = currentRun?.stable_frames; const required = currentRun?.required_stable_frames
    const terminalCurrent = terminal(currentRun)
    const ringSeconds = Number(currentRun?.engine_status?.ring_seconds || 0)
    const requiredPreSeconds = Number(currentRun?.engine_status?.required_pre_seconds || 5)
    const baselineReady = Boolean(currentRun?.engine_status?.baseline)
    const movementReady = baselineReady && ringSeconds + 1e-6 >= requiredPreSeconds
    const waitingForMovementBuffer = Boolean(currentRun && !terminalCurrent && scenario?.kind === 'movement' && !movementReady)
    const liveInstruction = waitingForMovementBuffer
      ? `先不要移动：把手机静止留在${regions.find((region) => region.key === scenario?.expectedFrom)?.name || scenario?.expectedFrom || '起点'}，等稳定基线和至少 ${requiredPreSeconds.toFixed(0)} 秒事件前视频都准备好。`
      : scenario?.instruction
    return <div className="acceptance-session-layout">
      <aside className="acceptance-scenario-list panel" aria-label="验收场景进度">
        <div><p className="eyebrow">服务端固定套件</p><h2>14 轮场景</h2><span>失败、取消或中断都会占用原轮次，不能用另一条历史记录替换。</span></div>
        <ol>{ACCEPTANCE_SCENARIOS.map((entry, index) => {
          const completed = finishedRuns.find((run) => runScenarioIndexes.get(runId(run)) === index)
          const isCurrent = index === currentScenarioIndex && currentRun && !terminalCurrent
          const result = verdict(completed)
          return <li key={`${entry.kind}-${index}`} className={`${completed ? 'done' : ''} ${isCurrent ? 'active' : ''} ${result === false ? 'failed' : ''}`}><span>{completed ? result === true ? <CheckCircle2 /> : <XCircle /> : isCurrent ? <RefreshCw /> : index + 1}</span><div><strong>{entry.shortLabel}</strong><small>{completed ? result === true ? '后端判定通过' : result === false ? '后端判定未通过' : '后端无明确结论' : isCurrent ? '正在进行' : '尚未开始'}</small></div></li>
        })}</ol>
      </aside>

      <div className="acceptance-session-main">
        <section className="acceptance-live-card panel" aria-live="polite">
          <div className="acceptance-live-head"><div><p className="eyebrow">第 {Math.min(currentScenarioIndex + 1, ACCEPTANCE_SCENARIOS.length)} / {ACCEPTANCE_SCENARIOS.length} 轮</p><h2>{scenario?.label || '等待本轮信息'}</h2></div><Badge tone={currentVerdict === true ? 'success' : currentVerdict === false ? 'danger' : terminalCurrent ? 'warning' : 'blue'}>{currentVerdict === true ? '真实结果通过' : currentVerdict === false ? '真实结果未通过' : terminalCurrent ? '等待明确结论' : '后端观察中'}</Badge></div>
          <div className={`acceptance-plain-status ${currentVerdict === true ? 'success' : currentVerdict === false ? 'danger' : ''}`}>{currentVerdict === true ? <CheckCircle2 /> : currentVerdict === false ? <XCircle /> : <RefreshCw />}<div><strong>{copy.title}</strong><span>{currentRun?.message || copy.detail}</span></div></div>
          <div className={`acceptance-marker-readout ${markerRecognized ? 'recognized' : ''}`}>{markerRecognized ? <CheckCircle2 /> : <Camera />}<div><strong>{markerRecognized ? '本轮后端已识别手机 Marker' : '本轮后端尚未识别 Marker'}</strong><span>{markerStatus?.message || markerError || '识别状态按当前 validation_run_id 从后端摄像头管线读取。'}</span><small>后端帧：{markerStatus?.source_frame ?? markerStatus?.frame ?? '—'} · 来源：{markerStatus?.source_type || '未回报'}</small></div></div>
          <div className="acceptance-instruction"><MoveRight /><div><span>现在怎么做</span><strong>{liveInstruction}</strong><small>本轮期望：{scenario ? scenarioExpectation(scenario, regions) : '等待配置'}</small></div></div>
          {waitingForMovementBuffer && <div className="acceptance-callout warning"><Clock3 /><div><strong>还不能移动</strong><span>{baselineReady ? '稳定基线已建立，继续积累事件前视频。' : '后端还没有建立稳定起点基线。'} 页面会等真实媒体缓存达到要求再显示移动指令。</span></div></div>}
          {currentRun && !terminalCurrent && <div className="acceptance-frame-progress"><span style={{ width: `${Math.min(100, Math.round(ringSeconds / Math.max(0.001, requiredPreSeconds) * 100))}%` }} /><small>事件前媒体缓存 {ringSeconds.toFixed(1)} / {requiredPreSeconds.toFixed(1)} 秒</small></div>}
          {typeof stable === 'number' && typeof required === 'number' && <div className="acceptance-frame-progress"><span style={{ width: `${Math.min(100, Math.round(stable / Math.max(1, required) * 100))}%` }} /><small>稳定帧 {stable} / {required}</small></div>}
          {runError && <div className="acceptance-callout danger"><AlertTriangle /><span>{runError}</span></div>}
          {!suiteContractValid && <div className="acceptance-callout danger"><ShieldAlert /><div><strong>验收记录合同不一致</strong><span>检测到重复轮次，或混入了其他物品、摄像头、区域或场景类型的历史记录；本页不会计算通过。</span></div></div>}
          {terminalCurrent && <div className="acceptance-run-result-grid"><div><span>本轮结论</span><strong>{currentVerdict === true ? '通过' : currentVerdict === false ? '未通过' : '后端未给明确结论'}</strong></div><div><span>移动事件数</span><strong>{typeof eventCount === 'number' ? eventCount : '未回报'}</strong></div><div><span>实际变化</span><strong>{currentRun?.result?.actual_from || evidence?.from_zone || '—'} → {currentRun?.result?.actual_to || evidence?.to_zone || '—'}</strong></div></div>}
          {evidence && <div className="acceptance-evidence-links"><span>真实证据</span>{evidence.event_id && <code>{evidence.event_id}</code>}{evidence.screenshot_path && <a href={mediaUrl(evidence.screenshot_path)} target="_blank" rel="noreferrer">关键截图<ExternalLink /></a>}{evidence.clip_path && <a href={mediaUrl(evidence.clip_path)} target="_blank" rel="noreferrer">事件短视频<ExternalLink /></a>}</div>}
          {currentRunContractValid && currentRun?.evidence_integrity_status === 'policy_deleted' && <div className="acceptance-callout info"><Clock3 /><span>此轮历史证据经后端验证通过，媒体已按保留策略清理；当前不能再查看原截图和短视频。</span></div>}
          {currentRunContractValid && currentRun?.evidence_integrity_status === 'missing_or_tampered' && <div className="acceptance-callout danger"><ShieldAlert /><span>此轮证据缺失或校验失败，后端已将其排除出通过计数。</span></div>}
          <div className="acceptance-run-actions">
            {!currentRun && suiteContractValid && nextScenarioIndex >= 0 && <button type="button" className="button primary" disabled={busy || !preflightReady || !markerAssigned || !markerStable} onClick={() => void beginScenario(nextScenarioIndex)}>{busy ? <LoaderCircle className="spin" /> : <Play />}{busy ? '正在建立…' : `建立固定第 ${nextScenarioIndex + 1} 轮`}</button>}
            {currentRun && String(currentRun.status || '').toUpperCase() === 'CREATED' && <button type="button" className="button primary" disabled={busy || !markerStable || !suiteContractValid} onClick={() => void startExisting()}><Play />{markerStable ? '明确开始本轮' : '先等后端稳定识别'}</button>}
            {currentRun && String(currentRun.status || '').toUpperCase() === 'ACTIVE' && String(currentRun.trial_kind).toLowerCase() === 'disconnect_reconnect' && !currentRun.progress?.controlled_disconnect && <button type="button" className="button primary" disabled={busy || !markerStable || !suiteContractValid} onClick={() => void triggerDisconnectReconnect()}><RefreshCw />{markerStable ? '执行受控断开与重连' : '先等本轮稳定识别'}</button>}
            {currentRun && !terminalCurrent && <button type="button" className="button secondary" disabled={busy} onClick={() => void cancelCurrent()}>取消本轮</button>}
            {terminalCurrent && suiteContractValid && nextScenarioIndex >= 0 && nextScenarioIndex < ACCEPTANCE_SCENARIOS.length && <button type="button" className="button primary" disabled={busy || !preflightReady || !markerAssigned || !markerStable} onClick={() => void beginScenario(nextScenarioIndex)}>{busy ? <LoaderCircle className="spin" /> : <ArrowRight />}{busy ? '正在建立…' : `开始下一轮：${ACCEPTANCE_SCENARIOS[nextScenarioIndex].shortLabel}`}</button>}
          </div>
        </section>

        <section className={`acceptance-scoreboard panel ${allScenariosComplete ? overallPassed ? 'passed' : 'failed' : ''}`}>
          <div><p className="eyebrow">服务端权威汇总</p><h2>跨区准确率</h2><span>页面只展示当前 suite 的后端 summary，不从 URL 或历史 run 自行拼分。</span></div>
          <div className="acceptance-score"><strong>{crossCorrect}</strong><span>/ {Math.min(crossTotal, 10)}</span><small>{accuracyComplete ? `后端计入 ${crossCorrect} / 10，门槛 ${crossRequired} / 10；总结果以后端总门禁为准` : `服务端尚有 ${Math.max(0, 10 - crossTotal)} 次跨区未建档`}</small></div>
          <div className="acceptance-score-meta"><span>负样本通过 <b>{negativeCorrect} / {negativeTotal}</b>（必须 {negativeRequired} / 4）</span><span>服务端终态 <b>{suite?.summary.terminal_runs || 0} / {ACCEPTANCE_SCENARIOS.length}</b></span><span>总门禁 <b>{!suiteContractValid ? '合同异常' : allScenariosComplete ? overallPassed ? '通过' : '不通过' : '未完成'}</b></span></div>
        </section>

        <CompanionAccess url={companionUrl} compact />

        <section className="acceptance-round-results panel">
          <div className="section-heading"><div><p className="eyebrow">逐轮证据</p><h2>每轮真实结果</h2><p>失败和无结论不会被页面包装成成功。</p></div></div>
          <div className="acceptance-results-table"><div className="header"><span>轮次</span><span>期望</span><span>后端结果</span><span>事件数</span></div>{finishedRuns.map((entry, index) => {
            const scenarioIndex = runScenarioIndexes.get(runId(entry)) ?? index
            const scenarioEntry = ACCEPTANCE_SCENARIOS[scenarioIndex]
            const result = verdict(entry)
            const count = entry.result?.movement_event_count ?? entry.result?.event_count
            return <div key={runId(entry) || index}><span>{scenarioEntry?.label || entry.trial_kind || `第 ${index + 1} 轮`}</span><span>{scenarioEntry ? scenarioExpectation(scenarioEntry, regions) : `${entry.expected_from_zone || '—'} → ${entry.expected_to_zone || '—'}`}</span><span className={result === true ? 'success-text' : result === false ? 'danger-text' : ''}>{result === true ? entry.evidence_integrity_status === 'policy_deleted' ? '历史通过 · 媒体按策略清理' : '通过' : result === false ? entry.evidence_integrity_status === 'missing_or_tampered' ? '证据无效 · 不计通过' : '未通过' : '无明确结论'}</span><span>{typeof count === 'number' ? count : '—'}</span></div>
          })}</div>
        </section>
      </div>
    </div>
  }

  return <div className="real-acceptance-page">
    <PageHeader eyebrow="P0 · 真实链路验收" title="真实移动验收" description="这不是演示页。通过表示 REAL 模式下的本机 OpenCV 闭环已完成；物理镜头身份仍需现场人员对照画面确认。" actions={<button type="button" className="button secondary" onClick={() => void refreshPreflight()}><RefreshCw />刷新预检</button>} />
    {suiteLoading
      ? <section className="acceptance-setup-panel panel"><div className="acceptance-plain-status"><LoaderCircle className="spin" /><div><strong>正在读取服务端验收套件</strong><span>刷新只凭 URL 中唯一的 suite ID 恢复，不读取 runs 列表。</span></div></div></section>
      : suiteId && !suite
        ? <section className="acceptance-setup-panel panel"><div className="acceptance-callout danger"><ShieldAlert /><div><strong>验收套件无法恢复</strong><span>{runError || '后端没有返回这一 suite；页面不会改用 URL 中的历史 run 拼装结果。'}</span></div></div></section>
        : !suite
          ? <><nav className="acceptance-setup-steps" aria-label="验收设置步骤">{['真实预检', '选择物品', 'A / B 区域', '识别校准'].map((label, index) => <div key={label} className={index === setupStep ? 'active' : index < setupStep ? 'done' : ''}><span>{index < setupStep ? <CheckCircle2 /> : index + 1}</span><strong>{label}</strong></div>)}</nav>{setupPanel()}</>
          : runPanel()}
  </div>
}

function CompanionAccess({ url, compact = false }: { url: string; compact?: boolean }) {
  return <section className="acceptance-phone-panel panel"><MobileAccess url={url} marker compact={compact} /></section>
}
