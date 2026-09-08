import { Badge } from './UI'
import { evidenceBadge, sourceBadge } from '../lib/provenance'
import type { Camera, EventRecord } from '../types'

export function SourceBadge({ record }: { record?: Partial<EventRecord & Camera> | null }) {
  const meta = sourceBadge(record)
  return <Badge tone={meta.tone}>{meta.label}</Badge>
}

export function EventProvenance({ event, compact = false }: { event?: EventRecord | null; compact?: boolean }) {
  if (!event) return null
  const source = sourceBadge(event)
  const evidence = evidenceBadge(event)
  return <span className={`provenance-badges ${compact ? 'compact' : ''}`}>
    <Badge tone={source.tone}>{source.label}</Badge>
    <Badge tone={evidence.tone}>{evidence.label}</Badge>
    {event.pinned && <Badge tone="blue">已固定</Badge>}
  </span>
}
