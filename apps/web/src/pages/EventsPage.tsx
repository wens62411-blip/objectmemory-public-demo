import { CheckCircle2, Clock3, Filter, ImageOff, MapPin, PencilLine, Pin, PinOff, PlayCircle, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Badge, EmptyState, ErrorState, Field, Loading, Modal, PageHeader } from '../components/UI'
import { ModeNotice } from '../components/ModeNotice'
import { useApi } from '../hooks/useApi'
import { api, mediaUrl, queryString } from '../lib/api'
import { confidenceText, eventLabel, formatTime } from '../lib/format'
import type { Camera, EventRecord, EventType, Item } from '../types'
import { useToast } from '../components/Toast'
import { EventProvenance } from '../components/ProvenanceBadge'
import { evidenceBadge } from '../lib/provenance'

const eventTypes: Array<{ value: EventType | ''; label: string }> = [{ value: '', label: '全部事件' }, { value: 'movement', label: '位置变化' }, { value: 'placed', label: '确认放下（旧数据）' }, { value: 'picked_up', label: '被拿起（旧数据）' }, { value: 'moved', label: '移动中（旧数据）' }, { value: 'seen', label: '看到' }, { value: 'occluded', label: '被遮挡' }, { value: 'exited_view', label: '离开画面' }, { value: 'reappeared', label: '重新出现' }, { value: 'manual_correction', label: '人工纠正' }]

