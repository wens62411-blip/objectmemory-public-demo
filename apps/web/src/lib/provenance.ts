import type { EventRecord, FirmwareDevice, ResolvedRuntimeMode, RuntimeMode, SearchResult, SourceType } from '../types'
import { confidenceText } from './format'

type ProvenanceRecord = Pick<EventRecord, 'event_type' | 'source_type' | 'is_simulated' | 'runtime_mode' | 'evidence_status' | 'final_status' | 'manually_corrected'> & {
  source_type_provenance?: string
  hardware_type?: string
  config?: Record<string, unknown>
  identity_evidence?: EventRecord['identity_evidence']
}

// browser_camera carries client-supplied JPEG bytes.  The server can validate
// the stream but cannot prove that the browser obtained it from a physical
// camera, so it must never receive verified_real trust.
const realSources = new Set(['opencv_camera', 'esp32_real', 'authorized_screen_capture'])
const networkStreamSources = new Set(['rtsp', 'onvif', 'mjpeg'])
const videoSources = new Set(['video_file', 'video'])
const simulatedSources = new Set(['video_file', 'video', 'virtual_esp32', 'demo_seed', 'test_fixture', 'mock', 'test'])
const confirmedEvidenceStatuses = new Set(['confirmed', 'verified', 'real_verified', 'confirmed_placed', 'accepted'])
const rejectedFinalStatuses = new Set(['candidate', 'pending', 'unconfirmed', 'rejected', 'invalid', 'no_evidence', 'missing_media', 'media_expired'])
const observationTypes = new Set(['last_seen', 'seen', 'occluded', 'lost', 'offline', 'exited_view'])
const observationStatuses = new Set(['last_seen', 'seen', 'last-seen', 'observation_only', 'occluded', 'lost', 'offline', 'exited_view'])
const unavailableTypes = new Set(['occluded', 'lost', 'offline', 'exited_view'])

export type ProvenanceTrust = 'verified_real' | 'verified_simulated' | 'unknown'
export type DeviceHardwareCategory = 'physical' | 'virtual' | 'unknown'

function normalizedSource(record?: Partial<ProvenanceRecord> | null) {
  return String(record?.source_type_provenance || record?.source_type || '').trim().toLowerCase()
}

export function recordRuntimeMode(record?: Partial<ProvenanceRecord> | null): ResolvedRuntimeMode {
  const raw = String(record?.runtime_mode || '').trim().toUpperCase().replaceAll('-', '_')
  if (raw === 'REAL' || raw === 'NORMAL') return 'REAL'
  if (raw === 'DEMO' || raw === 'NO_HARDWARE_DEMO') return 'DEMO'
  if (raw === 'TEST' || raw === 'TESTING') return 'TEST'
  return 'UNKNOWN'
}

export function recordIsSimulated(record?: Partial<ProvenanceRecord> | null) {
  if (record?.is_simulated === true) return true
  const source = normalizedSource(record)
  return simulatedSources.has(source)
}

export function provenanceTrust(record?: Partial<ProvenanceRecord> | null): ProvenanceTrust {
  if (!record || typeof record.is_simulated !== 'boolean') return 'unknown'
  const mode = recordRuntimeMode(record)
  const source = normalizedSource(record)
  if (mode === 'UNKNOWN' || !source) return 'unknown'
  if (realSources.has(source)) {
    if (mode === 'REAL' && record.is_simulated === false) return 'verified_real'
    if ((mode === 'DEMO' || mode === 'TEST') && record.is_simulated === true) return 'verified_simulated'
    return 'unknown'
  }
  if (simulatedSources.has(source)) {
    return (mode === 'DEMO' || mode === 'TEST') && record.is_simulated === true ? 'verified_simulated' : 'unknown'
  }
  return 'unknown'
}

