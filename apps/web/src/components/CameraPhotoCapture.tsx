import { Camera, Check, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { Badge, Modal } from './UI'

type FacingMode = 'environment' | 'user'
type CapturePhase = 'idle' | 'starting' | 'preview' | 'captured' | 'error'

type CapturedPhoto = {
  file: File
  height: number
  url: string
  width: number
}

const CAMERA_CONSTRAINTS = {
  width: { ideal: 1920 },
  height: { ideal: 1080 },
} as const

function cameraError(value: unknown) {
  const name = value instanceof DOMException ? value.name : value instanceof Error ? value.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') return '没有获得摄像头权限。请在浏览器地址栏允许摄像头后重试。'
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') return '没有找到可用摄像头。请检查设备连接，或改用“选择照片”。'
  if (name === 'NotReadableError' || name === 'TrackStartError') return '摄像头可能正被其他应用占用。关闭占用程序后重试。'
  if (name === 'OverconstrainedError' || name === 'ConstraintNotSatisfiedError') return '当前摄像头不支持请求的拍摄模式，请切换前后摄像头后重试。'
  return '摄像头没有成功启动。请检查浏览器权限和设备状态后重试。'
}

function stopTracks(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => {
    track.onended = null
    track.stop()
  })
}

export function CameraPhotoCapture({ disabled = false, fallbackFocusRef, onCapture }: {
  disabled?: boolean
  fallbackFocusRef?: RefObject<HTMLElement>
  onCapture: (file: File) => boolean
}) {
  const [open, setOpen] = useState(false)
  const [phase, setPhase] = useState<CapturePhase>('idle')
  const [facingMode, setFacingMode] = useState<FacingMode>('environment')
  const [actualFacingMode, setActualFacingMode] = useState<FacingMode | null>(null)
  const [stream, setStream] = useState<MediaStream | null>(null)
  const [captured, setCaptured] = useState<CapturedPhoto | null>(null)
  const [ready, setReady] = useState(false)
  const [encoding, setEncoding] = useState(false)
  const [error, setError] = useState('')
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const captureUrlRef = useRef('')
  const requestGeneration = useRef(0)
  const mounted = useRef(true)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const primaryActionRef = useRef<HTMLButtonElement>(null)
  const secondaryActionRef = useRef<HTMLButtonElement>(null)

  const releaseStream = useCallback(() => {
    const current = streamRef.current
    streamRef.current = null
    stopTracks(current)
    const video = videoRef.current
    if (video?.srcObject === current) {
      video.pause()
      video.srcObject = null
    }
    setStream(null)
    setReady(false)
    setActualFacingMode(null)
  }, [])

  const releaseCapture = useCallback(() => {
    if (captureUrlRef.current) URL.revokeObjectURL(captureUrlRef.current)
    captureUrlRef.current = ''
    setCaptured(null)
  }, [])

  const close = useCallback(() => {
    requestGeneration.current += 1
    releaseStream()
    releaseCapture()
    setOpen(false)
    setPhase('idle')
    setEncoding(false)
    setError('')
    window.setTimeout(() => {
      if (!mounted.current) return
      if (!triggerRef.current?.disabled) triggerRef.current?.focus({ preventScroll: true })
      else fallbackFocusRef?.current?.focus({ preventScroll: true })
    })
  }, [fallbackFocusRef, releaseCapture, releaseStream])

  const start = useCallback(async (requestedFacing: FacingMode = facingMode) => {
    if (disabled) return
    const generation = ++requestGeneration.current
    releaseStream()
    releaseCapture()
    setFacingMode(requestedFacing)
    setOpen(true)
    setEncoding(false)
    setError('')
    setPhase('starting')
    if (!window.isSecureContext) {
      setPhase('error')
      setError('当前地址不是浏览器认可的安全地址，网页不能直接调用摄像头。')
      return
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setPhase('error')
      setError('当前浏览器不支持网页摄像头，请升级浏览器或改用“选择照片”。')
      return
    }
    try {
      const next = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { ...CAMERA_CONSTRAINTS, facingMode: { ideal: requestedFacing } },
      })
      if (!mounted.current || requestGeneration.current !== generation || document.visibilityState === 'hidden') {
        stopTracks(next)
        if (mounted.current && requestGeneration.current === generation) {
          requestGeneration.current += 1
          setPhase('error')
          setError('页面已进入后台，摄像头请求已经取消，没有保存照片。')
        }
        return
      }
      const videoTrack = next.getVideoTracks()[0]
      if (!videoTrack) {
        stopTracks(next)
        setPhase('error')
        setError('浏览器没有返回可用的视频轨道，请检查摄像头后重试。')
        return
      }
      streamRef.current = next
      const reportedFacing = videoTrack.getSettings?.().facingMode
      setActualFacingMode(reportedFacing === 'environment' || reportedFacing === 'user' ? reportedFacing : null)
      next.getVideoTracks().forEach((track) => {
        track.onended = () => {
          if (!mounted.current || streamRef.current !== next) return
          requestGeneration.current += 1
          releaseStream()
          setPhase('error')
          setError('摄像头连接已经中断，没有保存照片。请重新打开摄像头。')
        }
      })
      setStream(next)
      setPhase('preview')
    } catch (value) {
      if (!mounted.current || requestGeneration.current !== generation) return
      setPhase('error')
      setError(cameraError(value))
    }
  }, [disabled, facingMode, releaseCapture, releaseStream])

  useLayoutEffect(() => {
    if (!open || phase !== 'preview' || !stream) return
    const video = videoRef.current
    if (!video) return
    let active = true
    video.srcObject = stream
    void video.play().catch(() => {
      if (!active || streamRef.current !== stream) return
      requestGeneration.current += 1
      releaseStream()
      setPhase('error')
      setError('浏览器没有开始播放摄像头画面，已释放摄像头。请重试。')
    })
    return () => {
      active = false
      if (video.srcObject === stream) {
        video.pause()
        video.srcObject = null
      }
    }
  }, [open, phase, releaseStream, stream])

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => {
      const target = phase === 'preview' && !ready ? secondaryActionRef.current : primaryActionRef.current
      const focusTarget = target || secondaryActionRef.current
      focusTarget?.focus()
    })
    return () => window.clearTimeout(timer)
  }, [open, phase, ready])

  useEffect(() => {
    if (!open) return
    const suspend = () => {
      if (phase !== 'starting' && phase !== 'preview') return
      requestGeneration.current += 1
      releaseStream()
      setPhase('error')
      setError('页面进入后台后已停止摄像头，没有保存照片。返回页面后请重新拍摄。')
    }
    const hidden = () => { if (document.visibilityState === 'hidden') suspend() }
    document.addEventListener('visibilitychange', hidden)
    window.addEventListener('pagehide', suspend)
    return () => {
      document.removeEventListener('visibilitychange', hidden)
      window.removeEventListener('pagehide', suspend)
    }
  }, [open, phase, releaseStream])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      requestGeneration.current += 1
      stopTracks(streamRef.current)
      streamRef.current = null
      if (captureUrlRef.current) URL.revokeObjectURL(captureUrlRef.current)
      captureUrlRef.current = ''
    }
  }, [])

  async function takePhoto() {
    const video = videoRef.current
    if (encoding) return
    if (!ready || !video?.videoWidth || !video.videoHeight || streamRef.current !== stream) {
      setError('摄像头画面尚未准备好，请稍等后再拍。')
      return
    }
    const generation = requestGeneration.current
    setEncoding(true)
    setError('')
    try {
      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
      const context = canvas.getContext('2d')
      if (!context) throw new Error('canvas_unavailable')
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error('jpeg_encode_failed')), 'image/jpeg', 0.92))
      if (!mounted.current || requestGeneration.current !== generation || streamRef.current !== stream) return
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-')
      const file = new File([blob], `camera-${timestamp}.jpg`, { type: 'image/jpeg', lastModified: Date.now() })
      const url = URL.createObjectURL(file)
      releaseStream()
      releaseCapture()
      captureUrlRef.current = url
      setCaptured({ file, height: canvas.height, url, width: canvas.width })
      setPhase('captured')
    } catch {
      setError('拍照编码失败，没有加入参考照片。请重试或改用“选择照片”。')
    } finally {
      if (mounted.current && requestGeneration.current === generation) setEncoding(false)
    }
  }

  function usePhoto() {
    if (!captured) return
    if (!onCapture(captured.file)) {
      setError('照片未能加入，可能已经达到 12 张上限。请先移除不需要的照片。')
      return
    }
    close()
  }

  return <>
    <button ref={triggerRef} type="button" className="button ghost" disabled={disabled} onClick={() => void start('environment')}><Camera />直接拍照</button>
    {open && <Modal title="拍摄参考照片" description="实时预览只在当前设备处理；确认后才加入待上传照片。" onClose={close}>
      <div className="camera-photo-capture">
        <div className="camera-photo-stage">
          {captured
            ? <img src={captured.url} alt="刚拍摄的参考照片预览" />
            : <video key={`${phase}-${requestGeneration.current}`} ref={videoRef} muted playsInline autoPlay aria-label="拍摄参考照片实时预览" onLoadedData={(event) => {
              if (stream && streamRef.current === stream && event.currentTarget.srcObject === stream) setReady(true)
            }} />}
          {phase === 'starting' && <div className="camera-photo-placeholder" role="status"><Camera /><strong>正在请求摄像头权限…</strong></div>}
          {phase === 'error' && <div className="camera-photo-placeholder"><Camera /><strong>摄像头尚未启动</strong></div>}
          {phase === 'preview' && <span className="camera-live-indicator"><i />实时画面</span>}
        </div>
        <div className="camera-photo-status" aria-live="polite">
          <Badge tone={phase === 'preview' ? 'success' : phase === 'captured' ? 'blue' : phase === 'error' ? 'danger' : 'neutral'}>
            {phase === 'preview' ? actualFacingMode ? `${actualFacingMode === 'environment' ? '后置' : '前置'}摄像头已开启` : `摄像头已开启 · 已请求${facingMode === 'environment' ? '后置' : '前置'}` : phase === 'captured' ? `${captured?.width}×${captured?.height} 已拍摄` : phase === 'starting' ? '等待授权' : '摄像头未开启'}
          </Badge>
          <span>{phase === 'captured' ? '检查清晰度和目标是否完整，再决定使用或重拍。' : '默认请求后置摄像头；照片不会自动上传或建立识别档案。'}</span>
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
        {!window.isSecureContext && <p className="camera-security-note" role="note">手机需通过浏览器认可并受信任的 HTTPS 地址打开，普通局域网 HTTP 页面不能直接调用摄像头。你仍可关闭此窗口并使用“选择照片”。</p>}
        <div className="camera-photo-actions">
          {phase === 'starting' && <button ref={primaryActionRef} type="button" className="button secondary" onClick={close}>取消摄像头请求</button>}
          {phase === 'preview' && <>
            <button ref={secondaryActionRef} type="button" className="button secondary" disabled={encoding} onClick={() => void start((actualFacingMode || facingMode) === 'environment' ? 'user' : 'environment')}><RefreshCw />请求切换到{(actualFacingMode || facingMode) === 'environment' ? '前置' : '后置'}</button>
            <button ref={primaryActionRef} type="button" className="button primary camera-shutter" disabled={!ready || encoding} onClick={() => void takePhoto()}><Camera />{encoding ? '正在拍照…' : '拍下这张'}</button>
          </>}
          {phase === 'captured' && <>
            <button ref={secondaryActionRef} type="button" className="button secondary" onClick={() => void start(facingMode)}><RefreshCw />重拍</button>
            <button ref={primaryActionRef} type="button" className="button primary" onClick={usePhoto}><Check />使用这张照片</button>
          </>}
          {phase === 'error' && (window.isSecureContext && typeof navigator.mediaDevices?.getUserMedia === 'function'
            ? <button ref={primaryActionRef} type="button" className="button primary" onClick={() => void start(facingMode)}><RefreshCw />重新打开摄像头</button>
            : <button ref={primaryActionRef} type="button" className="button secondary" onClick={close}>关闭并选择照片</button>)}
        </div>
      </div>
    </Modal>}
  </>
}
