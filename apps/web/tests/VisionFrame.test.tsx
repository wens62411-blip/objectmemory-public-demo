import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VisionFrame } from '../src/components/VisionFrame'
import type { RecognitionTest } from '../src/types'

const fixture = {
  source_session_id: 'mock-frame-session', source_frame: 20, source_timestamp: '2026-09-06T00:00:00Z', runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false,
  image_data_url: 'data:image/jpeg;base64,AA', width: 640, height: 480, hands: [],
  candidates: [
    { category: 'bed', bbox: [.1, .1, .8, .8], accepted: false, best_item_id: null, best_item_name: null },
    { category: 'person', bbox: [.1, .1, .3, .5], accepted: false, best_item_id: null, best_item_name: null },
    { category: 'cell phone', bbox: [.2, .3, .2, .3], accepted: false, best_item_id: null, best_item_name: null },
  ] as RecognitionTest['candidates'],
}
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const advance = async (milliseconds = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds) }) }

describe('同帧识别展示Mock合同，不证明真实物品检测', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('接收、物品和手部计数分别来自当前模型帧，person不冒充物品身份', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...fixture, pipeline_diagnostics: {
      source_session_id: fixture.source_session_id, source_frame: fixture.source_frame, profile_generation: 3,
      capture_frames_received: 50, processed_frames: 20, object_model_frames: 19, hand_model_frames: 8,
      loaded_profile_count: 1, loaded_profile_versions: { phone: 7 }, raw_detection_count: 1, raw_object_count: 0,
      candidate_count: 0, identity_accepted_count: 0, identity_rejected_count: 0, stage_errors: {}, stage_timings_ms: {},
    } })))
    render(<VisionFrame cameraId="cam" continuous={false} registeredItems={[{ id: 'phone', name: '登记手机', type: 'phone', aliases: [], aruco_id: null, ring_enabled: false }]} />); await advance()
    const panel = screen.getByRole('region', { name: '物品识别链路诊断' })
    expect(within(panel).getByText('物品模型帧').parentElement).toHaveTextContent('19')
    expect(within(panel).getByText('手部模型帧').parentElement).toHaveTextContent('8')
    expect(within(panel).getByText('其中物品候选（不含人）').parentElement).toHaveTextContent('0')
    expect(within(panel).getByText('本帧身份接受').parentElement).toHaveTextContent('0')
    expect(panel).toHaveTextContent('登记手机 v7')
  })

  it('没有诊断字段不伪造零，错帧计数不得放行当前帧', async () => {
    let data: unknown = fixture
    vi.stubGlobal('fetch', vi.fn(async () => json(data)))
    render(<VisionFrame cameraId="cam" continuous={false} />); await advance()
    expect(screen.getByText(/后端尚未回报物品管线计数/)).toBeInTheDocument()
    data = { ...fixture, source_frame: 21, pipeline_diagnostics: { source_session_id: 'old', source_frame: 20, loaded_profile_count: 99 } }
    await advance(500)
    expect(screen.getByRole('alert')).toHaveTextContent('诊断计数与当前模型帧来源不一致')
    expect(screen.queryByRole('region', { name: '物品识别链路诊断' })).not.toBeInTheDocument()
  })

  it('实际解码尺寸不同于坐标帧元数据时撤下画面和全部框', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(fixture)))
    const view = render(<VisionFrame cameraId="cam" continuous={false} />); await advance()
    const image = screen.getByAltText('同帧识别原图')
    Object.defineProperties(image, { naturalWidth: { value: 1280 }, naturalHeight: { value: 720 } })
    fireEvent.load(image)
    expect(screen.getByRole('alert')).toHaveTextContent('原图实际尺寸与识别坐标不一致')
    expect(view.container.querySelectorAll('rect')).toHaveLength(0)
    expect(screen.queryByAltText('同帧识别原图')).not.toBeInTheDocument()
  })

  it('默认只画手机等优先候选，已匹配与候选未确认文字不同，不铺满人或床', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(fixture)))
    const view = render(<VisionFrame cameraId="cam" />); await advance()
    expect(view.container.querySelectorAll('rect')).toHaveLength(0)
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    expect(screen.getByLabelText('候选手机（身份未确认）')).toBeInTheDocument()
    expect(screen.queryByLabelText('候选床（身份未确认）')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('候选人（身份未确认）')).not.toBeInTheDocument()
    expect(view.container.querySelectorAll('rect')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '查看其他候选（本帧共 3 个）' }))
    expect(view.container.querySelectorAll('rect')).toHaveLength(3)
  })

  it('新图未完成解码时不把新框贴在旧图上，解码后才显示同帧坐标', async () => {
    let value = { ...fixture }
    vi.stubGlobal('fetch', vi.fn(async () => json(value)))
    const view = render(<VisionFrame cameraId="cam" />); await advance()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    expect(view.container.querySelector('rect')).toHaveAttribute('x', '128')
    value = { ...fixture, source_frame: 21, image_data_url: 'data:image/jpeg;base64,BB', candidates: [{ ...fixture.candidates[2], bbox: [.6, .3, .2, .3], accepted: true, best_item_id: 'my-phone', best_item_name: '我的手机' }] }
    await advance(500)
    expect(view.container.querySelector('rect')).toHaveAttribute('x', '128')
    expect(screen.getByAltText('同帧识别原图')).toHaveAttribute('data-source-frame', '20')
    fireEvent.load(screen.getByAltText('等待解码的新帧'))
    expect(view.container.querySelector('rect')).toHaveAttribute('x', '384')
    expect(screen.getByLabelText('已匹配 我的手机')).toBeInTheDocument()
    expect(screen.getByText(/身份匹配不等于连续观察或确认放下/)).toBeInTheDocument()
  })

  it('延迟解码期间保留上一张图和对应旧框，不能逐帧隐藏整个画面造成闪烁', async () => {
    let value = { ...fixture }
    vi.stubGlobal('fetch', vi.fn(async () => json(value)))
    const view = render(<VisionFrame cameraId="cam" continuous={false} />); await advance()
    const previous = screen.getByAltText('同帧识别原图')
    fireEvent.load(previous)
    value = { ...fixture, source_frame: 21, image_data_url: 'data:image/jpeg;base64,BB', candidates: [{ ...fixture.candidates[2], bbox: [.6, .3, .2, .3] }] }
    await advance(500)
    expect(previous).toBeInTheDocument()
    expect(previous).toHaveStyle({ visibility: 'visible' })
    expect(previous).toHaveAttribute('data-source-frame', '20')
    expect(view.container.querySelector('rect')).toHaveAttribute('x', '128')
    const pending = screen.getByAltText('等待解码的新帧')
    expect(pending).toHaveStyle({ visibility: 'hidden' })
    expect(pending).toHaveAttribute('data-source-frame', '21')
    fireEvent.load(pending)
    expect(screen.getByAltText('同帧识别原图')).toHaveAttribute('data-source-frame', '21')
    expect(view.container.querySelector('rect')).toHaveAttribute('x', '384')
    expect(previous).toHaveStyle({ visibility: 'hidden' })
  })

  it('持续切帧只复用两个图像节点', async () => {
    let value = { ...fixture }
    vi.stubGlobal('fetch', vi.fn(async () => json(value)))
    const view = render(<VisionFrame cameraId="cam" continuous={false} />); await advance()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    const nodes = new Set<Element>(view.container.querySelectorAll('.vision-frame-image > img'))
    for (let sequence = 21; sequence < 27; sequence++) {
      value = { ...fixture, source_frame: sequence, image_data_url: `data:image/jpeg;base64,test-${sequence}` }
      await advance(500)
      const pending = screen.getByAltText('等待解码的新帧')
      fireEvent.load(pending)
      view.container.querySelectorAll('.vision-frame-image > img').forEach(node => nodes.add(node))
      expect(screen.getByAltText('同帧识别原图')).toHaveAttribute('data-source-frame', String(sequence))
    }
    expect(nodes.size).toBe(2)
  })

  it('切换来源会话立即撤下旧图和框，新会话解码后才重新展示', async () => {
    let value = { ...fixture }
    vi.stubGlobal('fetch', vi.fn(async () => json(value)))
    const view = render(<VisionFrame cameraId="cam" continuous={false} />); await advance()
    const old = screen.getByAltText('同帧识别原图')
    fireEvent.load(old)
    value = { ...fixture, source_session_id: 'new-source', source_frame: 1, image_data_url: 'data:image/jpeg;base64,CC' }
    await advance(500)
    expect(view.container.querySelector('rect')).not.toBeInTheDocument()
    const fresh = screen.getByAltText('同帧识别原图')
    expect(fresh).toHaveAttribute('data-source-session', 'new-source')
    expect(fresh).toHaveStyle({ visibility: 'hidden' })
    fireEvent.load(fresh)
    expect(fresh).toHaveStyle({ visibility: 'visible' })
    expect(view.container.querySelector('rect')).toHaveAttribute('x', '128')
  })

  it('重复帧超过2.5秒清空图与框，断开后没有后台读取', async () => {
    const requests = vi.fn(async () => json(fixture)); vi.stubGlobal('fetch', requests)
    const view = render(<VisionFrame cameraId="cam" />); await advance()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    await advance(2501)
    expect(screen.queryByAltText('同帧识别原图')).not.toBeInTheDocument()
    expect(view.container.querySelector('rect')).not.toBeInTheDocument()
    expect(screen.getByText('识别帧已过期，等待新鲜画面')).toBeInTheDocument()
    view.rerender(<VisionFrame cameraId="cam" active={false} />)
    const count = requests.mock.calls.length
    await advance(5000)
    expect(requests).toHaveBeenCalledTimes(count)
    expect(screen.getByText('摄像头尚未运行，当前没有物品识别帧。')).toBeInTheDocument()
  })

  it('隐藏识别框不关闭手部关键点或手部分析', async () => {
    const requests = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => json({ ...fixture, hands: [{ landmarks: [[.1, .2], [.2, .3]] }] })); vi.stubGlobal('fetch', requests)
    const view = render(<VisionFrame cameraId="cam" showHands />); await advance()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    fireEvent.click(screen.getByRole('checkbox', { name: '显示识别框' }))
    expect(view.container.querySelectorAll('rect')).toHaveLength(0)
    expect(view.container.querySelectorAll('circle')).toHaveLength(2)
    expect(requests.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })
  it.each(['geometry_only', undefined])('外形来源即使类别证明为%s也不能借accepted字段显示身份接受', async categoryEvidence => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...fixture, candidates: [{ ...fixture.candidates[2], proposal_backend: 'phone_shape_proposal', category_evidence: categoryEvidence, geometry_score: .94, accepted: true, best_item_id: 'phone', best_item_name: '我的手机', best_score: null }] })))
    const view = render(<VisionFrame cameraId="cam" />); await advance()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    fireEvent.click(screen.getByRole('button', { name: '查看其他候选（本帧共 1 个）' }))
    expect(screen.getByLabelText('外形像手机，未确认类别和身份，不更新位置')).toBeInTheDocument()
    expect(view.container.querySelector('rect')).toHaveAttribute('stroke', '#f4b74a')
    expect(screen.queryByLabelText('已匹配 我的手机')).not.toBeInTheDocument()
    expect(screen.getByText(/仅为轮廓外形建议/)).toBeInTheDocument()
  })
  it.each([true, false])('主画面=%s：纯轮廓建议默认不画成手机，主动展开其他候选后才展示', async primary => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...fixture, candidates: [{ ...fixture.candidates[2], proposal_backend: 'phone_shape_proposal', category_evidence: 'geometry_only', geometry_score: .94 }] })))
    const view = render(<VisionFrame cameraId="cam" continuous={false} primary={primary} />); await advance()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    expect(view.container.querySelectorAll('rect')).toHaveLength(0)
    expect(screen.queryByLabelText('外形像手机，未确认类别和身份，不更新位置')).not.toBeInTheDocument()
    expect(screen.getByText('本帧没有模型确认的相关候选；这不代表物品一定不在画面中。')).toBeInTheDocument()
    expect(screen.getByText('另有轮廓建议，未通过物品检测；仅在“查看其他候选”中展示，不更新位置。')).toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: '查看其他候选（本帧共 1 个）' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(screen.getByLabelText('外形像手机，未确认类别和身份，不更新位置')).toBeInTheDocument()
    expect(view.container.querySelectorAll('rect')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '收起其他候选' }))
    expect(view.container.querySelectorAll('rect')).toHaveLength(0)
  })
  it('隐藏轮廓建议不隐藏真实模型手机候选和已匹配注册物品', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...fixture, candidates: [
      { ...fixture.candidates[2], proposal_backend: 'phone_shape_proposal', category_evidence: 'geometry_only', accepted: true, best_item_id: 'spoofed-shape', best_item_name: '错误外形' },
      { ...fixture.candidates[2], proposal_backend: 'opencv_zoo_nanodet_experimental', category_evidence: 'model' },
      { ...fixture.candidates[2], bbox: [.6,.3,.2,.3], accepted: true, best_item_id: 'registered-phone', best_item_name: '我的手机', proposal_backend: 'opencv_zoo_nanodet_experimental', category_evidence: 'model' },
    ] })))
    const onSelectItem = vi.fn()
    const view = render(<VisionFrame cameraId="cam" continuous={false} onSelectItem={onSelectItem} />); await advance()
    fireEvent.load(screen.getByAltText('同帧识别原图'))
    expect(view.container.querySelectorAll('rect')).toHaveLength(2)
    expect(screen.getByLabelText('候选手机（身份未确认）')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('已匹配 我的手机'))
    expect(onSelectItem).toHaveBeenCalledWith('registered-phone')
    expect(screen.queryByLabelText('已匹配 错误外形')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看其他候选（本帧共 3 个）' }))
    fireEvent.click(screen.getByLabelText('外形像手机，未确认类别和身份，不更新位置'))
    expect(onSelectItem).toHaveBeenCalledTimes(1)
  })
  it('模型拒绝原因用中文解释而不把内部代码当用户文案', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...fixture, candidates: [{ ...fixture.candidates[2], rejection_reason: 'below_threshold' }] })))
    render(<VisionFrame cameraId="cam" />); await advance()
    expect(screen.getByText('外观相似度不足，尚不能确认是你的物品。')).toBeInTheDocument()
    expect(screen.queryByText('below_threshold')).not.toBeInTheDocument()
  })
})
