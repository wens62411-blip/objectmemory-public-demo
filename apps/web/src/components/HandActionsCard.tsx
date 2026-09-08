import { Hand } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { formatTime } from '../lib/format'
import type { HandAction, HandActionSnapshot, HandInteraction } from '../types'
import { Badge } from './UI'
import './HandActionsCard.css'

const INTERACTION_LABELS: Record<string, string> = {
  not_established: '尚未建立手物交互',
  nearby: '手在物品附近 · 不代表拿起',
  co_moving: '连续共同移动 · 可能携带',
  holding_uncertain: '持有状态不确定 · 不能判为放下',
  release_candidate: '释放候选 · 正在观察分离后稳定',
  released: '已观测可见分离 · 仍未确认放下',
}
const STATUS_LABELS: Record<string, string> = {
  disabled: '手部分析已关闭', loading: '本地手部模型加载中', ready: '当前帧已分析', no_hands: '当前帧未检测到手',
  failed: '手部分析暂时失败', unavailable: '本地手部模型不可用',
}
const REASON_LABELS: Record<string, string> = { stale_frame: '动作帧已过期，等待新鲜画面', camera_not_running: '摄像头未运行，暂无当前动作', camera_changed: '摄像头来源已变化，等待重新建立动作状态' }
const POSTURE_LABELS: Record<string, string> = { open_palm: '张手', fingers_extended: '四指展开', closed_fist: '握拳', pinch: '捏合', pointing: '指向', unknown: '姿态未知' }
const MOTION_LABELS: Record<string, string> = { still: '基本静止', left: '向左移动', right: '向右移动', up: '向上移动', down: '向下移动', unknown: '运动方向未知' }

function canDisplay(snapshot: HandActionSnapshot | null, cameraId: string) {
  return Boolean(snapshot && snapshot.camera_id === cameraId && snapshot.fresh === true && snapshot.source_session_id
    && Number.isInteger(snapshot.source_frame) && snapshot.source_frame! >= 0 && snapshot.timestamp && Number.isFinite(Date.parse(snapshot.timestamp))
    && snapshot.hand_status?.enabled === true && snapshot.hand_status.available === true && ['ready', 'no_hands'].includes(snapshot.hand_status.status))
}

/** Text shared with the exact-frame overlay. Geometry never establishes touch,
 * gripping, ownership, or a confirmed placement by itself. */
export function HandActionSummary({ hands, interactions }: { hands: HandAction[]; interactions: HandInteraction[] }) {
  return <div className="hand-action-summary">
    {hands.length > 0 && <ul className="hand-postures" aria-label="当前几何手动作">{hands.map((hand, index) => <li key={hand.hand_id}>
      <strong title={`手部轨迹编号：${hand.hand_id}`}>{hand.handedness?.toLowerCase() === 'left' ? '左手' : hand.handedness?.toLowerCase() === 'right' ? '右手' : `手 ${index + 1}`}</strong><span>{hand.stable ? '' : '待稳定：'}{POSTURE_LABELS[hand.posture] || '姿态未知'}</span><small>{MOTION_LABELS[hand.motion] || '运动方向未知'}</small>
      {hand.posture === 'fingers_extended' && <small>四根手指伸展，拇指姿态尚未确认。</small>}
      {!hand.stable && <Badge tone="neutral">候选</Badge>}
    </li>)}</ul>}
    {interactions.length > 0 && <ul className="hand-interactions" aria-label="手与物品的交互线索">{interactions.map((interaction) => <li key={interaction.item_id}>
      <strong>{interaction.item_name || '已登记物品'}</strong><span>{INTERACTION_LABELS[interaction.holding_status] || '交互状态待确认'}</span>
      {interaction.release_observed && <small>分离线索不能单独证明物品已放稳。</small>}
      {interaction.holding_status === 'co_moving' && <small>这是连续运动线索，不是触觉握住证明。</small>}
    </li>)}</ul>}
  </div>
}

