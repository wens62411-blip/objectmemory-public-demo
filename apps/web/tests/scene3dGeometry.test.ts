import * as THREE from 'three'
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js'
import { describe, expect, it, vi } from 'vitest'
import { createSceneWorld, createSceneMarker, disposeSceneObjects, markerIsStale, usableSceneMarker } from '../src/components/scene3dGeometry'
import { validScenePhysical } from '../src/components/ScenePhysicalEditor'
import type { SceneDocument, SceneMarker, SceneSurface, SceneWorldGeometry } from '../src/types'

const world: SceneWorldGeometry = { schema: 'scene3d_v1', coordinate_system: 'right_handed_y_up', unit: 'relative', status: 'confirmed', objects: [
  { id: 'top', surface_id: 'table-a', surface_type: 'table', part: 'tabletop', shape: 'box', position: [2, .75, 1], size: [1.2, .08, .8], rotation_y: 35, source: 'user_confirmed_geometry', estimated: true },
  { id: 'leg', surface_id: 'table-a', surface_type: 'table', part: 'leg', shape: 'box', position: [1.5, .35, .7], size: [.05, .7, .05], rotation_y: 35, source: 'user_confirmed_geometry', estimated: true },
] }
const scene = { camera_id: 'camera-a', scene_version: 4, runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, source_session_id: 'scene-session', calibration_status: 'calibrated', world_geometry: world } as SceneDocument
const marker: SceneMarker = { item_id: 'registered-phone', camera_id: 'camera-a', scene_version: 4, frame_id: 99, source_session_id: 'current-session', observation_timestamp: '2026-09-06T10:00:00Z', runtime_mode: 'REAL', source_type: 'opencv_camera', is_simulated: false, position_status: 'mapped', mapping_method: 'homography', map_position: [2, .79, 1] }
const known = new Map([['registered-phone', '我的手机']])
const time = Date.parse(marker.observation_timestamp!)

