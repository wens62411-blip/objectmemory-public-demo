import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CameraPhotoCapture } from '../src/components/CameraPhotoCapture'
import { PhotoFilePicker } from '../src/components/PhotoRegistration'

function cameraStream(name = 'rear', facingMode?: 'environment' | 'user') {
  const stop = vi.fn()
  const track = { stop, onended: null as null | (() => void), readyState: 'live' as MediaStreamTrackState, getSettings: () => ({ facingMode }) }
  const stream = {
    active: true,
    id: name,
    getTracks: () => [track],
    getVideoTracks: () => [track],
  } as unknown as MediaStream
  return { stop, stream, track }
}

describe('直接拍摄参考照片', () => {
  const rear = cameraStream('rear', 'environment')
  const getUserMedia = vi.fn()
  const drawImage = vi.fn()
  let createdUrl = 0

  beforeEach(() => {
    createdUrl = 0
    rear.stop.mockClear()
    getUserMedia.mockReset().mockResolvedValue(rear.stream)
    drawImage.mockClear()
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:camera-${++createdUrl}`) })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    Object.defineProperty(HTMLMediaElement.prototype, 'play', { configurable: true, value: vi.fn().mockResolvedValue(undefined) })
    Object.defineProperty(HTMLMediaElement.prototype, 'pause', { configurable: true, value: vi.fn() })
    Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', { configurable: true, get: () => 1280 })
    Object.defineProperty(HTMLVideoElement.prototype, 'videoHeight', { configurable: true, get: () => 720 })
    Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', { configurable: true, value: vi.fn(() => ({ drawImage })) })
    Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', { configurable: true, value: vi.fn((callback: BlobCallback, type?: string) => callback(new Blob(['jpeg-frame'], { type: type || 'image/jpeg' }))) })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('使用真实摄像头按钮和快门生成JPEG，再进入同一待上传队列', async () => {
    let selected: File[] = []
    function Picker() {
      const [files, setFiles] = useState<File[]>([])
      return <PhotoFilePicker files={files} onChange={(value) => { selected = value; setFiles(value) }} />
    }
    const view = render(<StrictMode><Picker /></StrictMode>)
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(view.container.querySelectorAll('input[type="file"]')).toHaveLength(1)
    expect(view.container.querySelector('input[capture]')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledWith({
      audio: false,
      video: { width: { ideal: 1920 }, height: { ideal: 1080 }, facingMode: { ideal: 'environment' } },
    }))
    await screen.findByRole('button', { name: '请求切换到前置' })
    const video = screen.getByLabelText('拍摄参考照片实时预览') as HTMLVideoElement
    await waitFor(() => expect(video.srcObject).toBe(rear.stream))
    fireEvent.loadedData(video)
    const shutter = screen.getByRole('button', { name: '拍下这张' })
    expect(shutter).toBeEnabled()
    fireEvent.click(shutter)

    await screen.findByAltText('刚拍摄的参考照片预览')
    expect(drawImage).toHaveBeenCalledWith(video, 0, 0, 1280, 720)
    expect(HTMLCanvasElement.prototype.toBlob).toHaveBeenCalledWith(expect.any(Function), 'image/jpeg', 0.92)
    expect(rear.stop).toHaveBeenCalledTimes(1)
    expect(video.srcObject).toBeNull()
    expect(selected).toEqual([])

    const usePhoto = screen.getByRole('button', { name: '使用这张照片' })
    await waitFor(() => expect(usePhoto).toHaveFocus())
    fireEvent.click(usePhoto)
    await waitFor(() => expect(selected).toHaveLength(1))
    expect(selected[0]).toBeInstanceOf(File)
    expect(selected[0].type).toBe('image/jpeg')
    expect(selected[0].name).toMatch(/^camera-.*\.jpg$/)
    expect(screen.getByText(selected[0].name)).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '拍摄参考照片' })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: '直接拍照' })).toHaveFocus())
  })

  it('切换前置摄像头前释放旧轨道，并使用新的facingMode请求', async () => {
    const front = cameraStream('front', 'user')
    getUserMedia.mockResolvedValueOnce(rear.stream).mockResolvedValueOnce(front.stream)
    render(<CameraPhotoCapture onCapture={() => true} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    const switcher = await screen.findByRole('button', { name: '请求切换到前置' })
    fireEvent.click(switcher)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2))
    expect(rear.stop).toHaveBeenCalledTimes(1)
    expect(getUserMedia.mock.calls[1][0]).toMatchObject({ audio: false, video: { facingMode: { ideal: 'user' } } })
    expect(await screen.findByText('前置摄像头已开启')).toBeInTheDocument()
    const video = screen.getByLabelText('拍摄参考照片实时预览') as HTMLVideoElement
    await waitFor(() => expect(video.srcObject).toBe(front.stream))
    const shutter = screen.getByRole('button', { name: '拍下这张' })
    expect(shutter).toBeDisabled()
    fireEvent.loadedData(video)
    await waitFor(() => expect(shutter).toHaveFocus())
  })

  it('关闭授权中的窗口后，迟到的摄像头流也会立即释放', async () => {
    let resolve!: (stream: MediaStream) => void
    getUserMedia.mockReturnValue(new Promise<MediaStream>((done) => { resolve = done }))
    render(<CameraPhotoCapture onCapture={() => true} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await screen.findByRole('dialog', { name: '拍摄参考照片' })
    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    await act(async () => { resolve(rear.stream); await Promise.resolve() })
    expect(rear.stop).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog', { name: '拍摄参考照片' })).not.toBeInTheDocument()
  })

  it.each(['visibility', 'pagehide'] as const)('等待授权时%s会作废请求，迟到流不能在后台开启', async (reason) => {
    let resolve!: (stream: MediaStream) => void
    getUserMedia.mockReturnValue(new Promise<MediaStream>((done) => { resolve = done }))
    const visibility = reason === 'visibility' ? vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible') : null
    render(<CameraPhotoCapture onCapture={() => true} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await screen.findByRole('dialog', { name: '拍摄参考照片' })
    if (visibility) {
      visibility.mockReturnValue('hidden')
      fireEvent(document, new Event('visibilitychange'))
    } else fireEvent(window, new Event('pagehide'))
    expect(await screen.findByRole('alert')).toHaveTextContent('页面进入后台后已停止摄像头')
    await act(async () => { resolve(rear.stream); await Promise.resolve() })
    expect(rear.stop).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('后置摄像头已开启')).not.toBeInTheDocument()
  })

  it('浏览器未报告实际朝向时只说明请求偏好，不宣称后置镜头已开启', async () => {
    const unknown = cameraStream('unknown')
    getUserMedia.mockResolvedValue(unknown.stream)
    render(<CameraPhotoCapture onCapture={() => true} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    expect(await screen.findByText('摄像头已开启 · 已请求后置')).toBeInTheDocument()
    expect(screen.queryByText('后置摄像头已开启')).not.toBeInTheDocument()
  })

  it('切换时旧视频迟到的loadedData不能提前启用新流快门', async () => {
    const front = cameraStream('front', 'user')
    let resolveFront!: (stream: MediaStream) => void
    getUserMedia.mockResolvedValueOnce(rear.stream).mockReturnValueOnce(new Promise<MediaStream>((done) => { resolveFront = done }))
    render(<CameraPhotoCapture onCapture={() => true} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await screen.findByRole('button', { name: '请求切换到前置' })
    const oldVideo = screen.getByLabelText('拍摄参考照片实时预览') as HTMLVideoElement
    await waitFor(() => expect(oldVideo.srcObject).toBe(rear.stream))
    fireEvent.loadedData(oldVideo)
    fireEvent.click(screen.getByRole('button', { name: '请求切换到前置' }))
    await act(async () => { resolveFront(front.stream); await Promise.resolve() })
    const newVideo = await screen.findByLabelText('拍摄参考照片实时预览') as HTMLVideoElement
    expect(newVideo).not.toBe(oldVideo)
    await waitFor(() => expect(newVideo.srcObject).toBe(front.stream))
    const shutter = screen.getByRole('button', { name: '拍下这张' })
    fireEvent.loadedData(oldVideo)
    expect(shutter).toBeDisabled()
    fireEvent.loadedData(newVideo)
    expect(shutter).toBeEnabled()
  })

  it('预览播放失败时释放摄像头并显示重试入口', async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValue(new Error('play failed'))
    render(<CameraPhotoCapture onCapture={() => true} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('没有开始播放摄像头画面')
    expect(rear.stop).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: '重新打开摄像头' })).toBeInTheDocument()
  })

  it('编码尚未完成时关闭窗口，迟到结果不会生成或加入照片', async () => {
    let finishEncoding!: BlobCallback
    Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', { configurable: true, value: vi.fn((callback: BlobCallback) => { finishEncoding = callback }) })
    const captured = vi.fn(() => true)
    render(<CameraPhotoCapture onCapture={captured} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await screen.findByRole('button', { name: '请求切换到前置' })
    const video = screen.getByLabelText('拍摄参考照片实时预览') as HTMLVideoElement
    await waitFor(() => expect(video.srcObject).toBe(rear.stream))
    fireEvent.loadedData(video)
    const shutter = screen.getByRole('button', { name: '拍下这张' })
    await waitFor(() => expect(shutter).toBeEnabled())
    fireEvent.click(shutter)
    await waitFor(() => expect(typeof finishEncoding).toBe('function'))
    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    await act(async () => { finishEncoding(new Blob(['late-jpeg'], { type: 'image/jpeg' })); await Promise.resolve() })
    expect(captured).not.toHaveBeenCalled()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog', { name: '拍摄参考照片' })).not.toBeInTheDocument()
  })

  it('非安全HTTP地址只说明限制，不调用摄像头或伪装成文件拍摄', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false })
    render(<PhotoFilePicker files={[]} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('不是浏览器认可的安全地址')
    expect(screen.getByRole('note')).toHaveTextContent('受信任的 HTTPS')
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(screen.getByLabelText('选择参考照片')).toHaveAttribute('type', 'file')
  })

  it('权限拒绝显示可操作错误，不把失败当成已拍照', async () => {
    getUserMedia.mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    const captured = vi.fn(() => true)
    render(<CameraPhotoCapture onCapture={captured} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('没有获得摄像头权限')
    expect(screen.getByRole('button', { name: '重新打开摄像头' })).toBeInTheDocument()
    expect(captured).not.toHaveBeenCalled()
  })

  it.each(['visibility', 'pagehide', 'track-ended'] as const)('%s中断时停止轨道，且不自动保存或重启', async (reason) => {
    const captured = vi.fn(() => true)
    const visibility = reason === 'visibility' ? vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible') : null
    render(<CameraPhotoCapture onCapture={captured} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await screen.findByRole('button', { name: '请求切换到前置' })
    if (visibility) {
      visibility.mockReturnValue('hidden')
      fireEvent(document, new Event('visibilitychange'))
    } else if (reason === 'pagehide') fireEvent(window, new Event('pagehide'))
    else rear.track.onended?.()
    expect(rear.stop).toHaveBeenCalledTimes(1)
    expect(await screen.findByRole('alert')).toHaveTextContent(reason === 'track-ended' ? '摄像头连接已经中断' : '页面进入后台后已停止摄像头')
    expect(captured).not.toHaveBeenCalled()
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('JPEG编码失败保留预览并明确报错，不加入照片', async () => {
    Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', { configurable: true, value: vi.fn((callback: BlobCallback) => callback(null)) })
    const captured = vi.fn(() => true)
    render(<CameraPhotoCapture onCapture={captured} />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await screen.findByRole('button', { name: '请求切换到前置' })
    const video = screen.getByLabelText('拍摄参考照片实时预览') as HTMLVideoElement
    await waitFor(() => expect(video.srcObject).toBe(rear.stream))
    fireEvent.loadedData(video)
    const shutter = screen.getByRole('button', { name: '拍下这张' })
    await waitFor(() => expect(shutter).toBeEnabled())
    fireEvent.click(shutter)
    expect(await screen.findByRole('alert')).toHaveTextContent('拍照编码失败')
    expect(captured).not.toHaveBeenCalled()
    expect(screen.queryByAltText('刚拍摄的参考照片预览')).not.toBeInTheDocument()
    expect(rear.stop).not.toHaveBeenCalled()
  })

  it('拍下第12张后触发按钮禁用，焦点回退到可移除照片的按钮', async () => {
    const originals = Array.from({ length: 11 }, (_, index) => new File(['reference'], `reference-${index}.jpg`, { type: 'image/jpeg', lastModified: index + 1 }))
    function Picker() {
      const [files, setFiles] = useState(originals)
      return <PhotoFilePicker files={files} onChange={setFiles} />
    }
    render(<Picker />)
    fireEvent.click(screen.getByRole('button', { name: '直接拍照' }))
    await screen.findByRole('button', { name: '请求切换到前置' })
    const video = screen.getByLabelText('拍摄参考照片实时预览') as HTMLVideoElement
    await waitFor(() => expect(video.srcObject).toBe(rear.stream))
    fireEvent.loadedData(video)
    const shutter = screen.getByRole('button', { name: '拍下这张' })
    await waitFor(() => expect(shutter).toBeEnabled())
    fireEvent.click(shutter)
    fireEvent.click(await screen.findByRole('button', { name: '使用这张照片' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '直接拍照' })).toBeDisabled())
    await waitFor(() => expect(screen.getByRole('button', { name: '移除待上传照片 1' })).toHaveFocus())
  })

  it('达到参考照片上限时不再申请摄像头权限', () => {
    render(<PhotoFilePicker files={[]} existingCount={12} onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: '直接拍照' })).toBeDisabled()
    expect(getUserMedia).not.toHaveBeenCalled()
  })
})
