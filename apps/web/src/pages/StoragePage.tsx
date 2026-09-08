import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Database,
  Download,
  Eye,
  FileImage,
  Film,
  HardDrive,
  LoaderCircle,
  Pin,
  PinOff,
  RefreshCw,
  ScanSearch,
  ShieldAlert,
  Trash2,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { EventProvenance } from '../components/ProvenanceBadge'
import { useToast } from '../components/Toast'
import { Badge, EmptyState, ErrorState, Loading, PageHeader } from '../components/UI'
import { useRuntime } from '../contexts/RuntimeContext'
import { useApi } from '../hooks/useApi'
import { api } from '../lib/api'
import { formatTime } from '../lib/format'
import type { EventRecord, StorageItemCount, StorageOperationResult, StoragePolicy, StorageStatus } from '../types'

const emptyStatus: StorageStatus = {
  policy: 'MINIMAL',
  database_bytes: 0,
  screenshots_bytes: 0,
  clips_bytes: 0,
  logs_bytes: 0,
  firmware_bytes: 0,
  total_bytes: 0,
  max_storage_mb: 500,
  pinned_event_count: 0,
  orphan_file_count: 0,
  item_event_counts: [],
}

const policies: Array<{ value: StoragePolicy; title: string; text: string }> = [
  { value: 'MINIMAL', title: 'MINIMAL', text: '默认：每件物品仅保留最近 2 次未固定的确认变化。' },
  { value: 'BALANCED', title: 'BALANCED', text: '每件物品保留最近 10 次确认变化，媒体最长保留 7 天。' },
  { value: 'FORENSIC', title: 'FORENSIC', text: '调试用途，会保留更多候选轨迹并显著增加磁盘占用。' },
]

function sizeOf(status: StorageStatus, key: 'database' | 'screenshots' | 'clips' | 'logs' | 'firmware' | 'total') {
  if (key === 'database') return status.database_bytes ?? status.database_size ?? status.sizes?.database ?? 0
  if (key === 'screenshots') return status.screenshots_bytes ?? status.screenshot_size ?? status.sizes?.screenshots ?? 0
  if (key === 'clips') return status.clips_bytes ?? status.recordings_bytes ?? status.clip_size ?? status.sizes?.clips ?? status.sizes?.recordings ?? 0
  if (key === 'logs') return status.logs_bytes ?? status.logs_size ?? status.sizes?.logs ?? 0
  if (key === 'firmware') return status.firmware_bytes ?? status.firmware_artifacts_size ?? status.sizes?.firmware ?? 0
  return status.total_bytes ?? status.total_managed_bytes ?? status.sizes?.total ?? 0
}

function formatBytes(bytes?: number | null) {
  const value = Number(bytes || 0)
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}

function normalizeCounts(value: StorageStatus['item_event_counts']): StorageItemCount[] {
  if (Array.isArray(value)) return value
  return Object.entries(value || {}).map(([item_id, event_count]) => ({ item_id, item_name: item_id, event_count }))
}