export function sourceCategory(record?: Partial<ProvenanceRecord> | null) {
  if (record?.manually_corrected || record?.event_type === 'manual_correction') return 'manual'
  const source = normalizedSource(record)
  if (networkStreamSources.has(source)) return 'network_stream'
  if (provenanceTrust(record) === 'unknown') return 'unknown'
  if (videoSources.has(source)) return 'video'
  if (source === 'virtual_esp32' || ((record?.hardware_type === 'virtual' || record?.config?.simulated === true) && source === 'esp32')) return 'virtual'
  if (source === 'demo_seed') return 'seed'
  if (['test_fixture', 'mock', 'test'].includes(source)) return 'test'
  if (source === 'authorized_screen_capture') return 'authorized_capture'
  if (realSources.has(source)) return 'real'
  return 'unknown'
}

export function sourceBadge(record?: Partial<ProvenanceRecord> | null): { label: string; tone: 'success' | 'warning' | 'danger' | 'neutral' | 'blue' } {
  const source = normalizedSource(record)
  if (source === 'browser_camera') return { label: '浏览器帧（来源未验证）', tone: 'warning' }
  if (source === 'opencv_camera' && provenanceTrust(record) === 'verified_real') return { label: '本机 OpenCV 来源', tone: 'success' }
  if (source === 'esp32_real' && provenanceTrust(record) === 'verified_real') return { label: '已验证物理 ESP32', tone: 'success' }
  switch (sourceCategory(record)) {
    case 'manual': return { label: '人工纠正', tone: 'blue' }
    case 'video': return { label: '测试视频', tone: 'warning' }
    case 'virtual': return { label: '虚拟设备', tone: 'warning' }
    case 'seed': return { label: '演示种子', tone: 'warning' }
    case 'test': return { label: '测试数据', tone: 'warning' }
    case 'authorized_capture': return { label: '授权窗口采集', tone: 'blue' }
    case 'network_stream': return { label: '网络视频流（物理来源未验证）', tone: 'warning' }
    case 'real': return { label: '非模拟视频来源', tone: 'success' }
    default: return { label: '来源未验证', tone: 'danger' }
  }
}

export function evidenceBadge(record?: Partial<ProvenanceRecord> | null): { label: string; tone: 'success' | 'warning' | 'danger' | 'neutral' | 'blue' } {
  if (!record) return { label: '暂无证据', tone: 'neutral' }
  const status = String(record.evidence_status || '').trim().toLowerCase()
  const finalStatus = String(record.final_status || '').trim().toLowerCase()
  if (status.includes('occlud') || record.event_type === 'occluded') return { label: '被遮挡', tone: 'warning' }
  if (record.event_type === 'lost' || status === 'lost') return { label: '已失去观察 · 当前位置未知', tone: 'warning' }
  if (record.event_type === 'offline' || status === 'offline') return { label: '视频源中断 · 当前位置未知', tone: 'warning' }
  if (record.event_type === 'exited_view' || status === 'exited_view') return { label: '离开视野 · 当前位置未知', tone: 'warning' }
  if (['last_seen', 'seen', 'last-seen', 'observation_only'].includes(status) || ['last_seen', 'seen'].includes(String(record.event_type))) return { label: '最后看到', tone: 'warning' }
  const trust = provenanceTrust(record)
  if (trust === 'unknown') return { label: '来源未验证', tone: 'danger' }
  if (finalStatus === 'position_changed') return { label: '位置变化，未确认放下', tone: 'warning' }
  const confirmedContract = confirmedEvidenceStatuses.has(status) && finalStatus === 'confirmed_placed'
  if ((record.manually_corrected || record.event_type === 'manual_correction') && confirmedContract) return { label: '人工纠正', tone: 'blue' }
  if (confirmedContract) {
    return trust === 'verified_real' ? { label: '真实确认', tone: 'success' } : { label: '模拟确认', tone: 'warning' }
  }
  if (finalStatus && rejectedFinalStatuses.has(finalStatus)) return { label: '无有效证据', tone: 'danger' }
  if (['candidate', 'pending', 'unconfirmed', 'needs_review'].includes(status)) return { label: trust === 'verified_real' ? '真实但未确认' : '模拟候选', tone: 'warning' }
  if (['invalid', 'rejected', 'no_evidence', 'missing_media'].includes(status)) return { label: '无有效证据', tone: 'danger' }
  return { label: '证据未验证', tone: 'danger' }
}

