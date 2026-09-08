import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VoiceSearch } from '../src/components/VoiceSearch'

class Recognition {
  static instances: Recognition[] = []
  static available = vi.fn(async () => 'available')
  lang = ''; continuous = true; interimResults = false; maxAlternatives = 3
  declare processLocally: boolean
  onstart: (() => void) | null = null
  onend: (() => void) | null = null
  onerror: ((event: { error: string }) => void) | null = null
  onresult: ((event: { resultIndex: number; results: Array<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null = null
  start = vi.fn(() => this.onstart?.())
  abort = vi.fn()
  stop = vi.fn()
  constructor() { Recognition.instances.push(this) }
}
class Utterance {
  lang = ''; rate = 1; voice: unknown
  onend: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(public text: string) {}
}
const synth = { speak: vi.fn(), cancel: vi.fn(), getVoices: vi.fn(() => [{ lang: 'zh-CN', localService: true }]) }
const startButton = () => screen.getByRole('button', { name: '点击说出你要找的物品' })
const last = () => Recognition.instances.at(-1)!
const received = (value: Recognition, text: string, isFinal = true) => value.onresult?.({ resultIndex: 0, results: [{ isFinal, 0: { transcript: text } }] })

describe('语音浏览器接口Mock生命周期，不能证明真实麦克风识别', () => {
  beforeEach(() => {
    Recognition.instances = []; Recognition.available = vi.fn(async () => 'available'); Recognition.prototype.processLocally = false
    vi.stubGlobal('SpeechRecognition', Recognition); vi.stubGlobal('SpeechSynthesisUtterance', Utterance); vi.stubGlobal('speechSynthesis', synth); vi.stubGlobal('isSecureContext', true)
    synth.speak.mockReset(); synth.cancel.mockReset(); synth.getVoices.mockReturnValue([{ lang: 'zh-CN', localService: true }])
  })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
  it('打开页面不录音；点击后优先本地，只有最终识别文本触发一次查询并先关闭麦克风', async () => {
    const ask = vi.fn(async () => '手机最后在书房看到，尚未确认放下。')
    render(<VoiceSearch onQuestion={ask} />)
    expect(Recognition.instances).toHaveLength(0)
    expect(Recognition.available).not.toHaveBeenCalled()
    await act(async () => fireEvent.click(startButton()))
    expect(last().processLocally).toBe(true)
    expect(last().continuous).toBe(false)
    await act(async () => received(last(), '手机在', false))
    expect(ask).not.toHaveBeenCalled()
    const callback = last().onresult!
    await act(async () => received(last(), '手机在哪里'))
    expect(last().abort).toHaveBeenCalledOnce()
    expect(ask).toHaveBeenCalledExactlyOnceWith('手机在哪里')
    await act(async () => callback({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: '重复结果' } }] }))
    expect(ask).toHaveBeenCalledOnce()
    expect(synth.speak).toHaveBeenCalledOnce()
    expect(synth.speak.mock.calls[0][0].text).toBe('手机最后在书房看到，尚未确认放下。')
  })
  it('本地不可用时没有偷偷联网；勾选授权后再次点击才启动服务', async () => {
    Recognition.available.mockResolvedValue('unavailable')
    render(<VoiceSearch onQuestion={vi.fn()} />)
    await act(async () => fireEvent.click(startButton()))
    expect(Recognition.instances).toHaveLength(0)
    expect(screen.getByText(/当前浏览器没有可用的本地中文识别/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /允许浏览器联网语音/ }))
    expect(Recognition.instances).toHaveLength(0)
    await act(async () => fireEvent.click(startButton()))
    expect(last().processLocally).toBe(false)
    expect(screen.getByText(/音频可能由浏览器厂商处理/)).toBeInTheDocument()
  })
  it('权限拒绝不给假答案，允许文字fallback且不自动重开录音', async () => {
    const ask = vi.fn(); render(<VoiceSearch onQuestion={ask} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => last().onerror?.({ error: 'not-allowed' }))
    expect(screen.getByText(/麦克风权限未允许/)).toBeInTheDocument()
    expect(last().abort).toHaveBeenCalledOnce(); expect(ask).not.toHaveBeenCalled()
    expect(startButton()).toBeEnabled(); expect(synth.speak).not.toHaveBeenCalled()
  })
  it('20秒超时和卸载后释放录音，不保留后台监听', async () => {
    vi.useFakeTimers()
    const view = render(<VoiceSearch onQuestion={vi.fn()} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(last().abort).toHaveBeenCalledOnce()
    expect(screen.getByText(/20 秒聆听上限/)).toBeInTheDocument()
    await act(async () => fireEvent.click(startButton()))
    const active = last(); view.unmount(); expect(active.abort).toHaveBeenCalledOnce()
    expect(active.onresult).toBeNull()
  })
  it('8千字转写不被静默截断成另一条查询；200字边界正常提交', async () => {
    const ask = vi.fn(async () => '文字回答')
    render(<VoiceSearch onQuestion={ask} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '物'.repeat(8000)))
    expect(ask).not.toHaveBeenCalled(); expect(last().abort).toHaveBeenCalledOnce()
    expect(screen.getByText(/本次没有截断后提交/)).toBeInTheDocument()
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '物'.repeat(200)))
    expect(ask).toHaveBeenCalledExactlyOnceWith('物'.repeat(200))
  })
  it('浏览器不结束朗读时一分钟强制停止，取消服务抛错也能卸载', async () => {
    vi.useFakeTimers()
    const view = render(<VoiceSearch onQuestion={async () => '真实查询答案'} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '手机在哪里'))
    const value = synth.speak.mock.calls[0][0]
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(synth.cancel).toHaveBeenCalledOnce(); expect(value.onend).toBeNull(); expect(value.onerror).toBeNull()
    expect(screen.getByText(/朗读已达到一分钟上限/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '朗读回答' }))
    synth.cancel.mockImplementationOnce(() => { throw new Error('speech service crashed') })
    expect(() => view.unmount()).not.toThrow()
  })
  it('取消的旧本地能力检查晚到，不会终止新一轮录音的超时保护', async () => {
    vi.useFakeTimers(); let finish!: (value: string) => void
    Recognition.available.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    render(<VoiceSearch onQuestion={vi.fn()} />)
    await act(async () => fireEvent.click(startButton()))
    fireEvent.click(screen.getByRole('button', { name: '停止语音' }))
    await act(async () => fireEvent.click(startButton()))
    const current = last()
    await act(async () => finish('available'))
    expect(Recognition.instances).toHaveLength(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(current.abort).toHaveBeenCalledOnce()
  })
  it('意外超长回答保留文字但不把8千字发送给朗读服务', async () => {
    render(<VoiceSearch onQuestion={async () => '物'.repeat(8000)} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '手机在哪里'))
    expect(synth.speak).not.toHaveBeenCalled()
    expect(screen.getByText(/本次不自动朗读/)).toBeInTheDocument()
  })
  it('取消正在查询的回答后，晚到响应不播放；模式变化清空旧回答', async () => {
    let resolve!: (value: string) => void
    const view = render(<VoiceSearch evidenceScope="REAL" onQuestion={() => new Promise(yes => { resolve = yes })} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '手机在哪里'))
    fireEvent.click(screen.getByRole('button', { name: '停止语音' }))
    await act(async () => resolve('旧答案'))
    expect(screen.queryByLabelText('本次语音回答')).not.toBeInTheDocument()
    expect(synth.speak).not.toHaveBeenCalled()
    view.rerender(<VoiceSearch evidenceScope="REAL" onQuestion={async () => '新答案'} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '手机在哪里'))
    expect(screen.getByLabelText('本次语音回答')).toHaveTextContent('新答案')
    view.rerender(<VoiceSearch evidenceScope="UNKNOWN" onQuestion={async () => '新答案'} />)
    expect(screen.queryByLabelText('本次语音回答')).not.toBeInTheDocument()
    expect(synth.cancel).toHaveBeenCalledOnce()
  })
  it('只有网络朗读声音但未授权时保留文字，不隐式联网朗读', async () => {
    synth.getVoices.mockReturnValue([{ lang: 'zh-CN', localService: false }])
    render(<VoiceSearch onQuestion={async () => '有证据的回答'} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '钥匙在哪里'))
    expect(synth.speak).not.toHaveBeenCalled()
    expect(screen.getByLabelText('本次语音回答')).toHaveTextContent('有证据的回答')
    expect(screen.getByText(/不会自动调用联网声音/)).toBeInTheDocument()
  })
  it('朗读声音枚举异常不能把已成功的真实查询说成失败', async () => {
    synth.getVoices.mockImplementationOnce(() => { throw new Error('voice provider unavailable') })
    render(<VoiceSearch onQuestion={async () => '查询已成功'} />)
    await act(async () => fireEvent.click(startButton()))
    await act(async () => received(last(), '手机在哪里'))
    expect(screen.getByLabelText('本次语音回答')).toHaveTextContent('查询已成功')
    expect(screen.getByText(/朗读服务暂不可用/)).toBeInTheDocument()
    expect(screen.queryByText(/位置查询失败/)).not.toBeInTheDocument()
  })
  it('不支持或非安全地址明确说明而非显示假可用按钮', () => {
    vi.stubGlobal('SpeechRecognition', undefined)
    const view = render(<VoiceSearch onQuestion={vi.fn()} />)
    expect(startButton()).toBeDisabled(); expect(screen.getByText(/当前浏览器不支持语音识别/)).toBeInTheDocument()
    vi.stubGlobal('isSecureContext', false); view.rerender(<VoiceSearch onQuestion={vi.fn()} />)
    expect(screen.getByText(/当前地址不是安全连接/)).toBeInTheDocument()
  })
  it('页面后台时立即停止录音，不自动恢复', async () => {
    render(<VoiceSearch onQuestion={vi.fn()} />)
    await act(async () => fireEvent.click(startButton()))
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    fireEvent(document, new Event('visibilitychange'))
    expect(last().abort).toHaveBeenCalledOnce(); expect(startButton()).toBeEnabled()
  })
})
