import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PhotoFilePicker, PhotoRegistration, profileIsLoaded } from '../src/components/PhotoRegistration'
import { ItemsPage } from '../src/pages/ItemsPage'
import { ToastProvider } from '../src/components/Toast'
import type { Camera, Item, RecognitionProfile, RecognitionTest, ReferenceImage } from '../src/types'

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const phone: Item = { id: 'photo-phone', name: '照片手机', type: '手机', aliases: [], aruco_id: null, ring_enabled: false }
const reference: ReferenceImage = { id: 'ref-one', path: '/media/registered-items/photo-phone/one.jpg', width: 640, height: 480, sha256: 'fixture-hash', region: null, region_confirmed: false, status: 'image_saved', suggested_regions: [{ bbox: [0.1, 0.2, 0.3, 0.4], label: 'cell phone', score: 0.91 }, { bbox: [0.6, 0.2, 0.2, 0.5], label: 'cell phone', score: 0.85 }] }
const saved: RecognitionProfile = { registration_status: 'images_saved', profile_version: 1, model_id: null, model_version: null, reference_count: 1, ready_reference_count: 0, loaded_profile_version: null, loaded_camera_ids: [] }
const ready: RecognitionProfile = { ...saved, registration_status: 'ready', profile_version: 2, model_id: 'local-features', model_version: '1.0', ready_reference_count: 1, loaded_profile_version: 2, loaded_camera_ids: [] }
const cameras: Camera[] = [
  { id: 'cam-one', name: '书桌相机', room_name: '书房', source_type: 'opencv_camera', source: '0', enabled: true, inference_fps: 5, save_clips: true, runtime_mode: 'REAL', is_simulated: false },
  { id: 'cam-two', name: '回放相机', room_name: '测试', source_type: 'video_file', source: 'test.mp4', enabled: true, inference_fps: 5, save_clips: true, runtime_mode: 'DEMO', is_simulated: true },
]
const result: RecognitionTest = { image_data_url: 'data:image/jpeg;base64,dGVzdA==', source_session_id: 'unit-contract-session', source_frame: 23, source_timestamp: '2026-09-05T13:00:00Z', model: { model_id: 'local-features', model_version: '1.0', status: 'ready' }, latency_ms: 51.3, loaded_profiles: [{ item_id: phone.id, name: phone.name, profile_version: 2 }], candidates: [{ bbox: [0.2, 0.3, 0.2, 0.4], category: 'cell phone', best_item_id: phone.id, best_item_name: phone.name, best_score: 0.92, second_item_name: '另一部手机', second_score: 0.9, margin: 0.02, accepted: false, rejection_reason: '相似物品分数差距不足', profile_version: 2 }] }

function setup(options: { profile?: RecognitionProfile; references?: ReferenceImage[]; build?: RecognitionProfile; buildStatus?: number; testResult?: RecognitionTest } = {}) {
  const state = { profile: options.profile || { ...saved }, references: options.references || [{ ...reference }], profileFails: false, testFails: false }
  const requests = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input), method = init?.method || 'GET'
    if (path === '/api/cameras' && method === 'GET') return json(cameras)
    if (path === `/api/items/${phone.id}` && method === 'GET') return json({ ...phone, reference_images: state.references, recognition_profile: state.profile })
    if (path.endsWith('/profile') && method === 'GET') return state.profileFails ? json({ detail: 'profile unavailable' }, 503) : json(state.profile)
    if (path.endsWith('/reference-images') && method === 'POST') { state.references = [{ ...reference }]; state.profile = { ...saved }; return json(state.references) }
    if (path.endsWith('/reference-images/ref-one') && method === 'PATCH') { state.references = [{ ...reference, region: JSON.parse(String(init?.body)).region, region_confirmed: true }]; return json(state.references[0]) }
    if (path.endsWith('/reference-images/ref-one') && method === 'DELETE') { state.references = []; state.profile = { ...saved, registration_status: 'needs_photos', reference_count: 0 }; return json({ success: true }) }
    if (path.endsWith('/reference-capture') && method === 'POST') { state.references = [{ ...reference, id: 'captured-frame' }]; state.profile = { ...saved }; return json(state.references[0]) }
    if (path.endsWith('/profile/build') && method === 'POST') { state.profile = options.build || { ...ready }; return options.buildStatus ? json({ detail: state.profile.error }, options.buildStatus) : json(state.profile) }
    if (path.endsWith('/recognition-test') && method === 'POST') return state.testFails ? json({ detail: '摄像头断开，当前帧不可用' }, 409) : json(options.testResult || result)
    throw new Error(`Unexpected business request: ${method} ${path}`)
  })
  vi.stubGlobal('fetch', requests)
  const changed = vi.fn()
  render(<MemoryRouter><PhotoRegistration item={phone} onClose={vi.fn()} onChanged={changed} /></MemoryRouter>)
  return { requests, state, changed }
}