export function isRealCameraEvidence(record?: Partial<ProvenanceRecord> | null) {
  return provenanceTrust(record) === 'verified_real' && sourceCategory(record) === 'real'
}

export function isDisplayableRealObservation(record?: Partial<ProvenanceRecord> | null) {
  if (!record || recordRuntimeMode(record) !== 'REAL' || record.is_simulated !== false) return false
  if (!isObservationEvidence(record)) return false
  const source = normalizedSource(record)
  return source === 'browser_camera' || realSources.has(source) || networkStreamSources.has(source)
}

export function isObservationEvidence(record?: Partial<ProvenanceRecord> | null) {
  if (!record || record.identity_evidence?.accepted === false) return false
  return observationTypes.has(String(record.event_type || '').toLowerCase())
    && observationStatuses.has(String(record.evidence_status || '').toLowerCase())
    && !['rejected', 'invalid', 'no_evidence'].includes(String(record.final_status || '').toLowerCase())
}

export function isLocationUnavailable(record?: EventRecord | null) {
  return unavailableTypes.has(String(record?.event_type || '').toLowerCase())
}

export function acceptsLocationEvidence(record: EventRecord | null | undefined, mode: ResolvedRuntimeMode) {
  if (!record) return false
  if (mode === 'REAL') return isTrustedRealLocationEvidence(record) || isDisplayableRealObservation(record)
  if (mode !== 'DEMO' && mode !== 'TEST') return false
  return recordRuntimeMode(record) === mode && provenanceTrust(record) === 'verified_simulated'
    && (isObservationEvidence(record) || evidenceBadge(record).label === '模拟确认')
}

export function locationEvidenceTime(record?: EventRecord | null) {
  const value = Date.parse(record?.timestamp_end || record?.timestamp_start || '')
  return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY
}

export function confirmedLocationEvidence(result: SearchResult, mode: ResolvedRuntimeMode) {
  const evidence = result.last_confirmed_placement || result.last_confirmed
  return evidence?.item_id === result.item.id && acceptsLocationEvidence(evidence, mode) && !isObservationEvidence(evidence) ? evidence : null
}

/** Shared by search and item cards: provenance first, then event time. A missing
 * state retains the last real observation time, so a tied timestamp must still
 * invalidate an old placement as a claim about the current location. */
export function selectLocationEvidence(result: SearchResult | null | undefined, mode: ResolvedRuntimeMode) {
  if (!result || mode === 'UNKNOWN') return null
  const matches = (record?: EventRecord | null) => Boolean(record && record.item_id === result.item.id && acceptsLocationEvidence(record, mode)
    && (!isObservationEvidence(record) || Number.isFinite(locationEvidenceTime(record))))
  let primary = matches(result.evidence) ? result.evidence! : confirmedLocationEvidence(result, mode)
  const observations = [result.last_observed, result.last_seen, result.last_occluded, result.last_exited, result.evidence]
    .filter((record): record is EventRecord => matches(record) && isObservationEvidence(record) && Number.isFinite(locationEvidenceTime(record)))
    .sort((left, right) => locationEvidenceTime(right) - locationEvidenceTime(left) || Number(isLocationUnavailable(right)) - Number(isLocationUnavailable(left)))
  const observation = observations[0]
  if (observation && (!primary || locationEvidenceTime(observation) > locationEvidenceTime(primary)
    || (locationEvidenceTime(observation) === locationEvidenceTime(primary) && isLocationUnavailable(observation)))) primary = observation
  return primary && matches(primary) ? primary : null
}

export function locationPlace(evidence?: EventRecord | null) {
  return evidence ? [evidence.room_name, evidence.to_zone || evidence.zone_name || evidence.new_zone].filter(Boolean).join(' · ') || '区域尚未命名' : ''
}

export function locationConfidenceLabel(evidence?: EventRecord | null) {
  if (!evidence) return '可信度未知'
  const identity = evidence.identity_evidence
  if (evidence.detection_mode === 'experimental' || identity?.score_kind === 'cosine_similarity_not_probability' || identity?.model_id?.includes('dinov2')) {
    const value = identity?.best_score
    return typeof value === 'number' && Number.isFinite(value) ? `外观分数 ${value.toFixed(3)}（非正确率）` : '外观分数未记录（非正确率）'
  }
  return `可信度 ${confidenceText(evidence.confidence)}`
}

