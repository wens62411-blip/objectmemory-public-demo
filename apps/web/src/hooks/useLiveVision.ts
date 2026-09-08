import { useEffect, useRef, useState } from 'react'
import { wsUrl } from '../lib/api'
import type { EventRecord, RecognitionTest } from '../types'

export type LiveVisionFrame = Pick<EventRecord, 'runtime_mode' | 'source_type' | 'is_simulated'> & {
  type: 'frame'; camera_id: string; source_session_id: string; source_frame: number
  source_timestamp: string; width: number; height: number; image_data_url: string
  coordinate_space: 'source_normalized'; candidates: RecognitionTest['candidates']; hands: { landmarks: number[][]; handedness?: string | null }[]
  display_only: true; inference_fps?: number
}

export function parseVisionPacket(packet: ArrayBuffer): { metadata: Omit<LiveVisionFrame, 'image_data_url'>; jpeg: Blob } {
  if (packet.byteLength < 8 || packet.byteLength > 8 * 1024 * 1024 + 131076) throw new Error('跟踪帧大小无效')
  const size = new DataView(packet).getUint32(0, false)
  if (size < 2 || size > 131072 || size + 7 > packet.byteLength) throw new Error('跟踪帧头损坏')
  const metadata = JSON.parse(new TextDecoder().decode(new Uint8Array(packet, 4, size)))
  const jpeg = new Uint8Array(packet, 4 + size)
  if (metadata.type !== 'frame' || typeof metadata.source_session_id !== 'string' || !metadata.source_session_id.trim()
      || typeof metadata.camera_id !== 'string' || !metadata.camera_id.trim()
      || typeof metadata.source_timestamp !== 'string' || !Number.isFinite(Date.parse(metadata.source_timestamp))
      || !Number.isSafeInteger(metadata.source_frame)
      || metadata.source_frame < 0 || !Number.isInteger(metadata.width) || !Number.isInteger(metadata.height)
      || metadata.width < 1 || metadata.height < 1 || metadata.width * metadata.height > 16777216
      || metadata.coordinate_space !== 'source_normalized' || metadata.display_only !== true
      || !['REAL', 'DEMO', 'TEST'].includes(metadata.runtime_mode) || typeof metadata.is_simulated !== 'boolean' || !metadata.source_type
      || metadata.runtime_mode === 'REAL' && (metadata.is_simulated || !['opencv_camera','browser_camera','rtsp','onvif','mjpeg','esp32_real','authorized_screen_capture'].includes(metadata.source_type))
      || metadata.runtime_mode !== 'REAL' && metadata.is_simulated !== true
      || !Array.isArray(metadata.candidates) || !Array.isArray(metadata.hands)
      || jpeg[0] !== 255 || jpeg[1] !== 216 || jpeg[2] !== 255) throw new Error('跟踪帧来源或图像格式不完整')
  return { metadata, jpeg: new Blob([jpeg], { type: 'image/jpeg' }) }
}