export function EventsPage() {
  const [params, setParams] = useSearchParams()
  const itemId = params.get('item_id') || ''
  const cameraId = params.get('camera_id') || ''
  const roomName = params.get('room_name') || ''
  const eventType = params.get('event_type') || ''
  const path = useMemo(() => `/api/events${queryString({ item_id: itemId, camera_id: cameraId, room_name: roomName, event_type: eventType, limit: 100 })}`, [itemId, cameraId, roomName, eventType])
  const events = useApi<EventRecord[]>(path, [])
  const items = useApi<Item[]>('/api/items', [])
  const cameras = useApi<Camera[]>('/api/cameras', [])
  const [correcting, setCorrecting] = useState<EventRecord | null>(null)
  const [correction, setCorrection] = useState({ room_name: '', zone_name: '', notes: '' })
  const [busy, setBusy] = useState(false)
  const { notify } = useToast()

  function update(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value); else next.delete(key)
    setParams(next)
  }
  function openCorrection(event: EventRecord) { setCorrecting(event); setCorrection({ room_name: event.room_name || '', zone_name: event.zone_name || event.new_zone || '', notes: '' }) }
  async function correct(event: React.FormEvent) {
    event.preventDefault(); if (!correcting) return; setBusy(true)
    try { await api(`/api/events/${correcting.id}/correct`, { method: 'POST', json: correction }); notify('人工纠正已作为新证据保存', 'success'); setCorrecting(null); await events.reload() }
    catch (value) { notify(value instanceof Error ? value.message : '纠正保存失败', 'danger') }
    finally { setBusy(false) }
  }
  async function remove(event: EventRecord) {
    if (!window.confirm('删除这条事件记录及其关联媒体？')) return
    try { await api(`/api/events/${event.id}`, { method: 'DELETE' }); notify('事件已删除', 'success'); await events.reload() }
    catch (value) { notify(value instanceof Error ? value.message : '删除失败', 'danger') }
  }
  async function togglePinned(event: EventRecord) {
    try {
      await api(`/api/events/${event.id}/pin`, { method: 'POST', json: { pinned: !event.pinned } })
      notify(event.pinned ? '已取消固定，事件将重新遵循保留策略' : '事件已固定，不会被自动清理', 'success')
      await events.reload()
    } catch (value) { notify(value instanceof Error ? value.message : '固定状态更新失败', 'danger') }
  }

  return (
    <div>
      <PageHeader title="每一次移动，都有证据可回看" description="按物品、摄像头、房间或事件类型筛选；纠正会新增一条人工证据。" />
      <section className="filter-bar panel"><div className="filter-title"><Filter /><span>筛选</span></div><label><span>物品</span><select aria-label="按物品筛选" value={itemId} onChange={(event) => update('item_id', event.target.value)}><option value="">全部物品</option>{items.data.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label><span>摄像头</span><select aria-label="按摄像头筛选" value={cameraId} onChange={(event) => update('camera_id', event.target.value)}><option value="">全部摄像头</option>{cameras.data.map((camera) => <option key={camera.id} value={camera.id}>{camera.name}</option>)}</select></label><label><span>房间</span><select aria-label="按房间筛选" value={roomName} onChange={(event) => update('room_name', event.target.value)}><option value="">全部房间</option>{Array.from(new Set(cameras.data.map((camera) => camera.room_name))).filter(Boolean).map((room) => <option key={room}>{room}</option>)}</select></label><label><span>事件</span><select aria-label="按事件类型筛选" value={eventType} onChange={(event) => update('event_type', event.target.value)}>{eventTypes.map((type) => <option value={type.value} key={type.value}>{type.label}</option>)}</select></label>{params.size > 0 && <button className="button ghost small" onClick={() => setParams({})}>清除筛选</button>}</section>

      {events.loading ? <Loading label="正在读取事件…" /> : events.error ? <ErrorState message={events.error} onRetry={events.reload} /> : events.data.length === 0 ? <EmptyState icon={<Clock3 />} title="这个范围内还没有事件" description="暂时没有满足证据门的事件记录。" /> : <div className="timeline">{events.data.map((event, index) => {
        const evidence = evidenceBadge(event)
        return <article className="timeline-event" key={event.id}>
          <div className="timeline-rail"><div className={`timeline-dot evidence-${evidence.tone}`}>{evidence.tone === 'success' || evidence.tone === 'blue' ? <CheckCircle2 /> : <Clock3 />}</div>{index < events.data.length - 1 && <span />}</div>
          <div className="event-card panel">
            <div className="event-screenshot">{event.screenshot_path ? <img loading={index < 2 ? undefined : "lazy"} decoding="async" src={mediaUrl(event.screenshot_path)} alt={`${event.item_name}${eventLabel(event)}截图`} /> : <div><ImageOff /><span>没有保存截图</span></div>}<Badge tone={evidence.tone}>{evidence.label}</Badge></div>
            <div className="event-content"><div className="event-card-head"><div><p className="eyebrow">{formatTime(event.timestamp_start)}</p><h2>{event.item_name} · {eventLabel(event)}</h2></div><EventProvenance event={event} /></div>
              <div className="event-location"><MapPin /><div><strong>{[event.room_name, event.to_zone || event.zone_name || event.new_zone].filter(Boolean).join(' · ') || '位置未定义'}</strong><span>{event.camera_name}</span></div></div>
              {(event.from_zone || event.previous_zone) && (event.to_zone || event.new_zone) && <div className="movement-line"><span>{event.from_zone || event.previous_zone}</span><strong>→</strong><span>{event.to_zone || event.new_zone}</span></div>}
              <div className="event-details"><span>可信度 {confidenceText(event.confidence)}</span>{event.notes && <span>备注：{event.notes}</span>}</div>
              {event.event_type === 'occluded' && <p className="evidence-warning">最后在这里出现，随后被遮挡；系统没有断言它仍在原处。</p>}
              <ModeNotice mode={event.detection_mode} compact />
              <div className="event-actions">{event.clip_path && <a className="button secondary small" href={mediaUrl(event.clip_path)} target="_blank" rel="noreferrer"><PlayCircle />播放录像</a>}<button className="button ghost small" onClick={() => void togglePinned(event)}>{event.pinned ? <PinOff /> : <Pin />}{event.pinned ? '取消固定' : '固定事件'}</button><button className="button ghost small" onClick={() => openCorrection(event)}><PencilLine />纠正位置</button><button className="icon-button danger-icon" onClick={() => remove(event)} title="删除事件"><Trash2 /></button></div>
            </div>
          </div>
        </article>
      })}</div>}

      {correcting && <Modal title="纠正物品位置" description={`为“${correcting.item_name}”添加一条人工确认记录，原事件仍保留。`} onClose={() => setCorrecting(null)}><form className="modal-form" onSubmit={correct}><Field label="房间"><input value={correction.room_name} onChange={(event) => setCorrection((value) => ({ ...value, room_name: event.target.value }))} required /></Field><Field label="区域"><input value={correction.zone_name} onChange={(event) => setCorrection((value) => ({ ...value, zone_name: event.target.value }))} required /></Field><Field label="备注"><textarea rows={3} value={correction.notes} onChange={(event) => setCorrection((value) => ({ ...value, notes: event.target.value }))} placeholder="例如：我刚刚在沙发右侧确认看到" /></Field><div className="modal-actions"><button className="button ghost" type="button" onClick={() => setCorrecting(null)}>取消</button><button className="button primary" disabled={busy}>{busy ? '正在保存…' : '保存人工纠正'}</button></div></form></Modal>}
    </div>
  )
}