describe('真实 Three.js 几何单元测试（不是浏览器 WebGL 或物理摄像头验收）', () => {
  it('无服务端几何时没有默认房间、地面或手机', () => {
    expect(createSceneWorld(null).children).toHaveLength(0)
    expect(createSceneWorld({ ...world, objects: [] }).children).toHaveLength(0)
  })
  it('直接构造有高度/厚度的实际 Mesh，采用服务端尺寸位置与角度', () => {
    const group = createSceneWorld(world)
    expect(group.children).toHaveLength(2)
    const top = group.getObjectByName('top') as THREE.Mesh<THREE.BoxGeometry>
    expect(top).toBeInstanceOf(THREE.Mesh)
    expect(top.geometry.parameters).toMatchObject({ width: 1.2, height: .08, depth: .8 })
    expect(top.position.toArray()).toEqual([2, .75, 1])
    expect(top.rotation.y).toBeCloseTo(35 * Math.PI / 180)
    expect(top.userData.surfaceId).toBe('table-a')
    expect(group.getObjectByName('leg')?.position.y).toBe(.35)
    disposeSceneObjects(group)
  })
  it('不消费无效坐标或未知来源几何，模型估计必须是禁用映射的明确草稿', () => {
    expect(createSceneWorld({ ...world, objects: [{ ...world.objects[0], position: [NaN, 0, 0] }] }).children).toHaveLength(0)
    expect(createSceneWorld({ ...world, objects: [{ ...world.objects[0], source: 'model_proposal_estimate' }] }).children).toHaveLength(0)
    const draft = { ...world, status: 'draft' as const, objects: [{ ...world.objects[0], source: 'model_proposal_estimate' as const, confirmed_by_user: false, eligible_for_mapping: false }] }
    expect(createSceneWorld(draft).children).toHaveLength(1)
    expect(usableSceneMarker(marker, { ...scene, world_geometry: draft }, known, time)).toBe(false)
  })
  it('注册身份、摄像头、模式、版本和来源均匹配才能使用后台坐标', () => {
    expect(usableSceneMarker(marker, scene, known, time)).toBe(true)
    for (const patch of [{ item_id: 'not-registered' }, { camera_id: 'elsewhere' }, { scene_version: 3 }, { runtime_mode: 'DEMO' }, { source_type: 'video_file' }, { source_type: 'mock' }, { source_type: 'unknown' }, { source_session_id: '' }, { frame_id: -1 }]) {
      expect(usableSceneMarker({ ...marker, ...patch }, scene, known, time)).toBe(false)
    }
    expect(usableSceneMarker(marker, { ...scene, invalid_reason: 'camera_moved' }, known, time)).toBe(false)
  })
  it('unknown/held/空坐标绝不变成原点或桌面位置', () => {
    for (const status of ['unknown', 'held'] as const) expect(usableSceneMarker({ ...marker, position_status: status }, scene, known, time)).toBe(false)
    expect(usableSceneMarker({ ...marker, map_position: null }, scene, known, time)).toBe(false)
    expect(createSceneMarker({ ...marker, map_position: null }, false, 2).children).toHaveLength(0)
  })
  it('区域级位置显示完整后台区域面而不是伪造测量中心点', () => {
    const region: SceneMarker = { ...marker, position_status: 'region_only', map_position: null, region: { surface_id: 'table-a', name: '桌面', approximate: true, world_polygon: [[1, .75, 1], [3, .75, 1], [3, .75, 2], [1, .75, 2]] } }
    expect(usableSceneMarker(region, scene, known, time)).toBe(true)
    const group = createSceneMarker(region, false, 2, time), mesh = group.children[0] as THREE.Mesh
    expect(mesh.geometry).toBeInstanceOf(THREE.BufferGeometry)
    expect(mesh.geometry.getAttribute('position').count).toBe(4)
    expect(mesh.geometry.getIndex()?.count).toBe(6)
    expect(mesh.position.toArray()).toEqual([0, 0, 0]) // World coordinates are the vertices, not a fabricated point mesh.
    expect(group.userData.marker.map_position).toBeNull()
    disposeSceneObjects(group)
  })
  it('人物3秒变灰6秒移除，离线不让旧人物永远在线', () => {
    const person: SceneMarker = { ...marker, item_id: undefined, person_track_id: 'anonymous-1', freshness: 'live', expires_at: new Date(time + 6000).toISOString() }
    expect(usableSceneMarker(person, scene, known, time + 2999)).toBe(true)
    expect(markerIsStale(person, time + 2999)).toBe(false)
    expect(markerIsStale(person, time + 3000)).toBe(true)
    expect(usableSceneMarker(person, scene, known, time + 5999)).toBe(true)
    expect(usableSceneMarker(person, scene, known, time + 6000)).toBe(false)
  })
  it('卸载/刷新释放 Mesh 几何和材质，不累计三维资源', () => {
    const group = createSceneWorld(world), mesh = group.children[0] as THREE.Mesh
    const geometry = vi.spyOn(mesh.geometry, 'dispose'), material = vi.spyOn(mesh.material as THREE.Material, 'dispose')
    disposeSceneObjects(group)
    expect(geometry).toHaveBeenCalledOnce(); expect(material).toHaveBeenCalledOnce(); expect(group.children).toHaveLength(0)
  })
  it('过期标记释放嵌套CSS标签，不残留匿名人物幽灵标签', () => {
    const group = new THREE.Group(), nested = new THREE.Group(), element = document.createElement('span')
    element.textContent = '匿名人物'; document.body.append(element)
    nested.add(new CSS2DObject(element)); group.add(nested)
    disposeSceneObjects(group)
    expect(document.body.contains(element)).toBe(false)
  })
  it('尺寸与独立验证点必须完整，不能用空验证点宣称误差通过', () => {
    const surface = { physical: { width: 1, depth: 1, height: .7, thickness: .05, origin_x: 0, origin_z: 0, rotation_y: 0, unit: 'relative' } } as SceneSurface
    expect(validScenePhysical(surface)).toBe(true)
    expect(validScenePhysical({ ...surface, physical: { ...surface.physical!, width: 0 } })).toBe(false)
    const calibration = { image_points: [[0, 0], [1, 0], [1, 1], [0, 1]] as [number, number][], plane_points: [[0, 0], [1, 0], [1, 1], [0, 1]] as [number, number][], validation_points: [{ image: [NaN, NaN] as [number, number], plane: [NaN, NaN] as [number, number] }] }
    expect(validScenePhysical({ ...surface, calibration })).toBe(false)
  })
})
