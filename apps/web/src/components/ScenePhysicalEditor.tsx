import { Field } from './UI'
import type { SceneCalibration, ScenePhysical, ScenePoint, SceneSurface } from '../types'

export function validScenePhysical(surface: SceneSurface) {
  const physical = surface.physical
  if (!physical) return !surface.calibration
  if (![physical.width, physical.depth, physical.height, physical.thickness, physical.origin_x, physical.origin_z, physical.rotation_y].every(Number.isFinite)
    || physical.width < .001 || physical.depth < .001 || physical.thickness < .001 || physical.height < 0
    || Math.max(physical.width, physical.depth, physical.height, physical.thickness) > 100
    || Math.max(Math.abs(physical.origin_x), Math.abs(physical.origin_z)) > 1000 || Math.abs(physical.rotation_y) > 360
    || physical.thickness > Math.max(physical.height, .1) || physical.thickness > Math.min(physical.width, physical.depth)
    || surface.surface_type === 'floor' && physical.height !== 0) return false
  const calibration = surface.calibration
  if (!calibration) return true
  const finitePoint = (point: ScenePoint) => point.length === 2 && point.every(Number.isFinite)
  return calibration.image_points.length === 4 && calibration.plane_points.length === 4
    && calibration.image_points.every(point => finitePoint(point) && point.every(value => value >= 0 && value <= 1))
    && calibration.plane_points.every(finitePoint)
    && (calibration.validation_points || []).every(point => finitePoint(point.image) && finitePoint(point.plane) && point.image.every(value => value >= 0 && value <= 1))
}

