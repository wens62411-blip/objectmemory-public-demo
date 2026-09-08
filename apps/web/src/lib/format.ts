import type { EventRecord } from '../types'

export const eventLabels: Record<string, string> = {
  seen: '再次看到',
  picked_up: '被拿起',
  moved: '移动中',
  movement: '位置变化',
  placed: '确认放下',
  occluded: '被遮挡',
  exited_view: '离开画面',
  reappeared: '重新出现',
  manual_correction: '人工纠正',
}

export const sourceLabels: Record<string, string> = {
  opencv_camera: '电脑 / USB 摄像头',
  webcam: '电脑 / USB 摄像头',
  browser_camera: '浏览器上传帧（来源未验证）',
  browser: '浏览器上传帧（来源未验证）',
  esp32_real: '真实 ESP32-CAM',
  esp32: 'ESP32-CAM',
  rtsp: 'RTSP 网络视频流',
  onvif: 'ONVIF 网络视频流',
  mjpeg: 'HTTP / MJPEG 网络视频流',
  authorized_screen_capture: '授权窗口区域',
  screen: '授权窗口区域',
  video_file: '测试视频文件',
  video: '视频文件回放',
  virtual_esp32: '虚拟 ESP32-CAM',
  demo_seed: '演示种子',
  test_fixture: '测试夹具',
}

export function eventLabel(event: EventRecord) {
  const status = String(event.evidence_status || '').toLowerCase()
  const finalStatus = String(event.final_status || '').toLowerCase()
  const confirmed = ['confirmed', 'verified', 'real_verified', 'confirmed_placed', 'accepted'].includes(status) && finalStatus === 'confirmed_placed'
  if (finalStatus === 'position_changed') return '位置变化，未确认放下'
  if (event.event_type === 'placed' && !confirmed) return '放置候选（未确认）'
  return eventLabels[event.event_type] || event.event_type
}

export function formatTime(value?: string | null) {
  if (!value) return '尚无记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date)
}

export function relativeTime(value?: string | null) {
  if (!value) return '尚无记录'
  const diff = Date.now() - new Date(value).getTime()
  if (!Number.isFinite(diff)) return value
  const seconds = Math.max(0, Math.round(diff / 1000))
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.round(hours / 24)} 天前`
}

export function confidenceText(value?: number) {
  if (value === undefined || value === null) return '未知'
  const percent = value <= 1 ? Math.round(value * 100) : Math.round(value)
  if (percent >= 85) return `${percent}% · 高`
  if (percent >= 65) return `${percent}% · 中`
  return `${percent}% · 需确认`
}

export function evidenceLabel(event?: EventRecord | null) {
  if (!event) return '暂无证据'
  if (event.source_type === 'video_file' || event.source_type === 'video') return '测试视频'
  if (event.source_type === 'virtual_esp32') return '虚拟设备'
  if (event.source_type === 'demo_seed') return '演示种子'
  if (event.source_type === 'test_fixture') return '测试数据'
  if (event.detection_mode === 'aruco') return '稳定标签模式'
  if (event.detection_mode === 'experimental') return '无标签 AI 实验模式'
  return '检测方式未验证'
}

export function statusTone(status?: string) {
  const value = (status || '').toLowerCase()
  if (['ready', 'online', 'healthy', 'running', 'success', 'ok'].some((item) => value.includes(item))) return 'success'
  if (['error', 'failed', 'offline', 'lost'].some((item) => value.includes(item))) return 'danger'
  if (['connecting', 'queued', 'building', 'flashing'].some((item) => value.includes(item))) return 'warning'
  return 'neutral'
}
