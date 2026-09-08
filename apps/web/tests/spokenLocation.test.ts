import { describe, expect, it } from 'vitest'
import { imageDirection, spokenLocation } from '../src/lib/spokenLocation'
import type { EventRecord, SearchResult } from '../src/types'

const observation: EventRecord = { id: null, item_id: 'p', item_name: '主力手机', event_type: 'last_seen', camera_id: 'c', camera_name: '书房相机', room_name: '书房', zone_name: '工作桌', timestamp_start: '2026-09-07T04:05:06Z', confidence: .9, detection_mode: 'experimental', runtime_mode: 'REAL', source_type: 'opencv_camera', source_session_id: 'live-session', source_frame: 50, is_simulated: false, evidence_status: 'observation_only', final_position: [.8, .8] }
const result = (evidence: EventRecord | null): SearchResult => ({ item: { id: 'p', name: '主力手机', type: '手机', aliases: [], aruco_id: null, ring_enabled: false }, status: 'unknown', answer: '硬编码的沙发答案不能朗读', evidence, last_confirmed: null, last_seen: null, last_picked_up: null, last_occluded: null, last_exited: null })

describe('语音答案来源契约，不证明麦克风和视觉识别', () => {
  it('读取最新真实观察和画面方位，但不朗读原始answer或猜测物理方向', () => {
    const answer = spokenLocation([result(observation)], 'REAL')
    expect(answer).toContain('最后在书房 · 工作桌被看到')
    expect(answer).toContain('摄像头画面右下方')
    expect(answer).toContain('不是你所在位置的左右')
    expect(answer).toContain('记录时间是')
    expect(answer).not.toContain('沙发')
    expect(answer).not.toContain('确认放在')
  })
  it('无证据、未知模式或错误物品ID均不编造位置', () => {
    expect(spokenLocation([], 'REAL')).toContain('不能猜测')
    expect(spokenLocation([result(observation)], 'UNKNOWN')).toContain('不能可靠回答')
    expect(spokenLocation([result({ ...observation, item_id: 'other' })], 'REAL')).not.toContain('工作桌')
    expect(spokenLocation([result(null)], 'REAL')).toContain('当前位置未确认')
  })
  it.each([{ source_session_id: null }, { source_frame: undefined }, { source_frame: -1 }])('旧记录缺少有效来源%j不得升级成语音确认位置', patch => {
    const answer = spokenLocation([result({ ...observation, ...patch })], 'REAL')
    expect(answer).toContain('缺少完整来源帧信息')
    expect(answer).not.toContain('工作桌'); expect(answer).not.toContain('右下')
  })
  it('REAL拒绝模拟记录；DEMO明确口头提示模拟', () => {
    const simulated = result({ ...observation, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true })
    expect(spokenLocation([simulated], 'REAL')).not.toContain('工作桌')
    expect(spokenLocation([simulated], 'DEMO')).toMatch(/^这是演示数据/)
  })
  it('被遮挡/离线优先于过去放下，携带不能说已放下', () => {
    const confirmed = { ...observation, id: 'placed', event_type: 'placed' as const, evidence_status: 'confirmed', final_status: 'confirmed_placed', timestamp_start: '2026-09-07T04:00:00Z', zone_name: '旧桌' }
    const lost = { ...result(confirmed), last_observed: { ...observation, event_type: 'occluded' as const }, last_confirmed: confirmed }
    expect(spokenLocation([lost], 'REAL')).toContain('随后被遮挡')
    expect(spokenLocation([lost], 'REAL')).not.toContain('旧桌')
    expect(spokenLocation([result({ ...observation, holding_status: 'co_moving' })], 'REAL')).toContain('可能正在携带，尚未确认放下')
    expect(spokenLocation([result({ ...observation, holding_status: 'release_candidate' })], 'REAL')).toContain('正在等待放下后稳定')
  })
  it('人工纠正明确说明，推测必须同来源并有依据', () => {
    expect(spokenLocation([result({ ...observation, event_type: 'manual_correction', manually_corrected: true, evidence_status: 'confirmed', final_status: 'confirmed_placed' })], 'REAL')).toContain('人工纠正')
    const proposed = { ...result(observation), location_hypotheses: [{ label: '桌边', reason: '最后轨迹靠近边缘', evidence_type: 'inferred' as const, source_session_id: 'live-session' }, { label: '另一个房间', reason: '来源不符', evidence_type: 'inferred' as const, source_session_id: 'wrong' }] }
    expect(spokenLocation([proposed], 'REAL')).toContain('未确认的推测是桌边')
    expect(spokenLocation([proposed], 'REAL')).not.toContain('另一个房间')
  })
  it.each([null, [200, 300], [NaN, .5], [.2, .3, .4], { x: .2, y: .3 }])('不从未标定或无效坐标%j猜方位', position => expect(imageDirection(position)).toBeNull())
  it('多件同名不擅自选中第一件，朗读数量有界', () => {
    expect(spokenLocation([result(observation), result(observation)], 'REAL')).toContain('找到2件匹配物品')
    expect(spokenLocation(Array.from({ length: 5 }, () => result(observation)), 'REAL')).toContain('先说明前三件')
  })
})
