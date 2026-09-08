import { Bell, BellRing, CheckCircle2, ChevronLeft, ShieldCheck, Smartphone, Volume2, VolumeX, Wifi, WifiOff, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, wsUrl } from '../lib/api'
import type { Item } from '../types'
import { useRuntime } from '../contexts/RuntimeContext'
import { RuntimeModeBanner } from '../components/RuntimeModeBanner'
import { Brand } from '../components/Brand'

type Connection = 'disconnected' | 'connecting' | 'online'

export function CompanionPage() {
  const runtime = useRuntime()
  const [params, setParams] = useSearchParams()
  const [items, setItems] = useState<Item[]>([])
  const [itemsLoaded, setItemsLoaded] = useState(false)
  const [itemId, setItemId] = useState(params.get('item') || '')
  const [connection, setConnection] = useState<Connection>('disconnected')
  const [audioEnabled, setAudioEnabled] = useState(false)
  const [ringing, setRinging] = useState(false)
  const [message, setMessage] = useState('选择要绑定的手机')
  const audioEnabledRef = useRef(false)
  const ringingRef = useRef(false)
  const socketRef = useRef<WebSocket | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const oscillatorRef = useRef<OscillatorNode | null>(null)
  const gainRef = useRef<GainNode | null>(null)
  const reconnectRef = useRef<number | null>(null)

  useEffect(() => { api<Item[]>('/api/items').then((value) => { const ringItems = value.filter((item) => item.ring_enabled); setItems(ringItems.length ? ringItems : value); setItemsLoaded(true); if (!itemId && (ringItems[0] || value[0])) setItemId((ringItems[0] || value[0]).id) }).catch(() => setMessage('暂时无法读取物品，请检查物忆主机')) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!itemId) return
    setParams({ item: itemId }, { replace: true })
    let cancelled = false; let attempt = 0
    function connect() {
      if (cancelled) return
      setConnection('connecting')
      const socket = new WebSocket(wsUrl(`/ws/companions/${itemId}`)); socketRef.current = socket
      socket.onopen = () => { attempt = 0; setConnection('online'); setMessage('已连接，等待电脑端响铃指令'); socket.send(JSON.stringify({ type: 'ready', audio_enabled: audioEnabledRef.current })) }
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as { type?: string; action?: string }
          const type = data.type || data.action
          if (type === 'ring') { ringingRef.current = true; setRinging(true); setMessage(audioEnabledRef.current ? '电脑正在寻找这部手机' : '收到响铃指令，请点击启用声音'); if (audioEnabledRef.current) void startSound(); if ('vibrate' in navigator) navigator.vibrate([700, 200, 700, 200, 1100]) }
          if (type === 'stop') { ringingRef.current = false; stopSound(); setRinging(false); setMessage('响铃已停止，继续保持在线') }
          if (type === 'ping') socket.send(JSON.stringify({ type: 'pong' }))
        } catch { /* Ignore malformed messages without disconnecting. */ }
      }
      socket.onclose = () => { if (cancelled) return; setConnection('disconnected'); setMessage('连接断开，正在自动重连…'); reconnectRef.current = window.setTimeout(connect, Math.min(10_000, 1000 * 2 ** attempt++)) }
      socket.onerror = () => socket.close()
    }
    connect()
    return () => { cancelled = true; if (reconnectRef.current) window.clearTimeout(reconnectRef.current); socketRef.current?.close() }
  }, [itemId]) // eslint-disable-line react-hooks/exhaustive-deps

  async function startSound() {
    const context = contextRef.current
    if (!context || oscillatorRef.current) return
    try { await context.resume() } catch { setMessage('浏览器暂停了声音，请再次启用响铃'); audioEnabledRef.current = false; setAudioEnabled(false); return }
    if (context.state !== 'running') { setMessage('浏览器尚未允许声音，请再次启用响铃'); audioEnabledRef.current = false; setAudioEnabled(false); return }
    if (!ringingRef.current || oscillatorRef.current) return
    const oscillator = context.createOscillator(); const gain = context.createGain()
    oscillator.type = 'square'; oscillator.frequency.setValueAtTime(880, context.currentTime)
    oscillator.frequency.exponentialRampToValueAtTime(1320, context.currentTime + 0.35)
    gain.gain.setValueAtTime(0.001, context.currentTime); gain.gain.exponentialRampToValueAtTime(0.23, context.currentTime + 0.03)
    oscillator.connect(gain); gain.connect(context.destination); oscillator.start(); oscillatorRef.current = oscillator; gainRef.current = gain
  }
  function stopSound() {
    try { oscillatorRef.current?.stop() } catch { /* Already stopped. */ }
    oscillatorRef.current?.disconnect(); gainRef.current?.disconnect(); oscillatorRef.current = null; gainRef.current = null
    if ('vibrate' in navigator) navigator.vibrate(0)
  }
  async function enableAudio() {
    try {
      const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
      if (!AudioContextClass) { setMessage('这个浏览器不支持网页响铃，请使用新版 Edge、Chrome 或 Safari'); return }
      if (!contextRef.current) contextRef.current = new AudioContextClass()
      const context = contextRef.current
      await context.resume()
      if (context.state !== 'running') { setMessage('浏览器尚未允许声音，请再次点击启用响铃'); return }
      const oscillator = context.createOscillator(); const gain = context.createGain(); gain.gain.value = 0.08; oscillator.frequency.value = 660; oscillator.connect(gain); gain.connect(context.destination); oscillator.start(); oscillator.stop(context.currentTime + 0.12)
      oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
      audioEnabledRef.current = true; setAudioEnabled(true); setMessage('声音已启用，等待电脑端响铃指令')
      if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'audio_enabled' }))
      if (ringingRef.current) window.setTimeout(() => void startSound(), 150)
    } catch { audioEnabledRef.current = false; setAudioEnabled(false); setMessage('浏览器没有允许声音，请检查音频权限后再试') }
  }
  async function stop() {
    ringingRef.current = false; stopSound(); setRinging(false); setMessage('响铃已停止，继续保持在线')
    if (itemId) { try { await api(`/api/items/${itemId}/ring/stop`, { method: 'POST' }) } catch { /* Local stop remains available offline. */ } }
  }
  useEffect(() => () => { ringingRef.current = false; stopSound(); void contextRef.current?.close() }, [])

  const selected = items.find((item) => item.id === itemId)
  return (
    <main className={`companion-page ${ringing ? 'is-ringing' : ''}`}>
      <RuntimeModeBanner mode={runtime.mode} showReal />
      <header className="companion-header"><Link to="/"><ChevronLeft />查看物品</Link><Brand compact /><span className={`companion-online ${connection}`}><span />{connection === 'online' ? '在线' : connection === 'connecting' ? '连接中' : '离线'}</span></header>
      <section className="companion-hero">
        <div className="phone-rings"><span /><span /><div className="phone-icon">{ringing ? <BellRing /> : <Smartphone />}</div></div>
        <p className="eyebrow">{runtime.mode === 'DEMO' ? 'DEMO 手机伴侣' : runtime.mode === 'TEST' ? 'TEST 手机伴侣' : runtime.mode === 'REAL' ? '本地手机伴侣' : '运行模式未验证'}</p><h1>{ringing ? '我在这里！' : '让这部手机能被找到'}</h1><p>{message}</p>
        <label className="companion-select"><span>绑定为</span><select value={itemId} onChange={(event) => setItemId(event.target.value)} disabled={ringing}><option value="">选择物品</option>{items.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
        {itemsLoaded && !items.length && <p className="companion-empty-note">还没有可绑定的物品。请先在<Link to="/items">物品管理</Link>添加这部手机，再回来启用响铃。</p>}
        {!audioEnabled ? <button className="companion-button enable" disabled={!selected} onClick={() => void enableAudio()}><Volume2 /><span><strong>启用响铃</strong><small>需要你首次点击来允许浏览器播放声音</small></span></button> : ringing ? <button className="companion-button stop" onClick={() => void stop()}><X /><span><strong>停止响铃</strong><small>电脑端也会同步停止</small></span></button> : <div className="companion-ready"><CheckCircle2 /><div><strong>声音已启用</strong><span>请保持这个页面打开</span></div></div>}
      </section>
      <section className="companion-status"><div>{connection === 'online' ? <Wifi /> : <WifiOff />}<span><strong>主机连接</strong>{connection === 'online' ? '已连接' : !itemId ? '先选择一件物品' : '自动重连中'}</span></div><div>{audioEnabled ? <Bell /> : <VolumeX />}<span><strong>声音权限</strong>{audioEnabled ? '已允许' : '需要点击'}</span></div><div><ShieldCheck /><span><strong>隐私</strong>无视频上传</span></div></section>
      <p className="companion-disclaimer">这是本地浏览器伴侣功能。浏览器完全关闭或被系统挂起后，不承诺仍能在后台响铃。</p>
    </main>
  )
}
