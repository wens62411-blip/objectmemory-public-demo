import { Clock3, ImagePlus, MapPin, PackagePlus, Pencil, Plus, Printer, Trash2, Upload } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, EmptyState, ErrorState, Field, Loading, Modal, PageHeader } from '../components/UI'
import { useApi } from '../hooks/useApi'
import { api } from '../lib/api'
import { formatTime } from '../lib/format'
import type { Item, ReferenceImage, SearchResult } from '../types'
import { useToast } from '../components/Toast'
import { EventProvenance } from '../components/ProvenanceBadge'
import { useRuntime } from '../contexts/RuntimeContext'
import { evidenceBadge, isLocationUnavailable, isObservationEvidence, locationConfidenceLabel, locationPlace, selectLocationEvidence } from '../lib/provenance'
import { PhotoFilePicker, PhotoRegistration, profileIsLoaded, profileStatusText } from '../components/PhotoRegistration'
import { reportPollingError, useVisiblePolling } from '../hooks/useVisiblePolling'

interface ItemForm { name: string; type: string; description: string; owner: string; color: string; features: string; aliases: string; aruco_id: string; ring_enabled: boolean }
const emptyForm: ItemForm = { name: '', type: '日常物品', description: '', owner: '', color: '', features: '', aliases: '', aruco_id: '', ring_enabled: false }

