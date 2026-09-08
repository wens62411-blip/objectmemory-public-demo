import { describe, expect, it } from 'vitest'
import { deviceHardwareCategory, evidenceBadge, isDisplayableRealObservation, isRealCameraEvidence, isTrustedRealLocationEvidence, provenanceTrust, selectLocationEvidence, sourceBadge, sourceCategory } from '../src/lib/provenance'
import type { EventRecord, SearchResult } from '../src/types'

const event = (overrides: Partial<EventRecord> = {}): EventRecord => ({
  id: 'event-one', item_id: 'item-one', item_name: '钥匙', event_type: 'placed', camera_id: 'camera-one', camera_name: '任意名称', room_name: '客厅', timestamp_start: '2026-09-04T10:00:00Z', confidence: .91, detection_mode: 'aruco',
  runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, evidence_status: 'confirmed', final_status: 'confirmed_placed', ...overrides,
})

describe('来源和证据标签', () => {
  it('只使用明确来源字段，不从摄像头名称猜测', () => {
    const misleadingCameraName = { ...event(), source_type: undefined, camera_name: '测试视频演示' }
    expect(sourceCategory(misleadingCameraName)).toBe('unknown')
    expect(sourceBadge({ ...event(), source_type: undefined })).toEqual({ label: '来源未验证', tone: 'danger' })
  })

  it('明确区分真实摄像头、测试视频、虚拟设备和演示种子', () => {
    expect(sourceBadge(event()).label).toBe('本机 OpenCV 来源')
    expect(sourceBadge(event({ source_type: 'rtsp' }))).toEqual({ label: '网络视频流（物理来源未验证）', tone: 'warning' })
    expect(sourceBadge(event({ source_type: 'onvif' }))).toEqual({ label: '网络视频流（物理来源未验证）', tone: 'warning' })
    expect(sourceBadge(event({ source_type: 'mjpeg' }))).toEqual({ label: '网络视频流（物理来源未验证）', tone: 'warning' })
    expect(sourceBadge(event({ source_type: 'browser_camera' }))).toEqual({ label: '浏览器帧（来源未验证）', tone: 'warning' })
    expect(sourceBadge(event({ source_type: 'video_file', runtime_mode: 'DEMO', is_simulated: true })).label).toBe('测试视频')
    expect(sourceBadge(event({ source_type: 'virtual_esp32', runtime_mode: 'DEMO', is_simulated: true })).label).toBe('虚拟设备')
    expect(sourceBadge(event({ source_type: 'demo_seed', runtime_mode: 'DEMO', is_simulated: true })).label).toBe('演示种子')
    expect(sourceBadge(event({ source_type: 'authorized_screen_capture' }))).toEqual({ label: '授权窗口采集', tone: 'blue' })
    expect(sourceBadge(event({ source_type: 'esp32_unverified' }))).toEqual({ label: '来源未验证', tone: 'danger' })
  })

  it('只有 REAL 模式的非模拟摄像头证据可作为真实位置', () => {
    expect(isRealCameraEvidence(event())).toBe(true)
    expect(isRealCameraEvidence(event({ runtime_mode: 'DEMO' }))).toBe(false)
    expect(isRealCameraEvidence(event({ is_simulated: true }))).toBe(false)
    expect(isRealCameraEvidence(event({ source_type: 'video_file' }))).toBe(false)
    expect(isRealCameraEvidence(event({ source_type: 'browser_camera' }))).toBe(false)
    expect(provenanceTrust(event({ source_type: 'browser_camera' }))).toBe('unknown')
    expect(isDisplayableRealObservation(event({ source_type: 'browser_camera', event_type: 'last_seen', evidence_status: 'observation_only' }))).toBe(true)
    expect(isDisplayableRealObservation(event({ source_type: 'browser_camera', event_type: 'placed', evidence_status: 'confirmed' }))).toBe(false)
    expect(isRealCameraEvidence(event({ source_type: 'authorized_screen_capture' }))).toBe(false)
    expect(isTrustedRealLocationEvidence(event({ source_type: 'authorized_screen_capture' }))).toBe(true)
    expect(isRealCameraEvidence(event({ source_type: 'rtsp' }))).toBe(false)
    expect(isTrustedRealLocationEvidence(event({ source_type: 'rtsp' }))).toBe(false)
    expect(isTrustedRealLocationEvidence(event({ evidence_status: 'candidate' }))).toBe(false)
    expect(isTrustedRealLocationEvidence(event({ evidence_status: 'no_evidence' }))).toBe(false)
    expect(isTrustedRealLocationEvidence(event({ final_status: 'rejected' }))).toBe(false)
    expect(isTrustedRealLocationEvidence(event({ final_status: null }))).toBe(false)
    expect(isTrustedRealLocationEvidence(event({ event_type: 'manual_correction', manually_corrected: true }))).toBe(true)
    expect(isTrustedRealLocationEvidence(event({ event_type: 'manual_correction', manually_corrected: true, runtime_mode: 'DEMO', is_simulated: true }))).toBe(false)
  })

  it('任何来源字段缺失或互相矛盾时都失败关闭', () => {
    expect(provenanceTrust({ ...event(), runtime_mode: undefined })).toBe('unknown')
    expect(provenanceTrust({ ...event(), source_type: undefined })).toBe('unknown')
    expect(provenanceTrust({ ...event(), is_simulated: undefined })).toBe('unknown')
    expect(provenanceTrust(event({ runtime_mode: 'DEMO', is_simulated: false }))).toBe('unknown')
    expect(provenanceTrust(event({ source_type: 'video_file', runtime_mode: 'REAL', is_simulated: false }))).toBe('unknown')
    expect(evidenceBadge({ ...event(), is_simulated: undefined })).toEqual({ label: '来源未验证', tone: 'danger' })
    expect(evidenceBadge(event({ runtime_mode: 'DEMO', is_simulated: false }))).toEqual({ label: '来源未验证', tone: 'danger' })
  })

  it('DEMO 或 TEST 的明确模拟来源不会显示真实确认', () => {
    expect(provenanceTrust(event({ runtime_mode: 'DEMO', is_simulated: true }))).toBe('verified_simulated')
    expect(evidenceBadge(event({ runtime_mode: 'DEMO', is_simulated: true })).label).toBe('模拟确认')
    expect(evidenceBadge(event({ runtime_mode: 'TEST', source_type: 'test_fixture', is_simulated: true })).label).toBe('模拟确认')
  })

  it('严格区分确认、未确认、最后看到、遮挡和人工纠正', () => {
    expect(evidenceBadge(event()).label).toBe('真实确认')
    expect(evidenceBadge(event({ final_status: null }))).toEqual({ label: '证据未验证', tone: 'danger' })
    expect(evidenceBadge(event({ final_status: 'rejected' }))).toEqual({ label: '无有效证据', tone: 'danger' })
    expect(evidenceBadge(event({ evidence_status: 'candidate' })).label).toBe('真实但未确认')
    expect(evidenceBadge(event({ event_type: 'seen', evidence_status: 'last_seen' })).label).toBe('最后看到')
    expect(evidenceBadge(event({ event_type: 'last_seen', evidence_status: 'observation_only' })).label).toBe('最后看到')
    expect(evidenceBadge(event({ source_type: 'browser_camera', event_type: 'last_seen', evidence_status: 'observation_only' })).label).toBe('最后看到')
    expect(evidenceBadge(event({ source_type: 'browser_camera', evidence_status: 'confirmed' })).label).toBe('来源未验证')
    expect(evidenceBadge(event({ event_type: 'occluded' })).label).toBe('被遮挡')
    expect(evidenceBadge(event({ manually_corrected: true })).label).toBe('人工纠正')
    expect(evidenceBadge(event({ manually_corrected: true, final_status: null }))).toEqual({ label: '证据未验证', tone: 'danger' })
  })

  it('设备只有明确 simulated=false 和物理标识时才算物理设备', () => {
    expect(deviceHardwareCategory({ device_id: 'real-one', simulated: false, hardware_type: 'physical' })).toBe('physical')
    expect(deviceHardwareCategory({ device_id: 'real-two', simulated: false, source_type: 'esp32_real' })).toBe('physical')
    expect(deviceHardwareCategory({ device_id: 'virtual', simulated: true })).toBe('virtual')
    expect(deviceHardwareCategory({ device_id: 'missing', simulated: false })).toBe('unknown')
    expect(deviceHardwareCategory({ device_id: 'conflict', simulated: true, source_type: 'esp32_real' })).toBe('unknown')
    expect(deviceHardwareCategory({ device_id: 'unverified', simulated: false, hardware_type: 'unverified', source_type: 'esp32_unverified' })).toBe('unknown')
    expect(deviceHardwareCategory({ device_id: 'legacy' })).toBe('unknown')
  })

  it('lost/offline是保留的真实观察状态，不能当成新的确认放置', () => {
    for (const event_type of ['lost', 'offline', 'occluded', 'exited_view'] as const) {
      const value = event({ event_type, evidence_status: 'observation_only', final_status: null })
      expect(isDisplayableRealObservation(value)).toBe(true)
      expect(isTrustedRealLocationEvidence(value)).toBe(false)
    }
    expect(isDisplayableRealObservation(event({ event_type: 'last_seen', evidence_status: 'candidate' }))).toBe(false)
    expect(isDisplayableRealObservation(event({ event_type: 'last_seen', evidence_status: 'observation_only', identity_evidence: { accepted: false } }))).toBe(false)
  })

  it('观察与确认按实际时刻比较，等时刻丢失优先；跨模式、错物品、无效时间拒绝', () => {
    const confirmed = event({ timestamp_start: '2026-09-05T12:00:00Z' })
    const base: SearchResult = { item: { id: 'item-one', name: '钥匙', type: '钥匙', aliases: [], aruco_id: null, ring_enabled: false }, status: 'placed', answer: '', last_confirmed: confirmed, last_seen: null, last_picked_up: null, last_occluded: null, last_exited: null, evidence: confirmed }
    const tied = event({ id: null, event_type: 'lost', evidence_status: 'observation_only', final_status: null, timestamp_start: '2026-09-05T20:00:00+08:00' })
    expect(selectLocationEvidence({ ...base, last_observed: tied }, 'REAL')).toBe(tied)
    const older = { ...tied, timestamp_start: '2026-09-05T19:59:59+08:00' }
    expect(selectLocationEvidence({ ...base, last_observed: older }, 'REAL')).toBe(confirmed)
    for (const invalid of [
      { ...tied, item_id: 'other-item' },
      { ...tied, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true },
      { ...tied, evidence_status: 'candidate' },
      { ...tied, timestamp_start: 'not a timestamp' },
    ]) expect(selectLocationEvidence({ ...base, last_observed: invalid }, 'REAL')).toBe(confirmed)
    expect(selectLocationEvidence(base, 'UNKNOWN')).toBeNull()
  })
})
