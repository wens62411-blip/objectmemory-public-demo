/** A remembered object inside a home: the open corner also reads as a bookmark. */
export function BrandMark({ large = false }: { large?: boolean }) {
  return <svg className={`brand-mark ${large ? 'large' : ''}`} viewBox="0 0 48 48" fill="none" aria-hidden="true" focusable="false">
    <rect className="brand-tile" x="1" y="1" width="46" height="46" rx="12" />
    <path d="M12 23 24 13l12 10v11a3 3 0 0 1-3 3H15a3 3 0 0 1-3-3V23Z" stroke="currentColor" strokeWidth="2.2" strokeLinejoin="round" />
    <path d="M18 25h12v9H18z" fill="currentColor" opacity=".18" />
    <path d="M19 25h10v9H19z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    <circle className="brand-memory-dot" cx="35" cy="13" r="4" />
  </svg>
}

export function Brand({ compact = false }: { compact?: boolean }) {
  return <span className={`brand-lockup ${compact ? 'compact' : ''}`}><BrandMark /><span className="brand-wordmark"><strong>物忆</strong>{!compact && <small>让每件物品，有迹可寻</small>}</span></span>
}
