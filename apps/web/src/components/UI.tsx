import { AlertCircle, ArrowRight, LoaderCircle, Search, WifiOff } from 'lucide-react'
import type { FormEvent, ReactNode } from 'react'

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'success' | 'warning' | 'danger' | 'neutral' | 'blue' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

export function Loading({ label = '正在读取…' }: { label?: string }) {
  return <div className="state-block"><LoaderCircle className="spin" /><span>{label}</span></div>
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-block error-state">
      <WifiOff /><strong>暂时没有连上</strong><span>{message}</span>
      {onRetry && <button className="button secondary small" onClick={onRetry}>再试一次</button>}
    </div>
  )
}

export function EmptyState({ icon, title, description, action }: { icon?: ReactNode; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state">{icon || <AlertCircle />}<h3>{title}</h3><p>{description}</p>{action}</div>
}

export function SearchBox({ value, onChange, onSubmit, loading, placeholder = '我的手机在哪里？', compact = false }: {
  value: string; onChange: (value: string) => void; onSubmit: () => void; loading?: boolean; placeholder?: string; compact?: boolean
}) {
  function submit(event: FormEvent) { event.preventDefault(); onSubmit() }
  return (
    <form className={`search-box ${compact ? 'compact-search' : ''}`} onSubmit={submit}>
      <Search aria-hidden="true" />
      <input aria-label="寻找物品" value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
      <button className="search-submit" type="submit" disabled={loading || !value.trim()}>
        {loading ? <LoaderCircle className="spin" /> : <><span>帮我找</span><ArrowRight /></>}
      </button>
    </form>
  )
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T; options: Array<{ value: T; label: string }>; onChange: (value: T) => void; label: string
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => <button type="button" className={value === option.value ? 'active' : ''} onClick={() => onChange(option.value)} key={option.value}>{option.label}</button>)}
    </div>
  )
}

export function Modal({ title, description, children, onClose, wide = false }: { title: string; description?: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <header><div><h2 id="modal-title">{title}</h2>{description && <p>{description}</p>}</div><button className="icon-button close-button" onClick={onClose} aria-label="关闭">×</button></header>
        {children}
      </section>
    </div>
  )
}

export function Field({ label, hint, error, children, className = '' }: { label: string; hint?: string; error?: string; children: ReactNode; className?: string }) {
  return <label className={`field ${className}`}><span className="field-label">{label}</span>{children}{hint && <small>{hint}</small>}{error && <small className="field-error">{error}</small>}</label>
}