export function locationAnswer(itemName: string, evidence: EventRecord | null, mode: ResolvedRuntimeMode) {
  if (!evidence) return mode === 'REAL' ? '暂时没有真实摄像头产生的位置记录。' : mode === 'UNKNOWN' ? '运行模式未确认，无法展示位置记录。' : '当前隔离模式没有可验证的位置记录。'
  const place = locationPlace(evidence)
  if (isObservationEvidence(evidence)) {
    if (evidence.event_type === 'occluded') return `${itemName}最后在${place}被看到，随后被遮挡；当前具体位置未确认。`
    if (evidence.event_type === 'exited_view') return `${itemName}最后在${place}被看到，之后离开视野；当前去向未确认。`
    if (evidence.event_type === 'offline') return `${itemName}最后在${place}被看到，视频源现已中断；当前所在位置未知。`
    if (evidence.event_type === 'lost') return `${itemName}最后在${place}被看到，随后未再可靠识别；当前所在位置未知。`
    return `${itemName}最后在${place}被看到，尚未有新的确认放置证据。`
  }
  return evidence.manually_corrected || evidence.event_type === 'manual_correction' ? `人工纠正位置：${place}。` : `${itemName}最后一次确认放在${place}。`
}

export function visibleLocationHypotheses(result: SearchResult, evidence: EventRecord | null) {
  if (!evidence?.source_session_id || !isObservationEvidence(evidence)) return []
  return (result.location_hypotheses || []).filter((value) => value.evidence_type === 'inferred'
    && typeof value.label === 'string' && Boolean(value.label.trim())
    && typeof value.reason === 'string' && Boolean(value.reason.trim())
    && value.source_session_id === evidence.source_session_id)
}

export function isTrustedRealLocationEvidence(record?: Partial<ProvenanceRecord> | null) {
  if (!record || provenanceTrust(record) !== 'verified_real') return false
  const status = String(record.evidence_status || '').trim().toLowerCase()
  const finalStatus = String(record.final_status || '').trim().toLowerCase()
  if (!confirmedEvidenceStatuses.has(status) || finalStatus !== 'confirmed_placed') return false
  const category = sourceCategory(record)
  return category === 'real' || category === 'authorized_capture' || category === 'manual'
}

export function deviceHardwareCategory(device?: Partial<FirmwareDevice> | null): DeviceHardwareCategory {
  if (!device || typeof device.simulated !== 'boolean') return 'unknown'
  const hardwareType = String(device.hardware_type || '').trim().toLowerCase()
  const sourceType = String(device.source_type || '').trim().toLowerCase()
  const hasPhysicalMarker = hardwareType === 'physical' || sourceType === 'esp32_real'
  const hasVirtualMarker = hardwareType === 'virtual' || sourceType === 'virtual_esp32'
  if (hasPhysicalMarker && hasVirtualMarker) return 'unknown'
  if (device.simulated === true) return hasPhysicalMarker ? 'unknown' : 'virtual'
  return hasPhysicalMarker && !hasVirtualMarker ? 'physical' : 'unknown'
}

export function runtimeModeText(mode: ResolvedRuntimeMode) {
  if (mode === 'DEMO') return '演示模式，当前事件不来自真实家庭摄像头。'
  if (mode === 'TEST') return '测试模式，当前数据只供自动化验收，不进入真实数据库。'
  if (mode === 'REAL') return '真实模式'
  return '运行模式未确认：已隐藏演示入口，事件默认视为来源未验证。'
}

export function canonicalSourceType(value: SourceType | string): SourceType | string {
  const aliases: Record<string, SourceType> = { webcam: 'opencv_camera', browser: 'browser_camera', esp32: 'esp32_real', screen: 'authorized_screen_capture', video: 'video_file' }
  return aliases[value] || value
}

export function canonicalRuntimeMode(value: ResolvedRuntimeMode): RuntimeMode | null {
  return value === 'UNKNOWN' ? null : value
}
