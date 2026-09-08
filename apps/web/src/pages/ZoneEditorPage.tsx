import { ArrowLeft, Check, Eye, EyeOff, ImageOff, Layers3, MousePointer2, Plus, Redo2, Save, Trash2, Undo2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { EmptyState, ErrorState, Field, Loading, PageHeader } from '../components/UI'
import { SourceBadge } from '../components/ProvenanceBadge'
import { useApi } from '../hooks/useApi'
import { api } from '../lib/api'
import type { Camera, Zone } from '../types'
import { useToast } from '../components/Toast'

const zoneSuggestions = ['桌面', '沙发左侧', '沙发右侧', '床头柜', '柜子', '门口', '地面', '未定义区域']

export function ZoneEditorPage() {
  const { cameraId = '' } = useParams()
  const camera = useApi<Camera | null>(cameraId ? `/api/cameras/${cameraId}` : null, null)
  const zones = useApi<Zone[]>(cameraId ? `/api/cameras/${cameraId}/zones` : null, [])
  const [points, setPoints] = useState<[number, number][]>([])
  const [redo, setRedo] = useState<[number, number][]>([])
  const [name, setName] = useState('桌面')
  const [priority, setPriority] = useState(10)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [drawing, setDrawing] = useState(true)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [imageFailed, setImageFailed] = useState(false)
  const [nonce, setNonce] = useState(Date.now())
  const [ratio, setRatio] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const svgRef = useRef<SVGSVGElement>(null)
  const { notify } = useToast()

  useEffect(() => { setNonce(Date.now()); setImageFailed(false) }, [cameraId])

  function coordinates(clientX: number, clientY: number): [number, number] {
    const rect = svgRef.current!.getBoundingClientRect()
    return [Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)), Math.max(0, Math.min(1, (clientY - rect.top) / rect.height))]
  }
  function addPoint(event: React.PointerEvent<SVGSVGElement>) {
    if (!drawing || dragIndex !== null || event.target !== event.currentTarget) return
    setPoints((value) => [...value, coordinates(event.clientX, event.clientY)]); setRedo([])
  }
  function drag(event: React.PointerEvent<SVGSVGElement>) {
    if (dragIndex === null) return
    const next = coordinates(event.clientX, event.clientY)
    setPoints((value) => value.map((point, index) => index === dragIndex ? next : point))
  }
  function resetEditor() { setEditingId(null); setPoints([]); setName('桌面'); setPriority(10); setDrawing(true); setRedo([]) }
  function edit(zone: Zone) { setEditingId(zone.id); setPoints(zone.points); setName(zone.name); setPriority(zone.priority); setDrawing(false); setRedo([]) }

  async function save() {
    if (points.length < 3) { notify('请至少点出 3 个顶点，围成一个区域', 'info'); return }
    if (!name.trim()) { notify('请给区域起个名字', 'info'); return }
    setBusy(true)
    try {
      const body = { name: name.trim(), points, priority, enabled: true }
      if (editingId) await api(`/api/zones/${editingId}`, { method: 'PATCH', json: body })
      else await api(`/api/cameras/${cameraId}/zones`, { method: 'POST', json: body })
      notify(editingId ? '区域修改已保存' : '区域已保存', 'success'); resetEditor(); await zones.reload()
    } catch (value) { notify(value instanceof Error ? value.message : '区域保存失败', 'danger') }
    finally { setBusy(false) }
  }
  async function remove(zone: Zone) {
    if (!window.confirm(`删除“${zone.name}”区域？已有事件中的区域文字仍会保留。`)) return
    try { await api(`/api/zones/${zone.id}`, { method: 'DELETE' }); if (editingId === zone.id) resetEditor(); await zones.reload(); notify('区域已删除', 'success') }
    catch (value) { notify(value instanceof Error ? value.message : '删除失败', 'danger') }
  }
  async function toggle(zone: Zone) {
    try { await api(`/api/zones/${zone.id}`, { method: 'PATCH', json: { enabled: !zone.enabled } }); await zones.reload() }
    catch (value) { notify(value instanceof Error ? value.message : '更新失败', 'danger') }
  }

  if (camera.loading || zones.loading) return <Loading label="正在准备区域画面…" />
  if (camera.error || zones.error) return <ErrorState message={camera.error || zones.error} onRetry={() => { void camera.reload(); void zones.reload() }} />
  if (!camera.data) return <EmptyState title="找不到这个摄像头" description="它可能已经被删除。" action={<Link className="button secondary" to="/cameras">返回摄像头管理</Link>} />

  return (
    <div>
      <PageHeader eyebrow="区域编辑" title={`${camera.data.room_name} · ${camera.data.name}`} description="沿区域边缘依次点击，至少 3 个点；保存的是归一化坐标。" actions={<Link className="button secondary" to="/cameras"><ArrowLeft />返回摄像头</Link>} />
      <div className="zone-editor-layout">
        <section className="zone-canvas-panel panel">
          <div className="zone-canvas-toolbar"><div><SourceBadge record={camera.data} /><span>{points.length ? `${points.length} 个顶点` : '点击画面开始描边'}</span></div><div><button className="icon-button" disabled={!points.length} title="撤销" onClick={() => { const last = points.at(-1); if (last) { setPoints((value) => value.slice(0, -1)); setRedo((value) => [...value, last]) } }}><Undo2 /></button><button className="icon-button" disabled={!redo.length} title="重做" onClick={() => { const last = redo.at(-1); if (last) { setPoints((value) => [...value, last]); setRedo((value) => value.slice(0, -1)) } }}><Redo2 /></button><button className="icon-button" title="刷新画面" onClick={() => { setNonce(Date.now()); setImageFailed(false) }}><ImageOff /></button></div></div>
          <div className="zone-canvas" style={{ aspectRatio: ratio || (camera.data.health?.width && camera.data.health.height ? camera.data.health.width / camera.data.health.height : 4 / 3) }}>
            {!imageFailed ? <img src={`/api/cameras/${cameraId}/frame?t=${nonce}`} alt="用于划分区域的摄像头静态画面" onLoad={(event) => { if (event.currentTarget.naturalWidth && event.currentTarget.naturalHeight) setRatio(event.currentTarget.naturalWidth / event.currentTarget.naturalHeight) }} onError={() => setImageFailed(true)} /> : <div className="canvas-no-frame"><ImageOff /><strong>暂时没有静态画面</strong><span>先启动摄像头，再点击右上角刷新；已有区域仍可编辑。</span></div>}
            <svg ref={svgRef} viewBox="0 0 1000 562" preserveAspectRatio="none" onPointerDown={addPoint} onPointerMove={drag} onPointerUp={() => setDragIndex(null)} onPointerLeave={() => setDragIndex(null)} className={drawing ? 'drawing' : ''}>
              {zones.data.filter((zone) => zone.id !== editingId).map((zone) => <g className={`saved-zone ${zone.enabled ? '' : 'disabled'}`} key={zone.id}><polygon points={zone.points.map(([x, y]) => `${x * 1000},${y * 562}`).join(' ')} /><text x={(zone.points[0]?.[0] || 0) * 1000 + 10} y={(zone.points[0]?.[1] || 0) * 562 + 25}>{zone.name}</text></g>)}
              {points.length > 0 && <g className="working-zone"><polyline points={points.map(([x, y]) => `${x * 1000},${y * 562}`).join(' ')} />{points.length >= 3 && <polygon points={points.map(([x, y]) => `${x * 1000},${y * 562}`).join(' ')} />}{points.map(([x, y], index) => <circle key={index} cx={x * 1000} cy={y * 562} r="10" onPointerDown={(event) => { event.stopPropagation(); setDragIndex(index); event.currentTarget.setPointerCapture(event.pointerId) }} />)}{points[0] && <text x={points[0][0] * 1000 + 12} y={points[0][1] * 562 - 14}>{name || '新区域'}</text>}</g>}
            </svg>
            {!points.length && <div className="canvas-instruction"><MousePointer2 />点击区域边缘添加第一个点</div>}
          </div>
          <p className="canvas-tip"><Layers3 />区域重叠时，数字更大的优先级先匹配。拖动圆点可以微调边界。</p>
        </section>
        <aside className="zone-sidebar panel">
          <div><p className="eyebrow">{editingId ? '修改区域' : '新建区域'}</p><h2>{editingId ? name : '圈出一个位置'}</h2></div>
          <Field label="区域名称"><input list="zone-names" value={name} onChange={(event) => setName(event.target.value)} /><datalist id="zone-names">{zoneSuggestions.map((value) => <option key={value} value={value} />)}</datalist></Field>
          <Field label="重叠优先级" hint="多个区域重叠时，较大的数字优先。"><input type="number" min={0} max={100} value={priority} onChange={(event) => setPriority(Number(event.target.value))} /></Field>
          <label className="toggle-row"><div><strong>继续添加顶点</strong><span>关闭后只允许拖动现有点</span></div><input type="checkbox" checked={drawing} onChange={(event) => setDrawing(event.target.checked)} /></label>
          <div className="zone-save-actions"><button className="button primary full" onClick={save} disabled={busy || points.length < 3}><Save />{busy ? '正在保存…' : editingId ? '保存修改' : '保存这个区域'}</button>{editingId && <button className="button ghost full" onClick={resetEditor}><Plus />改为新建区域</button>}</div>
          <hr />
          <div className="section-heading compact-heading"><div><p className="eyebrow">已保存</p><h3>{zones.data.length} 个区域</h3></div></div>
          <div className="zone-list">{zones.data.length === 0 ? <p className="muted-block">还没有区域。先在左侧画面上点击至少 3 个点。</p> : zones.data.sort((a, b) => b.priority - a.priority).map((zone) => <div className={`${editingId === zone.id ? 'active' : ''} ${zone.enabled ? '' : 'disabled'}`} key={zone.id}><button className="zone-select" onClick={() => edit(zone)}><span className="zone-color" /><div><strong>{zone.name}</strong><span>{zone.points.length} 个点 · 优先级 {zone.priority}</span></div>{editingId === zone.id && <Check />}</button><button className="icon-button compact" title={zone.enabled ? '停用区域' : '启用区域'} onClick={() => toggle(zone)}>{zone.enabled ? <Eye /> : <EyeOff />}</button><button className="icon-button compact danger-icon" title="删除区域" onClick={() => remove(zone)}><Trash2 /></button></div>)}</div>
        </aside>
      </div>
    </div>
  )
}
