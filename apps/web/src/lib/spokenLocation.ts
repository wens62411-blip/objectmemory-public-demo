import type { EventRecord, ResolvedRuntimeMode, SearchResult } from '../types'
import { isLocationUnavailable, isObservationEvidence, locationAnswer, locationEvidenceTime, selectLocationEvidence, sourceBadge, visibleLocationHypotheses } from './provenance'

/** A spoken answer uses the same provenance gate as the visible evidence card.
 * Never read the server's free-form `answer` or a diagnostic hint as a location. */
export function spokenLocation(results: SearchResult[], mode: ResolvedRuntimeMode): string {
  if (mode === 'UNKNOWN') return '运行模式尚未确认，现在不能可靠回答物品位置。'
  const prefix = mode === 'DEMO' ? '这是演示数据，不是真实家庭摄像头记录。' : mode === 'TEST' ? '这是自动化测试数据，不是真实位置记录。' : ''
  if (!results.length) return `${prefix}没有找到匹配的物品。现在没有可验证的位置，不能猜测它放在哪里。`
  const answers = results.slice(0, 3).map(result => {
    const evidence = selectLocationEvidence(result, mode)
    const name = result.item.name.slice(0, 80)
    if (!evidence || !Number.isFinite(locationEvidenceTime(evidence))) return `${name}还没有可验证的位置记录，当前位置未确认。`
    const frame = evidence.source_frame ?? evidence.source_frame_end
    if (!evidence.source_session_id || !Number.isInteger(frame) || Number(frame) < 0) return `${name}的旧记录缺少完整来源帧信息，不能作为已验证的位置回答。`
    const parts = [locationAnswer(name, evidence, mode)]
    const holding = holdingDescription(evidence)
    if (holding && !isLocationUnavailable(evidence) && isObservationEvidence(evidence)) parts.push(holding)
    const direction = imageDirection(evidence.final_position)
    if (direction) parts.push(`在这条记录对应的摄像头画面${direction}。这是画面方位，不是你所在位置的左右，也不是房间距离。`)
    const when = new Date(locationEvidenceTime(evidence)).toLocaleString('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    parts.push(`记录时间是${when}，不保证它现在仍在那里。`)
    if (mode === 'REAL' && ['browser_camera', 'rtsp', 'onvif', 'mjpeg'].includes(evidence.source_type)) parts.push(`${sourceBadge(evidence).label}。`)
    const hypothesis = visibleLocationHypotheses(result, evidence)[0]
    if (hypothesis) parts.push(`另外一个未确认的推测是${hypothesis.label.slice(0, 120)}。依据是${hypothesis.reason.slice(0, 200)}。这不是确认放置。`)
    return parts.join('')
  })
  return `${prefix}${results.length > 1 ? `找到${results.length}件匹配物品，${results.length > 3 ? '先说明前三件，' : ''}请核对名称。` : ''}${answers.join('')}`
}

export function imageDirection(position: EventRecord['final_position']): string | null {
  // Only the API's normalized image-coordinate pair is accepted. World axes,
  // pixels, arbitrary dictionaries, and depth cannot be inferred from it.
  if (!Array.isArray(position) || position.length !== 2 || !position.every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1)) return null
  const [x, y] = position
  const horizontal = x < 1 / 3 ? '左' : x > 2 / 3 ? '右' : ''
  const vertical = y < 1 / 3 ? '上' : y > 2 / 3 ? '下' : ''
  return horizontal && vertical ? `${horizontal}${vertical}方` : horizontal ? `${horizontal}侧` : vertical ? `中间偏${vertical}` : '中央'
}

function holdingDescription(evidence: EventRecord) {
  switch (evidence.holding_status) {
    case 'possibly_held': return '手和物品可能存在持有关系，尚未确认拿起或放下。'
    case 'co_moving': return '观察到手和物品共同移动，可能正在携带，尚未确认放下。'
    case 'holding_uncertain': return '当前持有关系不确定，不能当成已放下。'
    case 'nearby': return '手在物品附近，但没有证明手拿着它。'
    case 'release_candidate': return '正在等待放下后稳定和手部离开的证据，尚未确认放下。'
    case 'released': return '已看到手和物品分开，但还没有完成放下确认。'
    default: return ''
  }
}