export function ItemsPage() {
  const items = useApi<Item[]>('/api/items', [])
  const [locations, setLocations] = useState<Record<string, SearchResult | null>>({})
  const [locationErrors, setLocationErrors] = useState<Record<string, string>>({})
  const [saveReceipt, setSaveReceipt] = useState('')
  const [modal, setModal] = useState(false)
  const [editing, setEditing] = useState<Item | null>(null)
  const [photoItem, setPhotoItem] = useState<Item | null>(null)
  const [form, setForm] = useState<ItemForm>(emptyForm)
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const { notify } = useToast()
  const runtime = useRuntime()
  const occupiedMarkerIds = new Set(items.data.filter((item) => item.id !== editing?.id && item.aruco_id !== null && item.aruco_id !== undefined).map((item) => item.aruco_id))

  useVisiblePolling(async (signal) => {
    const snapshot = items.data
    const responses = await Promise.allSettled(snapshot.map((item) => api<SearchResult>(`/api/items/${encodeURIComponent(item.id)}/last-location`, { signal })))
    if (signal.aborted && !reportPollingError(signal)) return
    const updates: Record<string, SearchResult> = {}, failures: Record<string, string> = {}
    responses.forEach((result, index) => {
      const id = snapshot[index].id
      if (result.status === 'fulfilled' && !signal.aborted) updates[id] = result.value
      else failures[id] = signal.reason === 'poll_timeout' ? '位置读取超时，保留上次可靠记录。' : '位置暂时无法刷新，保留上次可靠记录；不代表物品仍在原处。'
    })
    setLocations((previous) => ({ ...previous, ...updates }))
    setLocationErrors(failures)
  }, { enabled: items.data.length > 0, immediate: true, resetKey: items.data })

  function openNew() { setEditing(null); setForm(emptyForm); setFiles([]); setModal(true) }
  function openEdit(item: Item) { setEditing(item); setForm({ name: item.name, type: item.type, description: item.description || '', owner: item.owner || '', color: item.color || '', features: item.features || '', aliases: item.aliases?.join('、') || '', aruco_id: item.aruco_id !== null && item.aruco_id !== undefined ? String(item.aruco_id).padStart(3, '0') : '', ring_enabled: item.ring_enabled }); setFiles([]); setModal(true) }
  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (!form.name.trim()) return
    if (form.aruco_id !== '' && occupiedMarkerIds.has(Number(form.aruco_id))) { notify('这个标签编号已被其他物品占用，请选择可用编号。', 'danger'); return }
    setBusy(true)
    try {
      const payload = { name: form.name.trim(), type: form.type, description: form.description, owner: form.owner, color: form.color, features: form.features, aliases: form.aliases.split(/[、,，]/).map((value) => value.trim()).filter(Boolean), aruco_id: form.aruco_id ? Number(form.aruco_id) : null, ring_enabled: form.ring_enabled }
      const item = editing ? await api<Item>(`/api/items/${editing.id}`, { method: 'PATCH', json: payload }) : await api<Item>('/api/items', { method: 'POST', json: payload })
      // Keep the created identity if photo validation fails, so retry cannot create a duplicate item.
      setEditing(item)
      let savedPhotos: ReferenceImage[] = []
      if (files.length) {
        const data = new FormData(); files.forEach((file) => data.append('files', file))
        savedPhotos = await api<ReferenceImage[]>(`/api/items/${item.id}/reference-images`, { method: 'POST', body: data })
      }
      const receipt = savedPhotos.length ? `“${item.name}”信息及本次 ${savedPhotos.length} 张照片已保存。下一步确认目标区域并建立档案；识别效果仍待现场验证。` : `“${item.name}”信息已保存；照片档案与现场识别是独立步骤。`
      setSaveReceipt(receipt); notify(receipt, 'success'); setModal(false)
      if (!editing || files.length) setPhotoItem({ ...item, reference_images: [...(item.reference_images || []), ...savedPhotos] })
      setFiles([]); await items.reload()
    } catch (value) { notify(value instanceof Error ? value.message : '保存失败', 'danger') }
    finally { setBusy(false) }
  }
  async function remove(item: Item) {
    if (!window.confirm(`删除“${item.name}”？这个操作不会删除其他物品。`)) return
    try { await api(`/api/items/${item.id}`, { method: 'DELETE' }); await items.reload(); notify('物品已删除', 'success') }
    catch (value) { notify(value instanceof Error ? value.message : '删除失败', 'danger') }
  }

  return (
    <div>
      <PageHeader eyebrow="物品管理" title="教物忆认出你的东西" description="从一张照片开始，确认物品、建立本地识别档案，再用摄像头的新视角检验。" actions={<button className="button primary" onClick={openNew}><Plus />添加物品</button>} />
      <div className="inline-banner info"><ImagePlus /><div><strong>照片已保存，不等于已经能识别</strong><span>每件物品都能单独管理照片、确认目标框，并查看后台实际加载的档案版本。相似物品仍可能无法区分，现场测试会给出拒绝原因。</span></div></div>
      {saveReceipt && <p className="photo-notice" role="status">{saveReceipt}</p>}
      {items.loading ? <Loading /> : items.error ? <ErrorState message={items.error} onRetry={items.reload} /> : items.data.length === 0 ? <EmptyState icon={<PackagePlus />} title="还没有注册物品" description="添加手机、钥匙或钱包，上传一张清楚的照片即可开始。" action={<button className="button primary" onClick={openNew}><Plus />添加第一个物品</button>} /> : <div className="item-grid">{items.data.map((item) => {
        const location = locations[item.id]
        const evidence = selectLocationEvidence(location, runtime.mode)
        const observation = isObservationEvidence(evidence)
        const currentProfile = locationErrors[item.id] ? null : location?.item && Object.prototype.hasOwnProperty.call(location.item, 'recognition_profile') ? location.item.recognition_profile : item.recognition_profile
        return <article className="item-card" key={item.id}>
          <div className="item-card-top"><div className="item-avatar">{item.name.slice(0, 2)}</div><div className="item-badges">{item.aruco_id !== null && item.aruco_id !== undefined && <Badge tone="success">标签 {String(item.aruco_id).padStart(3, '0')}</Badge>}{item.ring_enabled && <Badge tone="blue">可响铃</Badge>}</div></div>
          <h2>{item.name}</h2><p>{[item.color, item.type, item.owner].filter(Boolean).join(' · ') || '日常物品'}</p>
          <div className="item-photo-summary"><Badge tone={profileIsLoaded(currentProfile) ? 'blue' : 'neutral'}>{profileStatusText(currentProfile)}</Badge><p>{item.reference_images?.length || 0} 张参考照片 · 档案版本 {currentProfile?.profile_version ?? '尚未建立'}</p></div>
          {currentProfile?.quality_warnings?.[0] && <p className="photo-help">参考照片提示：{currentProfile.quality_warnings[0]} 不代表上传失败，详情见照片管理。</p>}
          <button className="button primary item-photo-manage" onClick={() => setPhotoItem(item)}><ImagePlus />管理照片 / 测试识别</button>
          <div className="last-location"><MapPin /><div><span>{observation ? '最后看到' : '最后确认位置'}</span><strong>{evidence ? locationPlace(evidence) : runtime.mode === 'REAL' ? '暂时没有真实摄像头产生的位置记录。' : runtime.mode === 'UNKNOWN' ? '运行模式未确认，位置证据已隐藏' : '还没有记录'}</strong>{evidence && <><small><Clock3 />{formatTime(evidence.timestamp_end || evidence.timestamp_start)} · {locationConfidenceLabel(evidence)}</small>{isLocationUnavailable(evidence) && <small>{evidenceBadge(evidence).label}；上方仅为最后可见区域。</small>}<EventProvenance event={evidence} compact />{observation && <Link to={`/search?q=${encodeURIComponent(item.name)}`}>查看观察截图与历史确认</Link>}</>}</div></div>
          {locationErrors[item.id] && <p className="inline-error" role="status">{locationErrors[item.id]}</p>}
          {location?.observation_hint?.message?.trim() && <p className="photo-help"><strong>当前观察提示（不是位置记录）：</strong>{location.observation_hint.message}</p>}
          <div className="item-card-actions"><Link className="button secondary small" to={`/events?item_id=${item.id}`}>查看历史</Link><button className="icon-button" title="编辑" onClick={() => openEdit(item)}><Pencil /></button><button className="icon-button danger-icon" title="删除" onClick={() => remove(item)}><Trash2 /></button></div>
        </article>
      })}</div>}
      <details className="item-label-tools"><summary>高级诊断 · ArUco 标签</summary><p>标签识别只用于诊断，不证明无标签照片识别已成功。{runtime.mode === 'DEMO' ? '演示标签与事件仍属于模拟来源。' : '可在物品编辑中绑定未占用编号。'}</p><a className="button secondary small" href="/api/demo/markers/aruco-markers-a4.svg" target="_blank" rel="noreferrer"><Printer />打印诊断标签</a></details>

      {photoItem && <PhotoRegistration key={photoItem.id} item={photoItem} onClose={() => setPhotoItem(null)} onChanged={() => { void items.reload() }} />}

      {modal && <Modal title={editing ? `编辑 ${editing.name}` : '添加物品'} description="保留名字和外观线索；照片保存后继续确认目标、建立档案。" onClose={() => { if (!busy) setModal(false) }} wide>
        <form className="wizard-body item-form" onSubmit={save}>
          <div className="form-grid"><Field label="物品名称"><input autoFocus value={form.name} onChange={(event) => setForm((value) => ({ ...value, name: event.target.value }))} placeholder="例如：我的手机" required /></Field><Field label="类型"><select value={form.type} onChange={(event) => setForm((value) => ({ ...value, type: event.target.value }))}><option>手机</option><option>钥匙</option><option>钱包</option><option>遥控器</option><option>眼镜</option><option>日常物品</option></select></Field><Field label="所有者"><input value={form.owner} onChange={(event) => setForm((value) => ({ ...value, owner: event.target.value }))} placeholder="例如：我 / 妈妈" /></Field><Field label="颜色"><input value={form.color} onChange={(event) => setForm((value) => ({ ...value, color: event.target.value }))} placeholder="例如：深蓝色" /></Field><Field label="别名" hint="用逗号分隔，搜索时也能识别。"><input value={form.aliases} onChange={(event) => setForm((value) => ({ ...value, aliases: event.target.value }))} placeholder="手机，电话" /></Field><Field label="特征说明" className="full-field"><input value={form.features} onChange={(event) => setForm((value) => ({ ...value, features: event.target.value }))} placeholder="例如：透明壳、背面有圆形贴纸" /></Field><Field label="描述" className="full-field"><textarea rows={2} value={form.description} onChange={(event) => setForm((value) => ({ ...value, description: event.target.value }))} placeholder="帮助家人区分相似物品" /></Field></div>
          <label className="toggle-row"><div><strong>启用手机伴侣响铃</strong><span>仅适合在浏览器伴侣页已打开并允许声音的测试手机。</span></div><input type="checkbox" checked={form.ring_enabled} onChange={(event) => setForm((value) => ({ ...value, ring_enabled: event.target.checked }))} /></label>
          <section className="photo-section"><div className="section-heading compact-heading"><div><p className="eyebrow">照片注册</p><h3>上传第一张参考照片</h3><p>也可以先保存物品，再从正在使用的摄像头抓拍。</p></div></div><PhotoFilePicker files={files} onChange={setFiles} disabled={busy} existingCount={editing?.reference_images?.length || 0} /></section>
          <details className="item-label-tools"><summary>高级诊断 · 绑定标签</summary><Field label="ArUco 标签编号"><select value={form.aruco_id} onChange={(event) => setForm((value) => ({ ...value, aruco_id: event.target.value }))}><option value="">不绑定</option>{Array.from({ length: 50 }, (_, id) => <option key={id} value={String(id).padStart(3, '0')} disabled={occupiedMarkerIds.has(id)}>{String(id).padStart(3, '0')}{occupiedMarkerIds.has(id) ? '（已被其他物品使用）' : ''}</option>)}</select></Field><p>仅用于标签诊断，不会替代照片识别。普通照片注册无需选择标签。</p></details>
          <div className="wizard-footer split"><button className="button ghost" type="button" disabled={busy} onClick={() => setModal(false)}>取消</button><button className="button primary" disabled={busy || !form.name.trim()}><Upload />{busy ? '正在保存…' : editing ? '保存修改' : '保存物品'}</button></div>
        </form>
      </Modal>}
    </div>
  )
}