export function StoragePage() {
  const status = useApi<StorageStatus>('/api/storage/status', emptyStatus)
  const events = useApi<EventRecord[]>('/api/events?limit=100', [])
  const runtime = useRuntime()
  const { notify } = useToast()
  const [policy, setPolicy] = useState<StoragePolicy>('MINIMAL')
  const [busy, setBusy] = useState('')
  const [operation, setOperation] = useState<StorageOperationResult | null>(null)
  const [realConfirmation, setRealConfirmation] = useState('')

  useEffect(() => setPolicy(status.data.policy || status.data.storage_mode || status.data.retention_policy || 'MINIMAL'), [status.data.policy, status.data.storage_mode, status.data.retention_policy])

  const counts = useMemo(() => normalizeCounts(status.data.item_event_counts || status.data.events_by_item), [status.data.item_event_counts, status.data.events_by_item])
  const lastReport = status.data.last_cleanup_report as { ended_at?: string; deleted_events?: number; deleted_images?: number; deleted_clips?: number; bytes_before?: number; bytes_after?: number; policy?: string } | null | undefined
  const total = sizeOf(status.data, 'total')
  const maximum = (status.data.max_storage_mb || 500) * 1024 ** 2
  const percent = maximum > 0 ? Math.min(100, total / maximum * 100) : 0

  async function run(name: string, path: string, body?: object) {
    setBusy(name)
    try {
      const result = await api<StorageOperationResult>(path, { method: 'POST', ...(body ? { json: body } : {}) })
      setOperation(result)
      notify(result.message || '操作已完成', 'success')
      await Promise.all([status.reload(), events.reload()])
      return result
    } catch (value) {
      notify(value instanceof Error ? value.message : '存储操作失败', 'danger')
      return null
    } finally {
      setBusy('')
    }
  }

  async function savePolicy(next: StoragePolicy) {
    setPolicy(next)
    setBusy('policy')
    try {
      const updated = await api<StorageStatus>('/api/storage/policy', { method: 'PATCH', json: { policy: next } })
      status.setData({ ...status.data, ...updated, policy: updated.policy || next })
      notify(`保留策略已切换为 ${next}`, 'success')
    } catch (value) {
      setPolicy(status.data.policy || status.data.storage_mode || status.data.retention_policy || 'MINIMAL')
      notify(value instanceof Error ? value.message : '保留策略保存失败', 'danger')
    } finally {
      setBusy('')
    }
  }

  async function setPinned(event: EventRecord, pinned: boolean) {
    if (!event.id) { notify('这条观察没有历史事件编号，不能作为事件固定。', 'info'); return }
    setBusy(`pin-${event.id}`)
    try {
      await api(`/api/events/${encodeURIComponent(event.id)}/pin`, { method: 'POST', json: { pinned } })
      events.setData(events.data.map((value) => value.id === event.id ? { ...value, pinned } : value))
      notify(pinned ? '事件已固定，不会被自动清理' : '事件已取消固定', 'success')
      void status.reload()
    } catch (value) {
      notify(value instanceof Error ? value.message : '固定状态更新失败', 'danger')
    } finally {
      setBusy('')
    }
  }

  async function deleteRealData() {
    if (realConfirmation !== 'DELETE REAL DATA') return
    if (!window.confirm('最后确认：永久删除所有真实事件、当前状态和对应媒体？此操作不能撤销，且不属于普通清理。')) return
    const result = await run('delete-real', '/api/storage/delete-real', { confirmation: 'DELETE REAL DATA' })
    if (result) setRealConfirmation('')
  }

  async function clearIsolated(mode: 'DEMO' | 'TEST') {
    if (!window.confirm(`清空独立 ${mode} 数据库和媒体？这不会删除 REAL 数据。`)) return
    await run(`clear-${mode.toLowerCase()}`, `/api/storage/clear-${mode.toLowerCase()}`)
  }

  if (status.loading) return <Loading label="正在核对数据库与媒体占用…" />
  if (status.error) return <ErrorState message={status.error} onRetry={status.reload} />

  return <div>
    <PageHeader
      eyebrow="管理员 · 本地存储"
      title="存储管理"
      description="扫描、预览和清理均由后端统一处理。固定事件、当前状态证据和用户参考图片不属于普通自动清理范围。"
      actions={<button className="button secondary" disabled={Boolean(busy)} onClick={() => void status.reload()}><RefreshCw />刷新状态</button>}
    />

    <section className="storage-overview panel" aria-label="存储概览">
      <div className="storage-capacity">
        <div><p className="eyebrow">当前占用</p><strong>{formatBytes(total)}</strong><span>上限 {status.data.max_storage_mb || 500} MB</span></div>
        <div className="storage-meter" role="progressbar" aria-label="磁盘配额使用率" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}><span style={{ width: `${percent}%` }} /></div>
        <Badge tone={percent >= 90 ? 'danger' : percent >= 70 ? 'warning' : 'success'}>{percent.toFixed(1)}%</Badge>
      </div>
      <div className="storage-facts">
        <div><Database /><span>数据库</span><strong>{formatBytes(sizeOf(status.data, 'database'))}</strong></div>
        <div><FileImage /><span>截图</span><strong>{formatBytes(sizeOf(status.data, 'screenshots'))}</strong></div>
        <div><Film /><span>短视频</span><strong>{formatBytes(sizeOf(status.data, 'clips'))}</strong></div>
        <div><Archive /><span>日志</span><strong>{formatBytes(sizeOf(status.data, 'logs'))}</strong></div>
        <div><HardDrive /><span>固件产物</span><strong>{formatBytes(sizeOf(status.data, 'firmware'))}</strong></div>
      </div>
      <dl className="storage-meta">
        <div><dt>运行模式</dt><dd><Badge tone={runtime.mode === 'REAL' ? 'success' : runtime.mode === 'UNKNOWN' ? 'danger' : 'warning'}>{runtime.mode}</Badge></dd></div>
        <div><dt>固定事件</dt><dd>{status.data.pinned_event_count ?? status.data.pinned_events ?? 0}</dd></div>
        <div><dt>孤儿文件</dt><dd>{status.data.orphan_file_count ?? status.data.orphan_files ?? 0}</dd></div>
        <div><dt>下次自动清理</dt><dd>{status.data.next_cleanup_at ? formatTime(status.data.next_cleanup_at) : '后端尚未报告'}</dd></div>
      </dl>
    </section>

    <div className="storage-layout">
      <section className="panel storage-section">
        <div className="section-heading"><div><p className="eyebrow">保留策略</p><h2>默认选择 MINIMAL</h2></div><Badge tone={policy === 'FORENSIC' ? 'warning' : 'success'}>{policy}</Badge></div>
        <div className="policy-options" role="radiogroup" aria-label="存储保留策略">
          {policies.map((option) => <button type="button" role="radio" aria-checked={policy === option.value} className={policy === option.value ? 'selected' : ''} disabled={busy === 'policy'} key={option.value} onClick={() => void savePolicy(option.value)}><span><strong>{option.title}</strong><small>{option.text}</small></span>{policy === option.value && <CheckCircle2 />}</button>)}
        </div>
        {policy === 'FORENSIC' && <div className="inline-banner danger"><AlertTriangle />FORENSIC 默认关闭。它会保留更多轨迹、候选记录和媒体，请持续关注磁盘占用。</div>}
      </section>

      <section className="panel storage-section">
        <div className="section-heading"><div><p className="eyebrow">安全清理</p><h2>先预览，再执行</h2></div>{busy && <LoaderCircle className="spin" />}</div>
        <p className="storage-copy">普通清理不会删除固定事件、当前状态证据、注册参考图片或唯一可回滚固件。</p>
        <div className="storage-actions">
          <button className="button secondary" disabled={Boolean(busy)} onClick={() => void run('scan', '/api/storage/scan')}><ScanSearch />立即扫描</button>
          <button className="button secondary" disabled={Boolean(busy)} onClick={() => void run('preview', '/api/storage/preview')}><Eye />预览将删除内容</button>
          <button className="button primary" disabled={Boolean(busy)} onClick={() => void run('cleanup', '/api/storage/cleanup')}><Trash2 />立即清理无用数据</button>
        </div>
        <div className="storage-secondary-actions">
          <button className="button ghost" disabled={Boolean(busy)} onClick={() => void clearIsolated('DEMO')}>清空演示数据</button>
          <button className="button ghost" disabled={Boolean(busy)} onClick={() => void clearIsolated('TEST')}>清空测试数据</button>
          <a className="button ghost" href="/api/storage/export-pinned"><Download />导出固定事件</a>
        </div>
        {operation && <div className="operation-result" aria-live="polite"><CheckCircle2 /><div><strong>{operation.message || '最近一次操作已完成'}</strong><span>{operation.reclaimable_bytes !== undefined ? `预计可释放 ${formatBytes(operation.reclaimable_bytes)}` : operation.deleted_bytes !== undefined ? `已释放 ${formatBytes(operation.deleted_bytes)}` : operation.scanned_at ? `扫描时间 ${formatTime(operation.scanned_at)}` : '详细结果以服务端报告为准。'}</span></div></div>}
        {lastReport && <details className="cleanup-report"><summary>查看最近一次清理报告</summary><dl><div><dt>完成时间</dt><dd>{formatTime(lastReport.ended_at)}</dd></div><div><dt>策略</dt><dd>{lastReport.policy || '未报告'}</dd></div><div><dt>删除事件</dt><dd>{lastReport.deleted_events ?? 0}</dd></div><div><dt>删除截图 / 录像</dt><dd>{lastReport.deleted_images ?? 0} / {lastReport.deleted_clips ?? 0}</dd></div><div><dt>清理前 / 后</dt><dd>{formatBytes(lastReport.bytes_before)} / {formatBytes(lastReport.bytes_after)}</dd></div></dl></details>}
      </section>
    </div>

    <section className="panel storage-section">
      <div className="section-heading"><div><p className="eyebrow">每件物品</p><h2>历史事件保留数量</h2></div><span className="muted">固定事件不计入自动保留上限</span></div>
      {counts.length === 0 ? <EmptyState icon={<Database />} title="暂无事件统计" description="当前数据库没有可展示的物品事件，或后端尚未返回按物品统计。" /> : <div className="item-retention-table"><div className="retention-header"><span>物品</span><span>事件</span><span>固定</span></div>{counts.map((item) => <div key={item.item_id}><strong>{item.item_name || item.item_id}</strong><span>{item.event_count}</span><span>{item.pinned_count || 0}</span></div>)}</div>}
    </section>

    <section className="panel storage-section">
      <div className="section-heading"><div><p className="eyebrow">固定证据</p><h2>保留或取消保留事件</h2></div><Badge tone="neutral">最近 100 条</Badge></div>
      {events.error ? <ErrorState message={events.error} onRetry={events.reload} /> : events.data.length === 0 ? <EmptyState icon={<Pin />} title="没有可固定的事件" description={runtime.mode === 'REAL' ? '暂时没有真实摄像头产生的位置记录。' : '当前模式尚无事件记录。'} /> : <div className="storage-event-list">{events.data.map((event) => <article key={event.id}><div><strong>{event.item_name}</strong><span>{event.zone_name || event.new_zone || '位置未确认'} · {formatTime(event.timestamp_end || event.timestamp_start)}</span><EventProvenance event={event} /></div><button type="button" className={`button small ${event.pinned ? 'secondary' : 'ghost'}`} disabled={busy === `pin-${event.id}`} aria-pressed={Boolean(event.pinned)} onClick={() => void setPinned(event, !event.pinned)}>{event.pinned ? <PinOff /> : <Pin />}{event.pinned ? '取消固定' : '固定事件'}</button></article>)}</div>}
    </section>

    <section className="storage-danger panel" aria-labelledby="delete-real-title">
      <div><ShieldAlert /><div><p className="eyebrow">危险操作</p><h2 id="delete-real-title">删除所有真实记录</h2><p>这不是“清理无用数据”。它会永久删除 REAL 数据库中的真实事件、当前状态与对应媒体，不影响演示清理按钮。</p></div></div>
      <label><span>输入 DELETE REAL DATA 才能解锁</span><input value={realConfirmation} autoComplete="off" onChange={(event) => setRealConfirmation(event.target.value)} placeholder="DELETE REAL DATA" /></label>
      <button className="button danger-button" disabled={runtime.mode !== 'REAL' || realConfirmation !== 'DELETE REAL DATA' || Boolean(busy)} onClick={() => void deleteRealData()}><Trash2 />{runtime.mode === 'REAL' ? '永久删除所有真实记录' : '只能在 REAL 模式执行'}</button>
    </section>
  </div>
}