/** All defaults are unsaved edit suggestions; enabling/changing revokes confirmation. */
export function ScenePhysicalEditor({ surface, onChange }: { surface: SceneSurface; onChange: (patch: Partial<SceneSurface>) => void }) {
  const physical = surface.physical, calibration = surface.calibration
  const editPhysical = (patch: Partial<ScenePhysical>) => physical && onChange({ physical: { ...physical, ...patch }, calibration: null })
  const editCalibration = (patch: Partial<SceneCalibration>) => calibration && onChange({ calibration: { ...calibration, ...patch } })
  const numberValue = (value: number) => Number.isFinite(value) ? value : ''
  return <div className="scene-physical-editor">
    <label className="scene-confirm"><input type="checkbox" checked={Boolean(physical)} onChange={event => onChange({ physical: event.target.checked ? { width: 1, depth: 1, height: surface.surface_type === 'floor' ? 0 : .7, thickness: .05, origin_x: 0, origin_z: 0, rotation_y: 0, unit: 'relative' } : null, calibration: null })} /><span>为此区域设置三维尺寸与支撑面高度</span></label>
    {physical && <>
      <p className="muted">以下初值仅为待修改的尺寸建议。请按实际比例核对后确认；未测量时使用相对单位。改尺寸会清除旧标定，避免错配。</p>
      <Field label="尺寸单位"><select value={physical.unit} onChange={event => editPhysical({ unit: event.target.value as ScenePhysical['unit'] })}><option value="relative">相对单位（未测量）</option><option value="m">米（我已测量）</option></select></Field>
      <div className="scene-dimension-grid">{([['width', '宽度'], ['depth', '纵深'], ['height', '支撑面高度'], ['thickness', '厚度'], ['origin_x', '世界横向起点'], ['origin_z', '世界纵深起点'], ['rotation_y', '水平旋转角度']] as const).map(([key, label]) => <Field key={key} label={label}><input type="number" step="0.01" value={numberValue(physical[key])} onChange={event => editPhysical({ [key]: event.target.valueAsNumber })} /></Field>)}</div>
      <p className="muted">支撑面高度是上表面离地高度；起点对应下方第 1 个锚点。世界坐标 X 向右、Y 向上、Z 向纵深。旋转角用度。</p>
      <button type="button" className="button secondary small" disabled={surface.image_polygon.length !== 4 || !validScenePhysical({ ...surface, calibration: null })} onClick={() => onChange({ calibration: { image_points: surface.image_polygon.map(point => [...point] as ScenePoint), plane_points: [[0, 0], [physical.width, 0], [physical.width, physical.depth], [0, physical.depth]], validation_points: [] } })}>将当前四个角确认为平面标定锚点</button>
      <p className="muted">按真实支撑面顺序标记四角：起点 → 宽方向 → 对角 → 深方向。检测框不等于桌面四角；不确定时只保存家具形状，不标定物品点。</p>
      {calibration && <div className="scene-calibration-points">
        <h3>图像四角 → 支撑面坐标</h3>
        {calibration.image_points.map((point, index) => <div className="scene-anchor-row" key={index}><strong>锚点 {index + 1}</strong>{(['image_points', 'plane_points'] as const).map(kind => <div key={kind}>{([0, 1] as const).map(axis => <label key={axis}><span>{kind === 'image_points' ? `图像${axis ? '纵' : '横'}%` : `平面${axis ? '深' : '宽'}`}</span><input type="number" step="0.01" aria-label={`锚点${index + 1}${kind === 'image_points' ? '图像' : '平面'}${axis ? '纵' : '横'}`} value={numberValue((kind === 'image_points' ? point[axis] * 100 : calibration.plane_points[index][axis]))} onChange={event => editCalibration({ [kind]: calibration[kind].map((value, i) => i === index ? value.map((number, a) => a === axis ? event.target.valueAsNumber / (kind === 'image_points' ? 100 : 1) : number) as ScenePoint : value) })} /></label>)}</div>)}</div>)}
        <button type="button" className="button ghost small" onClick={() => onChange({ calibration: null })}>移除标定，保留三维形状</button>
        <h3>独立验证点（不是四个拟合锚点）</h3>
        <p className="muted">选另一个清晰可见点，填写它在截图和平面中的实测位置。没有验证点会始终标为“未独立验证”，不会宣称投影准确。</p>
        {(calibration.validation_points || []).map((entry, index) => <div className="scene-anchor-row" key={index}><strong>验证 {index + 1}</strong>{(['image', 'plane'] as const).map(kind => <div key={kind}>{([0, 1] as const).map(axis => <label key={axis}><span>{kind === 'image' ? `图像${axis ? '纵' : '横'}%` : `平面${axis ? '深' : '宽'}`}</span><input type="number" step="0.01" aria-label={`验证点${index + 1}${kind === 'image' ? '图像' : '平面'}${axis ? '纵' : '横'}`} value={numberValue(entry[kind][axis] * (kind === 'image' ? 100 : 1))} onChange={event => editCalibration({ validation_points: calibration.validation_points!.map((value, i) => i === index ? { ...value, [kind]: value[kind].map((number, a) => a === axis ? event.target.valueAsNumber / (kind === 'image' ? 100 : 1) : number) } : value) as SceneCalibration['validation_points'] })} /></label>)}</div>)}<button type="button" className="button ghost small" onClick={() => editCalibration({ validation_points: calibration.validation_points!.filter((_, i) => i !== index) })}>删除验证点 {index + 1}</button></div>)}
        <button type="button" className="button secondary small" disabled={(calibration.validation_points?.length || 0) >= 8} onClick={() => editCalibration({ validation_points: [...(calibration.validation_points || []), { image: [NaN, NaN], plane: [NaN, NaN] }] })}>添加独立验证点</button>
      </div>}
      {surface.validation && <p className="scene-message">上次服务端验证：{surface.validation.status === 'passed' ? '独立点检查通过' : surface.validation.status === 'failed' ? '独立点误差超限，请修正' : '未独立验证'}{surface.validation.max_error != null ? ` · 最大误差 ${surface.validation.max_error.toFixed(4)} ${physical.unit === 'm' ? '米' : '相对单位'}` : ''}。未保存修改不计入这个结果。</p>}
      {!validScenePhysical(surface) && <p className="inline-error">请填写完整有效的尺寸和标定点，宽度、纵深、厚度须大于零。</p>}
    </>}
  </div>
}
