import { FlaskConical, PlayCircle, ScanLine } from 'lucide-react'

export function ModeNotice({ mode, replay = false, compact = false }: { mode?: string; replay?: boolean; compact?: boolean }) {
  if (replay) return <div className={`mode-notice mode-replay ${compact ? 'compact' : ''}`}><PlayCircle /><div><strong>测试视频</strong>{!compact && <span>这是模拟来源；即使经过正式检测算法，也不属于真实家庭摄像头事件。</span>}</div></div>
  if (mode === 'aruco_fallback') return <div className={`mode-notice mode-replay ${compact ? 'compact' : ''}`}><ScanLine /><div><strong>已回退稳定标签模式</strong>{!compact && <span>实验模型当前不可用，画面继续使用真实 ArUco 检测。</span>}</div></div>
  if (mode === 'aruco' || mode?.includes('aruco')) return <div className={`mode-notice mode-stable ${compact ? 'compact' : ''}`}><ScanLine /><div><strong>稳定标签模式</strong>{!compact && <span>用于早期验证和现场稳定演示。</span>}</div></div>
  if (mode === 'experimental') return <div className={`mode-notice mode-experimental ${compact ? 'compact' : ''}`}><FlaskConical /><div><strong>无标签 AI 实验模式</strong>{!compact && <span>相似物品、严重遮挡和低光环境下可能出现误判。</span>}</div></div>
  return <div className={`mode-notice mode-unknown ${compact ? 'compact' : ''}`}><FlaskConical /><div><strong>检测方式未验证</strong>{!compact && <span>后端没有提供可核验的检测器标识，不能推断为 AI 识别。</span>}</div></div>
}