/** ACK after actual image decoding: one in-flight frame, no growing JS queue. */
export function useLiveVision(cameraId: string, active: boolean) {
  const [state, setState] = useState<{ connected: boolean; frame: LiveVisionFrame | null; error: string; fps: number }>({ connected: false, frame: null, error: '', fps: 0 })
  const acknowledge = useRef<(frame: number, session: string) => void>(() => {})
  useEffect(() => {
    let disposed = false, socket: WebSocket | null = null, retry: ReturnType<typeof setTimeout> | undefined
    let expiry: ReturnType<typeof setTimeout> | undefined, currentUrl = '', previousUrl = '', pending = ''
    let lastSession = '', lastSequence = -1, retryMs = 500, delivered: number[] = []
    function clearImages() {
      clearTimeout(expiry)
      if (currentUrl) URL.revokeObjectURL(currentUrl)
      if (previousUrl) URL.revokeObjectURL(previousUrl)
      currentUrl = previousUrl = pending = ''
      delivered = []
    }
    function stop() {
      clearTimeout(retry); clearImages()
      const old = socket; socket = null; old?.close()
    }
    function connect() {
      if (disposed || !active || document.visibilityState === 'hidden' || socket) return
      try { socket = new WebSocket(wsUrl(`/ws/cameras/${encodeURIComponent(cameraId)}/vision`)) } catch {
        setState({ connected: false, frame: null, error: '连续跟踪连接创建失败，稍后重试；当前仅为低频诊断。', fps: 0 })
        retry = setTimeout(connect, retryMs); retryMs = Math.min(5000, retryMs * 2)
        return
      }
      const own = socket
      own.binaryType = 'arraybuffer'
      own.onopen = () => { if (socket === own && !disposed) setState({ connected: true, frame: null, error: '', fps: 0 }) }
      own.onmessage = (event) => {
        if (socket !== own || disposed) return
        try {
          if (typeof event.data === 'string') {
            clearImages(); setState({ connected: true, frame: null, error: '没有新鲜跟踪帧，旧框已撤下。', fps: 0 }); return
          }
          if (pending) throw new Error('服务端发送了未确认的多余帧')
          const { metadata, jpeg } = parseVisionPacket(event.data)
          if (metadata.camera_id !== cameraId || metadata.source_session_id === lastSession && metadata.source_frame <= lastSequence) throw new Error('跟踪帧来源错配或倒退')
          const newSession = metadata.source_session_id !== lastSession
          if (newSession) delivered = []
          lastSession = metadata.source_session_id; lastSequence = metadata.source_frame
          pending = `${lastSession}:${lastSequence}`
          previousUrl = currentUrl; currentUrl = URL.createObjectURL(jpeg)
          const frame = { ...metadata, image_data_url: currentUrl }
          setState(old => ({ connected: true, frame, error: '', fps: newSession ? 0 : old.fps }))
          clearTimeout(expiry)
          expiry = setTimeout(() => {
            if (socket !== own || disposed) return
            clearImages(); setState({ connected: true, frame: null, error: '跟踪画面已过期，旧框已撤下。', fps: 0 }); own.close()
          }, 1200)
          retryMs = 500
        } catch (error) {
          clearImages(); setState({ connected: false, frame: null, error: error instanceof Error ? error.message : '跟踪帧读取失败', fps: 0 }); own.close()
        }
      }
      own.onclose = () => {
        if (socket !== own || disposed) return
        socket = null; clearImages(); lastSession = ''; lastSequence = -1; delivered = []
        setState({ connected: false, frame: null, error: '连续跟踪连接中断；下方仅为低频诊断帧。', fps: 0 })
        retry = setTimeout(connect, retryMs); retryMs = Math.min(5000, retryMs * 2)
      }
      own.onerror = () => own.close()
    }
    acknowledge.current = (frame, session) => {
      if (!socket || socket.readyState !== WebSocket.OPEN || pending !== `${session}:${frame}`) return
      pending = ''
      if (previousUrl) { URL.revokeObjectURL(previousUrl); previousUrl = '' }
      const t = performance.now(); delivered = [...delivered.filter(value => t-value < 2000), t]
      // Decode acknowledgements measure updates, not physical screen refresh.
      // Do not extrapolate two startup frames into a misleading burst rate.
      const fps = delivered.length > 1 && t-delivered[0] >= 1000 ? (delivered.length-1)*1000/(t-delivered[0]) : 0
      setState(old => ({ ...old, fps }))
      socket.send(JSON.stringify({ ack: frame, source_session_id: session }))
    }
    function visibility() {
      if (document.visibilityState === 'hidden') { stop(); setState({ connected: false, frame: null, error: '', fps: 0 }) }
      else connect()
    }
    setState({ connected: false, frame: null, error: '', fps: 0 })
    // StrictMode validates effects with setup/cleanup/setup in one turn. Do
    // not create a throwaway CONNECTING socket in the discarded first setup.
    // connect() checks this effect's disposed flag; real pending handshakes
    // still close immediately in stop(), so navigation never strands them.
    void Promise.resolve().then(connect)
    document.addEventListener('visibilitychange', visibility)
    return () => { disposed = true; stop(); acknowledge.current = () => {}; document.removeEventListener('visibilitychange', visibility) }
  }, [cameraId, active])
  return { ...state, acknowledge: (frame: number, session: string) => acknowledge.current(frame, session) }
}