export function HandActionsCard({ cameraId, enabled = true, settingsKnown = true, cameraRunning = true }: { cameraId: string; enabled?: boolean; settingsKnown?: boolean; cameraRunning?: boolean }) {
  const [snapshot, setSnapshot] = useState<HandActionSnapshot | null>(null)
  const [message, setMessage] = useState('正在读取当前动作…')
  const [error, setError] = useState('')
  useEffect(() => {
    let disposed = false
    let pollTimer: ReturnType<typeof setTimeout> | undefined
    let expiryTimer: ReturnType<typeof setTimeout> | undefined
    let timeoutTimer: ReturnType<typeof setTimeout> | undefined
    let active: AbortController | undefined
    let lastFrame = ''
    let expiredFrame = ''
    let lastSession = ''
    let lastSequence = -1
    setSnapshot(null); setError('')
    if (!settingsKnown) { setMessage('手部分析设置尚未确认'); return }
    if (!enabled) { setMessage('手部分析已关闭'); return }
    if (!cameraRunning) { setMessage('摄像头未运行，暂无当前动作'); return }

    async function refresh() {
      active = new AbortController()
      const controller = active
      timeoutTimer = setTimeout(() => { controller.abort(); if (!disposed) { expiredFrame = lastFrame; if (expiryTimer) clearTimeout(expiryTimer); setSnapshot(null); setMessage('动作读取超时，旧动作已隐藏') } }, 3000)
      try {
        const value = await api<HandActionSnapshot>(`/api/cameras/${encodeURIComponent(cameraId)}/actions`, { signal: controller.signal })
        if (disposed || controller.signal.aborted) return
        setError('')
        if (!canDisplay(value, cameraId)) {
          expiredFrame = lastFrame
          if (expiryTimer) clearTimeout(expiryTimer)
          setSnapshot(null)
          const status = value.hand_status?.status || ''
          setMessage(['disabled', 'failed', 'unavailable', 'loading'].includes(status) ? STATUS_LABELS[status]
            : REASON_LABELS[value.reason || ''] || (value.fresh !== true ? '动作帧未通过新鲜度检查，旧动作已隐藏' : '动作来源或帧状态待确认'))
          if (value.hand_status?.error) setError(value.hand_status.error)
          return
        }
        const key = `${value.source_session_id}:${value.source_frame}`
        if (lastSession === value.source_session_id && value.source_frame! < lastSequence) { setSnapshot(null); setMessage('来源帧发生倒退，等待新的动作帧'); return }
        if (key === expiredFrame) { setSnapshot(null); setMessage('动作帧已过期，等待新鲜画面'); return }
        if (lastFrame !== key) {
          lastFrame = key
          lastSession = value.source_session_id!; lastSequence = value.source_frame!
          if (expiryTimer) clearTimeout(expiryTimer)
          expiryTimer = setTimeout(() => { expiredFrame = key; if (!disposed) { setSnapshot(null); setMessage('动作帧已过期，等待新鲜画面') } }, 2500)
        }
        setSnapshot(value); setMessage(STATUS_LABELS[value.hand_status.status])
      } catch (value) {
        if (!disposed && !controller.signal.aborted) { expiredFrame = lastFrame; if (expiryTimer) clearTimeout(expiryTimer); setSnapshot(null); setMessage('动作暂时不可用，旧动作已隐藏'); setError(value instanceof Error ? value.message : '连接失败') }
      } finally {
        if (timeoutTimer) clearTimeout(timeoutTimer)
        if (!disposed) pollTimer = setTimeout(refresh, 1000)
      }
    }
    void refresh()
    return () => { disposed = true; active?.abort(); if (pollTimer) clearTimeout(pollTimer); if (expiryTimer) clearTimeout(expiryTimer); if (timeoutTimer) clearTimeout(timeoutTimer) }
  }, [cameraId, enabled, settingsKnown, cameraRunning])
  const current = enabled && settingsKnown && cameraRunning && canDisplay(snapshot, cameraId) ? snapshot : null
  return <section className="panel hand-actions-card" aria-label="当前手部动作">
    <header><div><Hand /><h2>手部动作</h2></div><Badge tone={current ? 'blue' : 'neutral'}>{message}</Badge></header>
    <p className="hand-actions-boundary">本地关键点的几何解释，不等于物品拿起或放下。关闭关键点显示不会停止手部分析。</p>
    {error && <p className="inline-error" role="status">{error}</p>}
    {current && <>
      <HandActionSummary hands={current.hand_status.status === 'no_hands' ? [] : current.hands || []} interactions={current.interactions || []} />
      {!current.hands?.length && <p className="hand-actions-empty">当前没有可显示的手势。未检测到不等于没有手；可能是过小、模糊或遮挡。</p>}
      {current.profile_count === 0 && <p className="hand-actions-empty">尚未注册可识别的物品照片，仍可查看手部反馈。<Link to="/items">注册物品照片</Link>后才可关联具体物品。</p>}
      <small className="hand-actions-time">来源帧 {current.source_frame} · {formatTime(current.timestamp)} · 读取已有分析，不新增推理任务</small>
    </>}
    {!enabled && <Link className="hand-actions-settings" to="/settings">到设置开启本地手部分析</Link>}
  </section>
}
