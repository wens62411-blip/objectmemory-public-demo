import { AlertTriangle, Camera, CameraOff, CheckCircle2, ShieldCheck, Wifi, WifiOff } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Zone } from '../types'
import { wsUrl } from '../lib/api'
import { browserCameraError } from '../lib/cameraStatus'
import { Badge } from './UI'

export type BrowserPermission = 'unknown' | 'prompt' | 'granted' | 'denied' | 'unsupported' | 'insecure'

export interface BrowserCameraState {
  permission: BrowserPermission
  secureContext: boolean
  devices: MediaDeviceInfo[]
  selectedDeviceId: string
  width: number
  height: number
  fps: number
  previewing: boolean
  uploadState: 'idle' | 'connecting' | 'online' | 'disconnected' | 'error'
  uploadedFrames: number
  droppedFrames: number
  lastError: string
}

const initialState = (): BrowserCameraState => ({
  permission: !window.isSecureContext ? 'insecure' : navigator.mediaDevices ? 'unknown' : 'unsupported',
  secureContext: window.isSecureContext,
  devices: [], selectedDeviceId: '', width: 0, height: 0, fps: 0, previewing: false,
  uploadState: 'idle', uploadedFrames: 0, droppedFrames: 0, lastError: '',
})

export function BrowserCamera({ cameraId, targetFps = 4, targetWidth = 640, targetHeight = 480, zones = [], onState, showPreview = true }: {
  cameraId?: string
  targetFps?: number
  targetWidth?: number
  targetHeight?: number
  zones?: Zone[]
  onState?: (state: BrowserCameraState) => void
  showPreview?: boolean
}) {
  const [state, setState] = useState<BrowserCameraState>(initialState)
  const [starting, setStarting] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const uploadTimerRef = useRef<number | null>(null)
  const fpsFrameRef = useRef(0)
  const fpsStartedRef = useRef(0)
  const inFlightRef = useRef(false)
  const lastSendRef = useRef(0)
  const mountedRef = useRef(true)
  const previewingRef = useRef(false)
  const fpsCallbackRef = useRef<number | null>(null)
  const captureGeneration = useRef(0)
  const currentCameraId = useRef(cameraId); currentCameraId.current = cameraId
  const readyTimeoutRef = useRef<number | null>(null)

  useEffect(() => { onState?.(state) }, [onState, state])

  const closeSocket = useCallback(() => {
    if (readyTimeoutRef.current !== null) window.clearTimeout(readyTimeoutRef.current)
    readyTimeoutRef.current = null
    if (uploadTimerRef.current) window.clearInterval(uploadTimerRef.current)
    uploadTimerRef.current = null; inFlightRef.current = false
    const socket = socketRef.current; socketRef.current = null
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'browser camera stopped')
  }, [])

  const releaseCapture = useCallback((message = '', uploadState: BrowserCameraState['uploadState'] = 'idle') => {
    captureGeneration.current += 1
    closeSocket()
    streamRef.current?.getTracks().forEach((track) => { track.onended = null; track.stop() })
    streamRef.current = null
    previewingRef.current = false
    if (videoRef.current) { videoRef.current.pause(); videoRef.current.srcObject = null }
    if (mountedRef.current) {
      setStarting(false)
      setState((value) => ({ ...value, previewing: false, fps: 0, uploadState, lastError: message || (uploadState === 'idle' ? '' : value.lastError) }))
    }
  }, [closeSocket])

  const stopPreview = useCallback(() => releaseCapture(), [releaseCapture])

  const beginUpload = useCallback((id: string) => {
    closeSocket()
    if (!streamRef.current || !videoRef.current || !id) return
    setState((value) => ({ ...value, uploadState: 'connecting', uploadedFrames: 0, droppedFrames: 0 }))
    let socket: WebSocket
    try { socket = new WebSocket(wsUrl(`/ws/browser-cameras/${encodeURIComponent(id)}/ingest`)) }
    catch { setState(value => ({ ...value, uploadState: 'error', lastError: '上传连接无法创建，请检查当前主机连接后重新连接上传。' })); return }
    socketRef.current = socket
    let sentSequence = 0, expectedAck = 0, ready = false
    const failUpload = (message: string) => {
      if (socketRef.current !== socket || !mountedRef.current) return
      closeSocket()
      setState(value => ({ ...value, uploadState: 'error', lastError: message }))
    }
    readyTimeoutRef.current = window.setTimeout(() => failUpload('后端在 5 秒内没有确认采集通道，未发送视频。请重新连接上传。'), 5000)
    socket.onopen = () => {
      if (socketRef.current !== socket) return socket.close()
      setState((value) => ({ ...value, uploadState: 'connecting' }))
    }
    const startFramePump = () => {
      if (uploadTimerRef.current || socket.readyState !== WebSocket.OPEN) return
      const canvas = document.createElement('canvas'); canvas.width = targetWidth; canvas.height = targetHeight
      const context = canvas.getContext('2d', { alpha: false })
      uploadTimerRef.current = window.setInterval(() => {
        if (socketRef.current !== socket || document.visibilityState === 'hidden') return
        const video = videoRef.current
        const stream = streamRef.current
        const track = stream?.getVideoTracks()[0]
        if (!stream?.active || !track || track.readyState === 'ended') {
          releaseCapture('摄像头视频流已中断，已停止发送并释放设备。', 'disconnected')
          return
        }
        if (!context || !video || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || socket.readyState !== WebSocket.OPEN) return
        if (inFlightRef.current && Date.now() - lastSendRef.current > 3000) {
          failUpload('后端超过 3 秒未确认上一帧，已停止上传；不会把迟到确认算作新帧。请重新连接上传。')
          return
        }
        if (inFlightRef.current || socket.bufferedAmount > 256 * 1024) {
          setState((value) => ({ ...value, droppedFrames: value.droppedFrames + 1 }))
          return
        }
        inFlightRef.current = true
        lastSendRef.current = Date.now()
        // Preserve the actual source aspect ratio: no squeezed 16:9 -> 4:3
        // image paired with a different preview/zone geometry.
        const scale = Math.min(1, targetWidth / video.videoWidth, targetHeight / video.videoHeight)
        if (!Number.isFinite(scale) || scale <= 0) { inFlightRef.current = false; return }
        canvas.width = Math.max(1, Math.round(video.videoWidth * scale))
        canvas.height = Math.max(1, Math.round(video.videoHeight * scale))
        try {
          context.drawImage(video, 0, 0, canvas.width, canvas.height)
          canvas.toBlob((blob) => {
            // A delayed encode from a retired device/socket cannot affect its replacement.
            if (socketRef.current !== socket || streamRef.current !== stream || !mountedRef.current) return
            if (!blob || socket.readyState !== WebSocket.OPEN) {
              inFlightRef.current = false
              setState((value) => ({ ...value, droppedFrames: value.droppedFrames + 1 }))
              return
            }
            lastSendRef.current = Date.now()
            try { expectedAck = ++sentSequence; socket.send(blob) }
            catch { failUpload('视频发送失败，已停止上传。请重新连接上传。') }
          }, 'image/jpeg', 0.78)
        } catch { failUpload('浏览器无法编码当前画面，已停止上传。请重新连接上传。') }
      }, Math.max(125, Math.round(1000 / Math.min(8, Math.max(1, targetFps)))))
    }
    socket.onmessage = (event) => {
      if (socketRef.current !== socket || !mountedRef.current) return
      try {
        const message = JSON.parse(String(event.data)) as { type?: string; camera_id?: string; status?: string; error?: string; dropped_frames?: number; sequence?: number }
        if (message.type === 'ready') {
          if (message.camera_id !== id) { failUpload('后端返回的摄像头身份不一致，已拒绝发送画面。'); return }
          if (ready) return
          ready = true
          if (readyTimeoutRef.current !== null) window.clearTimeout(readyTimeoutRef.current)
          readyTimeoutRef.current = null
          setState((value) => ({ ...value, uploadState: 'connecting', lastError: '' }))
          startFramePump()
        } else if (message.type === 'ack' || message.status === 'streaming') {
          if (!ready || !inFlightRef.current || !expectedAck || message.sequence !== expectedAck) return
          expectedAck = 0
          inFlightRef.current = false
          setState((value) => ({ ...value, uploadState: 'online', uploadedFrames: value.uploadedFrames + 1, droppedFrames: Math.max(value.droppedFrames, message.dropped_frames || 0), lastError: '' }))
        } else if (message.error) {
          failUpload(message.error)
        }
      } catch { /* A malformed acknowledgement must not stop local preview. */ }
    }
    socket.onerror = () => {
      failUpload('浏览器画面上传通道连接失败，已停止发送。请确认主机在线后重新连接上传。')
    }
    socket.onclose = () => {
      if (socketRef.current !== socket) return
      closeSocket()
      if (mountedRef.current && streamRef.current) setState((value) => ({ ...value, uploadState: 'disconnected' }))
    }
  }, [closeSocket, releaseCapture, targetFps, targetHeight, targetWidth])

  useEffect(() => {
    if (cameraId && streamRef.current) beginUpload(cameraId)
    else if (!cameraId) closeSocket()
  }, [beginUpload, cameraId, closeSocket])

  useEffect(() => {
    if (!navigator.permissions?.query || !navigator.mediaDevices) return
    let disposed = false, observed: PermissionStatus | null = null
    navigator.permissions.query({ name: 'camera' as PermissionName }).then((permission) => {
      if (disposed) return
      observed = permission
      setState((value) => ({ ...value, permission: permission.state as BrowserPermission }))
      permission.onchange = () => {
        if (disposed) return
        if (permission.state === 'denied') releaseCapture('摄像头权限已撤销，采集和上传已停止。', 'disconnected')
        setState((value) => ({ ...value, permission: permission.state as BrowserPermission }))
      }
    }).catch(() => { /* Some browsers expose mediaDevices without the camera permission query. */ })
    return () => { disposed = true; if (observed) observed.onchange = null }
  }, [releaseCapture])

  useEffect(() => {
    const pauseHidden = () => {
      if (document.visibilityState === 'hidden') releaseCapture('页面已进入后台，摄像头和上传已暂停。返回后请重新授权启动。', 'disconnected')
    }
    const leave = () => releaseCapture()
    document.addEventListener('visibilitychange', pauseHidden)
    window.addEventListener('pagehide', leave)
    return () => { document.removeEventListener('visibilitychange', pauseHidden); window.removeEventListener('pagehide', leave) }
  }, [releaseCapture])

  async function startPreview(deviceId?: string) {
    if (!window.isSecureContext) return setState((value) => ({ ...value, permission: 'insecure', lastError: '浏览器摄像头需要使用 localhost 或 HTTPS 访问。' }))
    if (!navigator.mediaDevices?.getUserMedia) return setState((value) => ({ ...value, permission: 'unsupported', lastError: '这个浏览器不支持摄像头直连，请使用新版 Edge 或 Chrome。' }))
    stopPreview()
    const generation = captureGeneration.current
    setStarting(true)
    const isCurrent = () => mountedRef.current && captureGeneration.current === generation && currentCameraId.current === cameraId && document.visibilityState !== 'hidden'
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: {
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
        width: { ideal: targetWidth }, height: { ideal: targetHeight }, frameRate: { ideal: 20, max: 30 },
      } })
      if (!isCurrent()) { stream.getTracks().forEach(track => track.stop()); return }
      streamRef.current = stream
      previewingRef.current = true
      const video = videoRef.current
      if (!video) { stream.getTracks().forEach((track) => track.stop()); return }
      video.srcObject = stream; await video.play()
      if (!isCurrent()) { stream.getTracks().forEach(track => track.stop()); return }
      const track = stream.getVideoTracks()[0]; const settings = track.getSettings()
      const devices = (await navigator.mediaDevices.enumerateDevices().catch(() => [])).filter((item) => item.kind === 'videoinput')
      if (!isCurrent()) return
      fpsFrameRef.current = 0; fpsStartedRef.current = performance.now()
      setState((value) => ({ ...value, permission: 'granted', devices, selectedDeviceId: settings.deviceId || deviceId || '', width: settings.width || video.videoWidth, height: settings.height || video.videoHeight, previewing: true, lastError: '', uploadState: cameraId ? 'connecting' : 'idle' }))
      track.onended = () => releaseCapture('摄像头画面已由系统停止，上传通道与设备轨道已释放。', 'disconnected')
      if (cameraId) beginUpload(cameraId)
    } catch (error) {
      if (!isCurrent()) return
      releaseCapture()
      const name = (error as DOMException)?.name
      setState((value) => ({ ...value, permission: name === 'NotAllowedError' || name === 'SecurityError' ? 'denied' : value.permission, previewing: false, lastError: browserCameraError(error) }))
    } finally { if (mountedRef.current && captureGeneration.current === generation) setStarting(false) }
  }

  useEffect(() => {
    const video = videoRef.current
    if (!state.previewing || !video) return
    fpsFrameRef.current = 0; fpsStartedRef.current = performance.now()
    const update = (now: number) => {
      if (!previewingRef.current || !streamRef.current?.active) return
      fpsFrameRef.current += 1
      const elapsed = now - fpsStartedRef.current
      if (elapsed >= 1000) {
        const fps = fpsFrameRef.current * 1000 / elapsed
        fpsFrameRef.current = 0; fpsStartedRef.current = now
        setState((value) => ({ ...value, fps }))
      }
    }
    const record = (now: number) => {
      update(now)
      if (!previewingRef.current || !streamRef.current?.active) return
      fpsCallbackRef.current = video.requestVideoFrameCallback(record)
    }
    if (typeof video.requestVideoFrameCallback === 'function') {
      fpsCallbackRef.current = video.requestVideoFrameCallback(record)
      return () => { if (fpsCallbackRef.current !== null && typeof video.cancelVideoFrameCallback === 'function') video.cancelVideoFrameCallback(fpsCallbackRef.current); fpsCallbackRef.current = null }
    }
    let lastTime = video.currentTime
    const timer = window.setInterval(() => {
      if (video.currentTime !== lastTime) { lastTime = video.currentTime; update(performance.now()) }
    }, 50)
    return () => window.clearInterval(timer)
  }, [state.previewing]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false; captureGeneration.current += 1; previewingRef.current = false; closeSocket(); streamRef.current?.getTracks().forEach((track) => { track.onended = null; track.stop() }); streamRef.current = null }
  }, [closeSocket])

  return <section className="browser-camera" aria-label="浏览器直连摄像头">
    {/* Keep the capture video mounted: canvas uploads require this exact source. */}
    <div className={`browser-camera-stage${showPreview ? '' : ' browser-camera-stage-capture-only'}`} aria-hidden={!showPreview || undefined} style={{ aspectRatio: state.width && state.height ? `${state.width}/${state.height}` : '4/3' }}>
      <video ref={videoRef} muted playsInline aria-label="浏览器摄像头实时预览" />
      {!state.previewing && <div className="browser-camera-empty"><Camera /><strong>浏览器尚未读取摄像头</strong><span>只有点击下方按钮后才会请求权限。</span></div>}
      {zones.length > 0 && state.previewing && <svg className="zone-overlay" viewBox="0 0 1000 750" preserveAspectRatio="none" aria-hidden="true">{zones.filter((zone) => zone.enabled).map((zone) => <g key={zone.id}><polygon points={zone.points.map(([x, y]) => `${x * 1000},${y * 750}`).join(' ')} />{zone.points[0] && <text x={zone.points[0][0] * 1000 + 8} y={zone.points[0][1] * 750 + 22}>{zone.name}</text>}</g>)}</svg>}
      <div className="browser-camera-badges"><Badge tone={state.permission === 'granted' ? 'success' : state.permission === 'denied' || state.permission === 'insecure' ? 'danger' : 'warning'}>{state.permission === 'granted' ? '权限已允许' : state.permission === 'denied' ? '权限被拒绝' : state.permission === 'insecure' ? '非安全上下文' : '等待授权'}</Badge>{cameraId && <Badge tone={state.uploadState === 'online' ? 'success' : state.uploadState === 'error' ? 'danger' : 'warning'}>{state.uploadState === 'online' ? '后端接收中' : state.uploadState === 'connecting' ? '正在连接后端' : '上传未连接'}</Badge>}</div>
    </div>
    {!showPreview && <p className="photo-help" role="status">{state.previewing ? state.uploadState === 'online' ? '本地摄像头已授权，正在向主画面发送帧。' : '本地摄像头已授权，上传通道尚未就绪。' : '点击授权后开始本地采集；主画面显示后端接收到的同帧视频。'}</p>}
    {state.lastError && <div className="inline-banner danger"><AlertTriangle />{state.lastError}</div>}
    {!window.isSecureContext && <div className="inline-banner danger" role="note"><ShieldCheck /><span>这个 HTTP 手机页面可以管理资料、上传照片、查看主机画面，但浏览器禁止它持续调用手机摄像头。持续采集需要手机信任的 HTTPS 地址；localhost 仅指当前设备，不能在手机上代替电脑地址。也可使用电脑或已连接开发板采集。</span></div>}
    <div className="browser-camera-controls">
      <div className="browser-camera-actions">{starting ? <button type="button" className="button secondary" onClick={stopPreview}>取消摄像头请求</button> : !state.previewing ? <button type="button" className="button primary" disabled={!state.secureContext || state.permission === 'unsupported'} onClick={() => void startPreview()}><Camera />授权并选择摄像头</button> : <button type="button" className="button secondary" onClick={stopPreview}><CameraOff />停止浏览器画面</button>}{state.previewing && cameraId && ['disconnected', 'error'].includes(state.uploadState) && <button type="button" className="button secondary" onClick={() => beginUpload(cameraId)}>重新连接上传</button>}{state.devices.length > 0 && <label><span>视频设备</span><select aria-label="浏览器摄像头设备" disabled={starting} value={state.selectedDeviceId} onChange={(event) => void startPreview(event.target.value)}>{state.devices.map((device, index) => <option value={device.deviceId} key={device.deviceId || index}>{device.label || `摄像头 ${index + 1}`}</option>)}</select></label>}</div>
      <dl><div><dt>安全上下文</dt><dd>{state.secureContext ? <><CheckCircle2 />是</> : <><AlertTriangle />否</>}</dd></div><div><dt>分辨率</dt><dd>{state.width && state.height ? `${state.width}×${state.height}` : '—'}</dd></div><div><dt>浏览器 FPS</dt><dd>{state.previewing ? state.fps.toFixed(1) : '—'}</dd></div><div><dt>后端通道</dt><dd>{state.uploadState === 'online' ? <><Wifi />已连接</> : <><WifiOff />未连接</>}</dd></div><div><dt>已上传</dt><dd>{state.uploadedFrames} 帧</dd></div><div><dt>背压丢帧</dt><dd>{state.droppedFrames} 帧</dd></div></dl>
    </div>
  </section>
}
