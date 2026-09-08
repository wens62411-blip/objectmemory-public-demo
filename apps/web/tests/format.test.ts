import { describe, expect, it } from 'vitest'
import { confidenceText, evidenceLabel, eventLabel, eventLabels, statusTone } from '../src/lib/format'
import { mediaUrl, queryString } from '../src/lib/api'
import type { EventRecord } from '../src/types'

const event: EventRecord = {
  id: 'e1', item_id: 'i1', item_name: '我的手机', event_type: 'placed', camera_id: 'c1', camera_name: '客厅摄像头', room_name: '客厅', zone_name: '沙发右侧', timestamp_start: '2026-09-03T08:32:00Z', confidence: .94, detection_mode: 'aruco',
  runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, evidence_status: 'confirmed', final_status: 'confirmed_placed',
}

describe('用户可见格式', () => {
  it('把置信度转换为直白等级', () => {
    expect(confidenceText(.91)).toBe('91% · 高')
    expect(confidenceText(.72)).toBe('72% · 中')
    expect(confidenceText(.42)).toBe('42% · 需确认')
  })
  it('严格区分标签、回放和实验模式', () => {
    expect(evidenceLabel(event)).toBe('稳定标签模式')
    expect(evidenceLabel({ ...event, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true })).toBe('测试视频')
    expect(evidenceLabel({ ...event, detection_mode: 'experimental' })).toBe('无标签 AI 实验模式')
    expect(eventLabels.occluded).toBe('被遮挡')
    expect(eventLabel({ ...event, evidence_status: null })).toBe('放置候选（未确认）')
    expect(eventLabel({ ...event, final_status: null })).toBe('放置候选（未确认）')
    expect(eventLabel({ ...event, final_status: 'rejected' })).toBe('放置候选（未确认）')
  })
  it('状态颜色不会把失败显示为正常', () => {
    expect(statusTone('ready')).toBe('success')
    expect(statusTone('offline')).toBe('danger')
    expect(statusTone('queued')).toBe('warning')
  })
  it('安全构造查询和本地媒体路径', () => {
    expect(queryString({ room_name: '客厅', item_id: '', limit: 10 })).toBe('?room_name=%E5%AE%A2%E5%8E%85&limit=10')
    expect(mediaUrl('media\\events\\a.jpg')).toBe('/media/events/a.jpg')
  })
})
