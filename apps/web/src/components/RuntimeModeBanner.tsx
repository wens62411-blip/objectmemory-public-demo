import { ShieldCheck, TriangleAlert } from 'lucide-react'
import type { ResolvedRuntimeMode } from '../types'
import { runtimeModeText } from '../lib/provenance'

export function RuntimeModeBanner({ mode, showReal = false }: { mode: ResolvedRuntimeMode; showReal?: boolean }) {
  if (mode === 'REAL' && !showReal) return null
  const Icon = mode === 'REAL' ? ShieldCheck : TriangleAlert
  return (
    <div className={`runtime-mode-banner runtime-${mode.toLowerCase()}`} role="status" aria-live="polite">
      <Icon />
      <strong>{mode === 'REAL' ? 'REAL · 真实模式' : runtimeModeText(mode)}</strong>
    </div>
  )
}
