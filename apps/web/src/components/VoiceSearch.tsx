import { Mic, Square, Volume2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import './VoiceSearch.css'

interface RecognitionResult { isFinal: boolean; 0: { transcript: string } }
interface Recognition {
  lang: string; continuous: boolean; interimResults: boolean; maxAlternatives: number; processLocally?: boolean
  onstart: (() => void) | null; onend: (() => void) | null
  onerror: ((event: { error: string }) => void) | null
  onresult: ((event: { resultIndex: number; results: ArrayLike<RecognitionResult> }) => void) | null
  start: () => void; abort: () => void; stop: () => void
}
interface RecognitionConstructor {
  new (): Recognition
  available?: (options: { langs: string[]; processLocally: boolean }) => Promise<string>
}
type SpeechWindow = Window & { SpeechRecognition?: RecognitionConstructor; webkitSpeechRecognition?: RecognitionConstructor }
type Phase = 'idle' | 'preparing' | 'listening' | 'answering' | 'speaking'
const MAX_QUERY_LENGTH = 200
const MAX_SPOKEN_LENGTH = 2400
const MAX_SPEAKING_MS = 60_000
const errorText: Record<string, string> = {
  'not-allowed': '麦克风权限未允许。请检查浏览器地址栏的权限，也可以直接输入文字。',
  'service-not-allowed': '浏览器没有允许语音识别服务，请直接输入文字。',
  'audio-capture': '没有可用麦克风，或麦克风正被占用。请检查设备后重试。',
  'network': '浏览器语音服务连接失败，未生成查询结果。请改用文字输入。',
  'no-speech': '没有听清这次提问，请再点一次提问或输入文字。',
  'language-not-supported': '当前语音服务不支持中文。可以在下方允许浏览器联网服务，或继续文字输入。',
}

/** Deliberately not a wake-word/background recorder. Every recording starts at
 * an explicit button press; only a final transcript is sent to the search API. */
export function VoiceSearch({ onQuestion, disabled = false, evidenceScope = '' }: { onQuestion: (question: string) => Promise<string | null>; disabled?: boolean; evidenceScope?: string }) {
  const constructor = (window as SpeechWindow).SpeechRecognition || (window as SpeechWindow).webkitSpeechRecognition
  const supported = Boolean(constructor) && window.isSecureContext !== false
  const [phase, setPhase] = useState<Phase>('idle')
  const [allowNetwork, setAllowNetwork] = useState(false)
  const [readAloud, setReadAloud] = useState(true)
  const [transcript, setTranscript] = useState('')
  const [answer, setAnswer] = useState('')
  const [notice, setNotice] = useState('')
  const recognizer = useRef<Recognition | null>(null)
  const utterance = useRef<SpeechSynthesisUtterance | null>(null)
  const generation = useRef(0)
  const timer = useRef<number | undefined>()
  const speechTimer = useRef<number | undefined>()
  const mounted = useRef(true)
  const questionHandler = useRef(onQuestion)
  questionHandler.current = onQuestion

  function clearTimer() { if (timer.current !== undefined) window.clearTimeout(timer.current); timer.current = undefined }
  function releaseRecognition() {
    clearTimer()
    const value = recognizer.current; recognizer.current = null
    if (value) { value.onstart = null; value.onend = null; value.onerror = null; value.onresult = null; try { value.abort() } catch { /* Already stopped by browser. */ } }
  }
  function clearSpeechTimer() { if (speechTimer.current !== undefined) window.clearTimeout(speechTimer.current); speechTimer.current = undefined }
  function releaseSpeech() {
    clearSpeechTimer()
    const value = utterance.current; utterance.current = null
    if (value) { value.onend = null; value.onerror = null; try { window.speechSynthesis?.cancel() } catch { /* Browser service may already be unavailable. */ } }
  }
  function stop() {
    generation.current += 1; releaseRecognition()
    releaseSpeech()
    if (mounted.current) setPhase('idle')
  }
  useEffect(() => {
    mounted.current = true
    const hidden = () => { if (document.hidden) stop() }
    document.addEventListener('visibilitychange', hidden)
    return () => { mounted.current = false; stop(); document.removeEventListener('visibilitychange', hidden) }
    // This lifecycle owns only its own microphone and speech instances.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => { stop(); setAnswer(''); setTranscript(''); setNotice('') }, [evidenceScope]) // eslint-disable-line react-hooks/exhaustive-deps

  function speak(text: string, current = generation.current) {
    if (!mounted.current || current !== generation.current) return
    if (text.length > MAX_SPOKEN_LENGTH) { setNotice('回答较长，本次不自动朗读，请阅读文字并缩小查询范围。'); setPhase('idle'); return }
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) { setNotice('当前浏览器不能朗读，回答已显示为文字。'); setPhase('idle'); return }
    try {
      const voices = window.speechSynthesis.getVoices().filter(voice => /^zh(?:-|_)/i.test(voice.lang))
      const voice = voices.find(value => value.localService) || (allowNetwork ? voices[0] : undefined)
      if (!voice && !allowNetwork) { setNotice('没有可用的本地中文朗读声音。回答已显示为文字；不会自动调用联网声音。'); setPhase('idle'); return }
      releaseSpeech()
      const value = new window.SpeechSynthesisUtterance(text)
      value.lang = 'zh-CN'; value.rate = 1; if (voice) value.voice = voice
      const active = () => current === generation.current && mounted.current && utterance.current === value
      value.onend = () => { if (active()) { clearSpeechTimer(); utterance.current = null; setPhase('idle') } }
      value.onerror = () => { if (active()) { clearSpeechTimer(); utterance.current = null; setPhase('idle'); setNotice('朗读未能播放。回答仍保留在页面，可点击“朗读回答”重试。') } }
      utterance.current = value; setPhase('speaking')
      speechTimer.current = window.setTimeout(() => { if (active()) { releaseSpeech(); setPhase('idle'); setNotice('朗读已达到一分钟上限并停止；完整文字回答仍保留。') } }, MAX_SPEAKING_MS)
      window.speechSynthesis.speak(value)
    } catch { releaseSpeech(); setPhase('idle'); setNotice('朗读服务暂不可用，查询回答仍保留为文字；没有重新录音。') }
  }

  async function start() {
    if (!constructor || !supported || phase !== 'idle' || disabled) return
    stop(); const current = generation.current
    setAnswer(''); setTranscript(''); setNotice(''); setPhase('preparing')
    let local = false
    if (constructor.available && 'processLocally' in constructor.prototype) {
      let probeTimer: number | undefined
      try {
        const availability = await Promise.race([
          constructor.available({ langs: ['zh-CN'], processLocally: true }),
          new Promise<string>(resolve => { probeTimer = window.setTimeout(() => resolve('unavailable'), 2500) }),
        ])
        local = availability === 'available'
      } catch { /* Browser policy or an unavailable language pack; no silent download/fallback. */ }
      finally { if (probeTimer !== undefined) window.clearTimeout(probeTimer) }
    }
    if (!mounted.current || current !== generation.current) return
    if (!local && !allowNetwork) { setPhase('idle'); setNotice('当前浏览器没有可用的本地中文识别。可勾选允许浏览器联网语音后再提问，或直接输入文字。'); return }
    let value: Recognition
    try { value = new constructor() } catch { setPhase('idle'); setNotice('浏览器未能创建语音服务，请直接输入文字。'); return }
    value.lang = 'zh-CN'; value.continuous = false; value.interimResults = true; value.maxAlternatives = 1
    if ('processLocally' in value) value.processLocally = local
    recognizer.current = value
    const active = () => mounted.current && current === generation.current && recognizer.current === value
    value.onstart = () => { if (active()) { setPhase('listening'); setNotice(local ? '正在本地识别中文，不上传语音。' : '正在使用浏览器语音服务，音频可能由浏览器厂商处理。') } }
    value.onerror = event => { if (active()) { releaseRecognition(); setPhase('idle'); setNotice(errorText[event.error] || '这次语音没有完成，请重试或输入文字。') } }
    value.onend = () => { if (active()) { releaseRecognition(); setPhase('idle'); setNotice('本次聆听已结束，没有提交提问。请重试或输入文字。') } }
    value.onresult = event => {
      if (!active()) return
      let pending = '', final = ''
      for (let index = event.resultIndex; index < event.results.length; index++) {
        const result = event.results[index]
        if (result.isFinal) final = (final + result[0].transcript).slice(0, MAX_QUERY_LENGTH + 1)
        else pending = (pending + result[0].transcript).slice(0, MAX_QUERY_LENGTH + 1)
      }
      const question = (final || pending).trim()
      setTranscript(question)
      if (!final.trim()) return
      if (question.length > MAX_QUERY_LENGTH) { releaseRecognition(); setPhase('idle'); setNotice('提问最多 200 字，本次没有截断后提交。请简短说出物品名称和问题。'); return }
      // Release before awaiting the answer: never record the spoken reply.
      releaseRecognition(); setPhase('answering'); setNotice('已停止麦克风，正在查询已保存的位置证据。')
      void questionHandler.current(question).then(text => {
        if (!mounted.current || current !== generation.current) return
        if (!text) { setPhase('idle'); setNotice('本次查询未完成或已被新查询替代，没有生成语音位置。'); return }
        setAnswer(text); setNotice('回答来自这次查询的已保存证据，不会把推测当成确认位置。')
        if (readAloud) speak(text, current); else setPhase('idle')
      }).catch(() => { if (mounted.current && current === generation.current) { setPhase('idle'); setNotice('位置查询失败，没有猜测答案。请用文字查询重试。') } })
    }
    timer.current = window.setTimeout(() => { if (active()) { releaseRecognition(); setPhase('idle'); setNotice('已达到本次 20 秒聆听上限，麦克风已停止。请再次点击提问。') } }, 20_000)
    try { value.start() } catch { releaseRecognition(); setPhase('idle'); setNotice('无法启动麦克风或语音服务，请检查权限后重试。') }
  }

  return <section className="voice-search" aria-label="语音寻找物品">
    <div className="voice-search-actions">
      {phase === 'idle' ? <button className="button secondary" disabled={!supported || disabled} onClick={() => void start()} aria-describedby="voice-privacy"><Mic />点击说出你要找的物品</button>
        : <button className="button secondary" onClick={() => { stop(); setNotice('语音已停止，麦克风不会继续监听。') }}><Square />停止语音</button>}
      <span role="status">{{ idle: '未监听', preparing: '检查语音能力…', listening: '正在聆听', answering: '正在查找', speaking: '正在回答' }[phase]}</span>
      {answer && phase === 'idle' && <button className="button ghost small" onClick={() => speak(answer)}><Volume2 />朗读回答</button>}
    </div>
    <p id="voice-privacy" className="voice-search-privacy">只在你点击后开启麦克风；一句提问结束即关闭，不在物忆保存录音。优先使用浏览器已安装的本地中文识别。联网模式可能将音频及朗读文本交给浏览器厂商的服务，不是完全离线。</p>
    <div className="voice-search-options"><label><input type="checkbox" checked={allowNetwork} disabled={phase !== 'idle'} onChange={event => setAllowNetwork(event.target.checked)} />允许浏览器联网语音（可选）</label><label><input type="checkbox" checked={readAloud} disabled={phase !== 'idle'} onChange={event => setReadAloud(event.target.checked)} />语音提问后朗读回答</label></div>
    {!supported && <p className="voice-search-notice">{window.isSecureContext === false ? '当前地址不是安全连接，麦克风功能不可用。请使用本机 localhost 或已配置的 HTTPS；仍可直接输入查询。' : '当前浏览器不支持语音识别，文字查询仍可使用。'}</p>}
    {transcript && <p className="voice-search-transcript">听到：{transcript}</p>}
    {notice && <p className="voice-search-notice" role="status">{notice}</p>}
    {answer && <p className="voice-search-answer" aria-label="本次语音回答">{answer}</p>}
  </section>
}
