import { BellRing, Clock3, Film, History, MapPin, PencilLine, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { EventRecord, ResolvedRuntimeMode, SearchResult } from '../types'
import { evidenceLabel, formatTime } from '../lib/format'
import { mediaUrl } from '../lib/api'
import { Badge } from './UI'
import { ModeNotice } from './ModeNotice'
import { EventProvenance } from './ProvenanceBadge'
import { confirmedLocationEvidence, evidenceBadge, isLocationUnavailable, isObservationEvidence, locationAnswer, locationConfidenceLabel, locationEvidenceTime, locationPlace, selectLocationEvidence, visibleLocationHypotheses } from '../lib/provenance'

export function SearchResultCard({ result, runtimeMode = 'UNKNOWN', onRing, onCorrect }: { result: SearchResult; runtimeMode?: ResolvedRuntimeMode; onRing?: () => void; onCorrect?: (event: EventRecord) => void }) {
  const evidence = selectLocationEvidence(result, runtimeMode)
  const confirmed = confirmedLocationEvidence(result, runtimeMode)
  const observation = isObservationEvidence(evidence)
  const evidenceId = evidence?.event_id || evidence?.id
  const showPreviousConfirmation = Boolean(confirmed && (observation || (confirmed.event_id || confirmed.id) !== evidenceId))
  const location = evidence ? locationPlace(evidence) : runtimeMode === 'REAL' ? '位置尚未由真实摄像头确认' : runtimeMode === 'UNKNOWN' ? '运行模式未确认，位置证据已隐藏' : '位置还没有可验证记录'
  const answer = locationAnswer(result.item.name, evidence, runtimeMode)
  const hypotheses = visibleLocationHypotheses(result, evidence)
  const observationHint = typeof result.observation_hint?.message === 'string' ? result.observation_hint.message.trim() : ''
  const frame = evidence?.source_frame ?? evidence?.source_frame_end
  const screenshotFrame = evidence?.screenshot_source_frame
  const screenshotTime = evidence?.screenshot_observed_at
  const sourceMismatch = Boolean(observation && evidence?.screenshot_source_session_id && evidence.source_session_id && evidence.screenshot_source_session_id !== evidence.source_session_id)
  const screenshotKey = `${evidence?.screenshot_path || ''}:${evidence?.screenshot_sha256 || ''}`
  const [failedImage, setFailedImage] = useState<string | null>(null)
  const hasImage = Boolean(evidence?.screenshot_path && !sourceMismatch && failedImage !== screenshotKey)
  const oldImage = Boolean(observation && (screenshotTime && Date.parse(screenshotTime) < locationEvidenceTime(evidence)
    || typeof screenshotFrame === 'number' && typeof frame === 'number' && screenshotFrame < frame))
  return (
    <article className="result-card">
      <div className="result-copy">
        <div className="result-heading">
          <div className="item-token">{result.item.type?.includes('phone') || result.item.name.includes('手机') ? '手机' : result.item.name.slice(0, 2)}</div>
          <div><p className="eyebrow">{result.item.name}</p><h2>{answer}</h2></div>
        </div>
        {observationHint && <section className="evidence-warning" aria-label="观察提示"><strong>观察提示（不是位置记录）</strong><p>{observationHint}</p></section>}
        <div className="location-line" aria-label={isLocationUnavailable(evidence) ? '最后可见区域，当前位置未知' : observation ? '最后看到的区域' : '确认位置'}><MapPin /><strong>{location}</strong></div>
        {isLocationUnavailable(evidence) && <p className="evidence-warning">这是最后可见区域，不表示物品现在仍在那里；当前位置未知。</p>}
        <div className="result-meta">
          <span><Clock3 />{observation ? '最后看到：' : '记录时间：'}{formatTime(evidence?.timestamp_end || evidence?.timestamp_start)}</span>
          <span><ShieldCheck />{locationConfidenceLabel(evidence)}</span>
          {evidence && <Badge tone={evidenceBadge(evidence).tone}>{evidenceBadge(evidence).label}</Badge>}
        </div>
        {evidence?.event_type === 'occluded' && <p className="evidence-warning">物品随后被遮挡，当前具体位置尚未完全确认。</p>}
        {evidence && <><EventProvenance event={evidence} /><ModeNotice mode={evidence.detection_mode} /></>}
        {observation && !evidence?.clip_path && <p className="photo-help">没有拿放录像不影响这条可靠观察；“最后看到”并不是“确认放下”。</p>}
        {showPreviousConfirmation && confirmed && <section className="evidence-warning" aria-label="历史确认放置"><strong>上一次确认放置（历史）</strong><p>{locationPlace(confirmed)}</p><p>{formatTime(confirmed.timestamp_end || confirmed.timestamp_start)} · {locationConfidenceLabel(confirmed)}</p><EventProvenance event={confirmed} compact /><p>这条历史不能替代上方更新的观察或未知状态。</p></section>}
        {hypotheses.length > 0 && <section className="evidence-warning" aria-label="有依据的候选位置"><strong>可能位置 · 推测，尚未确认</strong>{hypotheses.map((hypothesis, index) => <div key={`${hypothesis.source_session_id}:${index}`}><p>{hypothesis.label}</p><p>依据：{hypothesis.reason}</p>{hypothesis.observed_at && <p>依据观察时间：{formatTime(hypothesis.observed_at)}</p>}</div>)}</section>}
        <div className="result-actions">
          {result.item.ring_enabled && onRing && <button className="button accent" onClick={onRing}><BellRing />让手机响铃</button>}
          {evidence?.clip_path && <a className="button secondary" href={mediaUrl(evidence.clip_path)} target="_blank" rel="noreferrer"><Film />播放事件录像</a>}
          <Link className="button ghost" to={`/events?item_id=${result.item.id}`}><History />查看历史</Link>
          {evidence && evidenceId && !observation && onCorrect && <button className="button ghost" onClick={() => onCorrect(evidence)}><PencilLine />纠正位置</button>}
        </div>
      </div>
      <div>
        <div className="evidence-frame">
          {hasImage ? <img style={{ height: 'auto', objectFit: 'contain', display: 'block' }} src={mediaUrl(evidence?.screenshot_path)} alt={`${result.item.name}${observation ? '最后可见截图' : '事件截图'}`} onError={() => setFailedImage(screenshotKey)} /> : <div className="no-evidence"><MapPin /><span>{sourceMismatch ? '截图来源会话不一致，已隐藏' : failedImage === screenshotKey ? '截图无法读取' : '还没有关键截图'}</span></div>}
          <div className="evidence-caption"><span>{evidence?.camera_name || (evidence ? '已记录视频来源' : '尚无可验证摄像头证据')}</span><strong>{evidenceLabel(evidence)}</strong></div>
        </div>
        {observation && <div className="photo-help" aria-label="截图与观察时间说明">
          <p>最后观察帧：{frame ?? '未记录'} · 最后看到时间：{formatTime(evidence?.timestamp_end || evidence?.timestamp_start)}</p>
          {hasImage && <p>截图拍摄时间：{screenshotTime ? formatTime(screenshotTime) : '未单独记录，不能视为最新帧'}<br />截图来源帧：{screenshotFrame ?? '未单独记录'}</p>}
          {oldImage && hasImage && <p>保留的是较早的同来源截图，不是最后一次观察的同一帧；最后看到时间以左侧记录为准。</p>}
          {!hasImage && <p>图像证据当前不完整，可靠观察与最后看到时间仍保留。</p>}
          {evidence?.image_error && <p>截图状态：{evidence.image_error}</p>}
        </div>}
      </div>
    </article>
  )
}