async function waitLoaded() { await waitFor(() => expect(screen.getByRole('button', { name: '刷新状态' })).toBeEnabled()) }

describe('Mock UI 契约：照片注册与现场测试（不证明实物识别）', () => {
  beforeEach(() => {
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:unit-photo-preview') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
  })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('仅保存照片不显示可识别，多个建议物体仍须用户确认区域', async () => {
    const { requests } = setup()
    await waitLoaded()
    expect(screen.getByText('照片已保存 · 等待建立档案')).toBeInTheDocument()
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '建立 / 更新识别档案' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '确认这是我的物品' })).toBeDisabled()
    fireEvent.load(screen.getByAltText('待确认的原始参考照片'))
    fireEvent.click(screen.getByRole('button', { name: 'cell phone 2' }))
    expect(screen.getByRole('spinbutton', { name: '左边距（%）' })).toHaveValue(60)
    expect(requests.mock.calls.some(([, init]) => init?.method === 'POST' || init?.method === 'PATCH')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '确认这是我的物品' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '建立 / 更新识别档案' })).toBeEnabled())
    const patch = requests.mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(patch[0]).toBe(`/api/items/${phone.id}/reference-images/ref-one`)
    expect(JSON.parse(String(patch[1]?.body))).toEqual({ region: [0.6, 0.2, 0.2, 0.5], confirmed: true })
    expect(requests.mock.calls.some(([path]) => String(path).endsWith('/profile/build'))).toBe(false)
  })

  it('数值越界或损坏的预览不能确认目标', async () => {
    const { requests } = setup()
    await waitLoaded()
    fireEvent.load(screen.getByAltText('待确认的原始参考照片'))
    fireEvent.change(screen.getByRole('spinbutton', { name: '宽度（%）' }), { target: { value: '95' } })
    expect(screen.getByRole('button', { name: '确认这是我的物品' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('框选不能超出照片')
    fireEvent.change(screen.getByRole('spinbutton', { name: '宽度（%）' }), { target: { value: '30' } })
    fireEvent.error(screen.getByAltText('待确认的原始参考照片'))
    expect(screen.getByRole('button', { name: '确认这是我的物品' })).toBeDisabled()
    expect(requests.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
  })

  it('拖动按显示图像坐标归一化，未保存框选不能建立或测试旧档案', async () => {
    const { requests } = setup({ profile: ready, references: [{ ...reference, region: [0.1, 0.2, 0.3, 0.4], region_confirmed: true }] })
    await waitLoaded()
    const image = screen.getByAltText('待确认的原始参考照片')
    fireEvent.load(image)
    const canvas = image.parentElement!
    vi.stubGlobal('PointerEvent', MouseEvent)
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ left: 10, top: 20, width: 200, height: 100, right: 210, bottom: 120, x: 10, y: 20, toJSON: () => ({}) })
    Object.defineProperty(canvas, 'setPointerCapture', { value: vi.fn(), configurable: true })
    fireEvent.pointerDown(canvas, { clientX: 50, clientY: 50, button: 0 })
    fireEvent.pointerMove(canvas, { clientX: 150, clientY: 90 })
    fireEvent.pointerUp(canvas, { clientX: 150, clientY: 90 })
    expect(screen.getByRole('spinbutton', { name: '左边距（%）' })).toHaveValue(20)
    expect(screen.getByRole('spinbutton', { name: '宽度（%）' })).toHaveValue(50)
    expect(screen.getByRole('spinbutton', { name: '高度（%）' })).toHaveValue(40)
    expect(screen.getByRole('button', { name: '建立 / 更新识别档案' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '测试当前画面识别' })).toBeDisabled()
    expect(requests.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '放弃本次框选修改' }))
    expect(screen.getByRole('spinbutton', { name: '左边距（%）' })).toHaveValue(10)
    expect(screen.getByRole('button', { name: '测试当前画面识别' })).toBeEnabled()
  })

  it.each([
    { ...ready, loaded_profile_version: null, loaded_camera_ids: [] },
    { ...ready, loaded_profile_version: 1, loaded_camera_ids: ['cam-one'] },
    { ...ready, loaded_profile_version: 2, loaded_camera_ids: [] },
  ])('未加载当前实时档案时不冒充可识别：%j', async (profile) => {
    setup({ profile, references: [{ ...reference, region_confirmed: true }] })
    await waitLoaded()
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
    expect(screen.getByText('档案已就绪 · 实时摄像头尚未加载当前版本')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '测试当前画面识别' })).toBeEnabled()
  })

  it('只有服务端当前版本真实加载到相机后才显示可识别，读取失败撤下旧绿色状态', async () => {
    const { state } = setup({ profile: { ...ready, loaded_camera_ids: ['cam-one'] } })
    await waitLoaded()
    expect(screen.getByText('已启用当前档案 · 识别效果待现场验证')).toBeInTheDocument()
    state.profileFails = true
    fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
    await screen.findByText('profile unavailable')
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '测试当前画面识别' })).toBeDisabled()
  })

  it('建立档案失败显示模型原始原因，不显示成功或继续测试', async () => {
    const { requests } = setup({ references: [{ ...reference, region: [0.1, 0.2, 0.3, 0.4], region_confirmed: true }], build: { ...ready, registration_status: 'failed', error: '本地模型文件未就绪', loaded_profile_version: null } })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '建立 / 更新识别档案' }))
    await screen.findByText('处理失败 · 请查看原因')
    expect(screen.getAllByText('本地模型文件未就绪').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '测试当前画面识别' })).toBeDisabled()
    expect(requests.mock.calls.filter(([path]) => String(path).endsWith('/profile/build'))).toHaveLength(1)
  })

  it('允许单张上传，上传API只保存照片且不自动建立档案', async () => {
    const { requests } = setup({ references: [] })
    await waitLoaded()
    fireEvent.change(screen.getByLabelText('选择参考照片'), { target: { files: [new File(['unit-image'], 'front.jpg', { type: 'image/jpeg' })] } })
    fireEvent.click(screen.getByRole('button', { name: '上传 1 张照片' }))
    await screen.findByText('本次 1 张照片已保存。请逐张确认目标区域，再建立识别档案。')
    const upload = requests.mock.calls.find(([path, init]) => String(path).endsWith('/reference-images') && init?.method === 'POST')!
    expect(upload[1]?.body).toBeInstanceOf(FormData)
    expect((upload[1]?.body as FormData).getAll('files')).toHaveLength(1)
    expect(requests.mock.calls.some(([path]) => String(path).endsWith('/profile/build'))).toBe(false)
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
  })

  it('抓拍指定当前相机，不启动相机或自动把抓拍当确认参考', async () => {
    const { requests } = setup({ references: [] })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '从当前相机抓拍' }))
    await screen.findByText('当前帧已保存为参考照片，仍需确认目标区域。现场测试请换一个视角。')
    const capture = requests.mock.calls.find(([path]) => String(path).endsWith('/reference-capture'))!
    expect(JSON.parse(String(capture[1]?.body))).toEqual({ camera_id: 'cam-one' })
    expect(screen.getByRole('button', { name: '建立 / 更新识别档案' })).toBeDisabled()
    expect(requests.mock.calls.some(([path]) => /start|events|seed/.test(String(path)))).toBe(false)
  })

  it('测试直接发送camera_id，展示同帧图和拒绝原因；分数不是百分比正确率', async () => {
    const { requests } = setup({ profile: ready })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '测试当前画面识别' }))
    const output = await screen.findByRole('region', { name: '现场识别测试结果' })
    expect(within(output).getByAltText('现场测试的摄像头原始快照')).toHaveAttribute('src', result.image_data_url)
    expect(within(output).getByText('身份未确认')).toBeInTheDocument()
    expect(within(output).getByText(/拒绝原因：相似物品分数差距不足/)).toBeInTheDocument()
    expect(within(output).getByText('照片手机 · 0.920')).toBeInTheDocument()
    expect(within(output).queryByText(/92%/)).not.toBeInTheDocument()
    const request = requests.mock.calls.find(([path]) => String(path).endsWith('/recognition-test'))!
    expect(JSON.parse(String(request[1]?.body))).toEqual({ camera_id: 'cam-one' })
    await waitLoaded()
    fireEvent.change(screen.getByRole('combobox', { name: '用于抓拍和测试的相机' }), { target: { value: 'cam-two' } })
    expect(screen.queryByRole('region', { name: '现场识别测试结果' })).not.toBeInTheDocument()
  })

  it('重测断流后撤下前一次结果，不能用旧图继续宣布识别', async () => {
    const { state } = setup({ profile: ready })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '测试当前画面识别' }))
    await screen.findByRole('region', { name: '现场识别测试结果' })
    await waitLoaded()
    state.testFails = true
    fireEvent.click(screen.getByRole('button', { name: '测试当前画面识别' }))
    await screen.findByText('摄像头断开，当前帧不可用')
    expect(screen.queryByRole('region', { name: '现场识别测试结果' })).not.toBeInTheDocument()
  })

  it('测试结果以本帧来源而非旧相机配置标记来源，回放不会显示为真实相机', async () => {
    setup({ profile: ready, testResult: { ...result, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true } })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '测试当前画面识别' }))
    const output = await screen.findByRole('region', { name: '现场识别测试结果' })
    expect(within(output).getByText('测试视频')).toBeInTheDocument()
    expect(within(output).queryByText('本机 OpenCV 来源')).not.toBeInTheDocument()
    await waitLoaded()
  })

  it('删除单张参考图走明确接口并使旧档案失效，取消确认不发送删除', async () => {
    const { requests } = setup({ profile: { ...ready, loaded_camera_ids: ['cam-one'] } })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '删除参考照片 1' }))
    expect(requests.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '删除参考照片 1' }))
    await screen.findByText('参考照片已删除，请重新建立识别档案。')
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(requests.mock.calls.filter(([, init]) => init?.method === 'DELETE')).toHaveLength(1)
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '删除参考照片 1' })).not.toBeInTheDocument()
  })

  it('照片选择拒绝不支持格式、过大和重复文件，预览地址不随重渲染新建并正确释放', async () => {
    function Picker() { const [files, setFiles] = useState<File[]>([]); return <PhotoFilePicker files={files} onChange={setFiles} /> }
    const { unmount } = render(<Picker />)
    const file = new File(['photo'], 'phone.jpg', { type: 'image/jpeg', lastModified: 1 })
    fireEvent.change(screen.getByLabelText('选择参考照片'), { target: { files: [file, new File(['webp'], 'phone.webp', { type: 'image/webp' })] } })
    expect(screen.getByRole('alert')).toHaveTextContent('仅支持 JPEG、PNG')
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    fireEvent.change(screen.getByLabelText('选择参考照片'), { target: { files: [file] } })
    expect(screen.getByRole('alert')).toHaveTextContent('已经选择')
    const large = new File(['small-content'], 'large.png', { type: 'image/png' })
    Object.defineProperty(large, 'size', { value: 13 * 1024 * 1024 })
    fireEvent.change(screen.getByLabelText('选择参考照片'), { target: { files: [large] } })
    expect(screen.getByRole('alert')).toHaveTextContent('不得超过 12 MB')
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:unit-photo-preview')
  })

  it('正常物品入口允许一张照片保存；上传失败重试不创建第二个物品', async () => {
    let uploadFails = true
    const requests = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input), method = init?.method || 'GET'
      if (path === '/api/items' && method === 'GET') return json([])
      if ((path === '/api/items' && method === 'POST') || (path === `/api/items/${phone.id}` && method === 'PATCH')) return json(phone)
      if (path.endsWith('/reference-images') && method === 'POST') return uploadFails ? json({ detail: '照片内容已损坏，请换一张' }, 422) : json([reference])
      if (path === `/api/items/${phone.id}`) return json({ ...phone, reference_images: [reference] })
      if (path.endsWith('/profile')) return json(saved)
      if (path === '/api/cameras') return json(cameras)
      throw new Error(`Unexpected request ${method} ${path}`)
    })
    vi.stubGlobal('fetch', requests)
    render(<MemoryRouter><ToastProvider><ItemsPage /></ToastProvider></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '添加第一个物品' }))
    fireEvent.change(screen.getByRole('textbox', { name: '物品名称' }), { target: { value: phone.name } })
    fireEvent.change(screen.getByLabelText('选择参考照片'), { target: { files: [new File(['mock-image'], 'one.jpg', { type: 'image/jpeg' })] } })
    fireEvent.click(screen.getByRole('button', { name: '保存物品' }))
    await screen.findByText('照片内容已损坏，请换一张')
    expect(requests.mock.calls.filter(([path, init]) => path === '/api/items' && init?.method === 'POST')).toHaveLength(1)
    uploadFails = false
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    await screen.findByRole('dialog', { name: '照片手机 · 照片与识别' })
    await waitLoaded()
    expect(requests.mock.calls.filter(([path, init]) => path === '/api/items' && init?.method === 'POST')).toHaveLength(1)
    const uploads = requests.mock.calls.filter(([path, init]) => String(path).endsWith('/reference-images') && init?.method === 'POST')
    expect(uploads).toHaveLength(2)
    expect((uploads[1][1]?.body as FormData).getAll('files')).toHaveLength(1)
  })

  it('不可凭未知/零版本或未就绪状态宣布已加载', () => {
    expect(profileIsLoaded({ ...ready, loaded_camera_ids: ['cam-one'] })).toBe(true)
    expect(profileIsLoaded({ ...ready, profile_version: 0, loaded_profile_version: 0, loaded_camera_ids: ['cam-one'] })).toBe(false)
    expect(profileIsLoaded({ ...ready, registration_status: 'failed', loaded_camera_ids: ['cam-one'] })).toBe(false)
    expect(profileIsLoaded({ ...ready, loaded_camera_ids: ['cam-one'], model_runtime: { status: 'unavailable', available: false } })).toBe(false)
    expect(profileIsLoaded({ ...ready, loaded_camera_ids: ['cam-one'], model_runtime: { status: 'ready', available: true, error: 'inference failed' } })).toBe(false)
  })

  it('下载进度来自后台字节记录，自动读到准备完成也不冒充当前服务已加载', async () => {
    const { state, requests } = setup({ profile: { ...saved, model_preparation: { status: 'downloading', downloaded_bytes: 5 * 1024 * 1024, total_bytes: 10 * 1024 * 1024, updated_at: '2026-09-05T13:00:00Z' }, model_runtime: { status: 'not_loaded', available: false } } })
    await waitLoaded()
    expect(screen.getByText('正在下载权重')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: '模型权重下载进度' })).toHaveAttribute('value', String(5 * 1024 * 1024))
    expect(screen.getByRole('progressbar')).toHaveAttribute('max', String(10 * 1024 * 1024))
    expect(screen.getByText('5.0 / 10.0 MB')).toBeInTheDocument()
    state.profile = { ...state.profile, model_preparation: { status: 'ready', downloaded_bytes: 10 * 1024 * 1024, total_bytes: 10 * 1024 * 1024 } }
    await screen.findByText('本地准备已完成', {}, { timeout: 4000 })
    expect(screen.getByText('当前服务尚未加载')).toBeInTheDocument()
    expect(screen.queryByText('当前服务模型已加载')).not.toBeInTheDocument()
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(requests.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })

  it.each([
    ['verifying', '正在校验模型文件'],
    ['loading', '准备器正在加载模型'],
    ['warming', '准备器正在预热模型'],
    ['failed', '模型准备失败'],
  ])('显示独立准备阶段%s，不通过计时伪造进度或服务就绪', async (status, label) => {
    setup({ profile: { ...saved, model_preparation: { status, downloaded_bytes: 200, total_bytes: 100, error: status === 'failed' ? '模型哈希校验失败' : null }, model_runtime: { status: 'not_loaded', available: false } } })
    await waitLoaded()
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText('当前服务尚未加载')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '测试当前画面识别' })).toBeDisabled()
    if (status === 'failed') expect(screen.getByText('模型哈希校验失败')).toBeInTheDocument()
  })

  it('HTTP拒绝建档后读取持久化失败状态，原始失败不被状态刷新吞掉', async () => {
    const { requests } = setup({ references: [{ ...reference, region_confirmed: true }], buildStatus: 422, build: { ...ready, registration_status: 'failed', error: '外观模型文件未通过校验', model_preparation: { status: 'ready' }, model_runtime: { status: 'unavailable', available: false, error_code: 'model_hash_mismatch', error: '当前服务模型不可用：校验失败' } } })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '建立 / 更新识别档案' }))
    await screen.findByText('外观模型文件未通过校验')
    expect(screen.getByText('处理失败 · 请查看原因')).toBeInTheDocument()
    expect(screen.getByText('当前服务模型不可用：校验失败')).toBeInTheDocument()
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
    expect(requests.mock.calls.filter(([path]) => String(path).endsWith('/profile')).length).toBeGreaterThan(1)
  })

  it.each(['upload', 'capture', 'confirm'] as const)('%s已写入但GET失败保留回执，只重试读取不重复写入', async (operation) => {
    const { state, requests } = setup({ references: operation === 'confirm' ? [{ ...reference }] : [] })
    await waitLoaded()
    state.profileFails = true
    if (operation === 'upload') {
      fireEvent.change(screen.getByLabelText('选择参考照片'), { target: { files: [new File(['unit-image'], 'saved.jpg', { type: 'image/jpeg' })] } })
      fireEvent.click(screen.getByRole('button', { name: '上传 1 张照片' }))
    } else if (operation === 'capture') fireEvent.click(screen.getByRole('button', { name: '从当前相机抓拍' }))
    else {
      fireEvent.load(screen.getByAltText('待确认的原始参考照片'))
      fireEvent.click(screen.getByRole('button', { name: '确认这是我的物品' }))
    }
    await screen.findByText(/保存已完成，但状态同步失败/)
    expect(screen.getByText(operation === 'upload' ? '本次 1 张照片已保存。请逐张确认目标区域，再建立识别档案。' : operation === 'capture' ? '当前帧已保存为参考照片，仍需确认目标区域。现场测试请换一个视角。' : '目标区域已确认。请建立或更新识别档案，让新照片参与匹配。')).toBeInTheDocument()
    const writes = () => requests.mock.calls.filter(([, init]) => init?.method === 'POST' || init?.method === 'PATCH')
    expect(writes()).toHaveLength(1)
    expect(screen.queryByRole('button', { name: '上传 1 张照片' })).not.toBeInTheDocument()
    state.profileFails = false
    fireEvent.click(screen.getByRole('button', { name: '重试读取已保存资料' }))
    await waitFor(() => expect(screen.queryByText(/保存已完成，但状态同步失败/)).not.toBeInTheDocument())
    expect(writes()).toHaveLength(1)
  })

  it('ready仍读取真实加载变化，启用不宣称物品识别已验证', async () => {
    vi.useFakeTimers()
    let context!: ReturnType<typeof setup>
    await act(async () => { context = setup({ profile: { ...ready }, references: [{ ...reference, region_confirmed: true }] }) })
    expect(screen.getByRole('button', { name: '刷新状态' })).toBeEnabled()
    context.state.profile = { ...ready, loaded_camera_ids: ['cam-one'] }
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.getByText('已启用当前档案 · 识别效果待现场验证')).toBeInTheDocument()
    expect(screen.queryByText('可识别 · 已加载当前档案')).not.toBeInTheDocument()
    context.state.profile = { ...ready, loaded_camera_ids: [] }
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.queryByText('已启用当前档案 · 识别效果待现场验证')).not.toBeInTheDocument()
    expect(screen.getByText('档案已就绪 · 实时摄像头尚未加载当前版本')).toBeInTheDocument()
    expect(context.requests.mock.calls.filter(([path]) => path === '/api/cameras')).toHaveLength(3)
    expect(context.requests.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  })

  it('现场只有家具候选时明确手机未进入候选，不把其他物品身份接受当手机成功', async () => {
    setup({ profile: ready, testResult: { ...result, candidates: [{ ...result.candidates[0], category: 'bed', best_item_id: 'bed-item', best_item_name: '床', accepted: true }] } })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '测试当前画面识别' }))
    expect(await screen.findByText('本帧没有手机类别候选，尚未进入手机身份匹配；其他家具候选不代表手机已被识别。')).toBeInTheDocument()
    expect(screen.queryByText(/本帧接受了“照片手机”的身份/)).not.toBeInTheDocument()
  })

  it('当前手机单帧身份接受仍明确不是连续观察或位置确认', async () => {
    const { requests } = setup({ profile: ready, testResult: { ...result, candidates: [{ ...result.candidates[0], accepted: true }] } })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '测试当前画面识别' }))
    expect(await screen.findByText('本帧接受了“照片手机”的身份；这不是连续观察或位置更新的确认。')).toBeInTheDocument()
    expect(requests.mock.calls.some(([path]) => /events|start|seed/.test(String(path)))).toBe(false)
  })

  it('参考质量警告不改写已启用档案，也不把警告当上传失败', async () => {
    setup({ profile: { ...ready, loaded_camera_ids: ['cam-one'], quality_warnings: ['参考图尚未检出手机类别候选。'] } })
    await waitLoaded()
    expect(screen.getByLabelText('参考照片质量提示')).toHaveTextContent('参考图尚未检出手机类别候选。')
    expect(screen.getByText('已启用当前档案 · 识别效果待现场验证')).toBeInTheDocument()
    expect(screen.getByText(/这是参考照片的检测提示，不判定照片上传错误/)).toBeInTheDocument()
  })
  it('参考照片的geometry_only建议明确需手动确认，选择不发送确认或建档请求', async () => {
    const { requests } = setup({ references: [{ ...reference, suggested_regions: [{ bbox: [.1, .2, .3, .4], label: 'cell phone', score: .95, category_evidence: 'geometry_only', proposal_backend: 'phone_shape_proposal' }] }] })
    await waitLoaded()
    fireEvent.load(screen.getByAltText('待确认的原始参考照片'))
    fireEvent.click(screen.getByRole('button', { name: '外形建议 1（需手动确认）' }))
    expect(screen.getByText(/外形建议仅表示轮廓像手机，未确认类别和身份/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认这是我的物品' })).toBeEnabled()
    expect(requests.mock.calls.some(([, init]) => init?.method === 'POST' || init?.method === 'PATCH')).toBe(false)
  })
  it.each(['geometry_only', undefined])('现场外形来源在类别证明为%s时仍不能冒充身份匹配', async categoryEvidence => {
    setup({ profile: ready, testResult: { ...result, candidates: [{ ...result.candidates[0], category_evidence: categoryEvidence, geometry_score: .95, proposal_backend: 'phone_shape_proposal', accepted: true, best_score: null }] } })
    await waitLoaded()
    fireEvent.click(screen.getByRole('button', { name: '测试当前画面识别' }))
    expect(await screen.findByText('外形像手机，未确认类别和身份，不更新位置')).toBeInTheDocument()
    expect(screen.queryByText('身份接受', { exact: true })).not.toBeInTheDocument()
    expect(screen.queryByText(/本帧接受了“照片手机”的身份/)).not.toBeInTheDocument()
    expect(screen.getByText(/轮廓分数不是物品匹配分数/)).toBeInTheDocument()
  })
})
