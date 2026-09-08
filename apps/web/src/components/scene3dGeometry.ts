import * as THREE from 'three'
import type { SceneDocument, SceneMarker, SceneVector, SceneWorldGeometry } from '../types'

export const worldVectorValid = (value: unknown): value is SceneVector => Array.isArray(value) && value.length === 3
  && value.every(number => typeof number === 'number' && Number.isFinite(number) && Math.abs(number) <= 10000)

const COLORS: Record<string, number> = { floor: 0x9eafa2, table: 0xb79573, sofa: 0x76998a, shelf: 0xc1a782, bed: 0xa8b8c0, other: 0xb5b3ac }
const REAL_SOURCES = new Set(['opencv_camera', 'browser_camera', 'rtsp', 'onvif', 'mjpeg', 'esp32_real', 'authorized_screen_capture'])

/** Geometry consumes server-owned transforms only. It never projects image pixels. */
export function createSceneWorld(geometry: SceneWorldGeometry | null | undefined) {
  const group = new THREE.Group()
  group.name = 'server-confirmed-scene'
  if (geometry?.schema !== 'scene3d_v1' || geometry.coordinate_system !== 'right_handed_y_up' || !Array.isArray(geometry.objects)) return group
  for (const object of geometry.objects.slice(0, 256)) {
    const confirmed = object.source === 'user_confirmed_geometry'
    const draft = geometry.status === 'draft' && object.source === 'model_proposal_estimate' && object.confirmed_by_user === false && object.eligible_for_mapping === false
    if (object.shape !== 'box' || (!confirmed && !draft) || !object.surface_id || !object.id
      || !worldVectorValid(object.position) || !worldVectorValid(object.size) || !object.size.every(value => value > 0)
      || !Number.isFinite(object.rotation_y)) continue
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(...object.size), new THREE.MeshStandardMaterial({
      color: COLORS[object.surface_type] || COLORS.other, roughness: .82, metalness: 0, transparent: draft, opacity: draft ? .72 : 1,
    }))
    mesh.name = object.id
    mesh.position.fromArray(object.position)
    mesh.rotation.y = THREE.MathUtils.degToRad(object.rotation_y)
    mesh.userData = { surfaceId: object.surface_id, part: object.part, estimated: object.estimated, source: object.source }
    mesh.castShadow = true; mesh.receiveShadow = true
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry), new THREE.LineBasicMaterial({ color: 0x435449, transparent: true, opacity: .22 }))
    mesh.add(edges)
    group.add(mesh)
  }
  return group
}

export function usableSceneMarker(marker: SceneMarker, scene: SceneDocument, knownItemIds: ReadonlySet<string>, now = Date.now()) {
  if (scene.invalid_reason || scene.world_geometry?.status === 'draft' || ['needs_review', 'needs_confirmation', 'pending_delete', 'validation_failed'].includes(scene.calibration_status)
    || marker.camera_id !== scene.camera_id || marker.scene_version !== scene.scene_version
    || marker.runtime_mode !== scene.runtime_mode || marker.is_simulated !== scene.is_simulated
    || !marker.source_session_id || typeof marker.frame_id !== 'number' || !Number.isInteger(marker.frame_id) || marker.frame_id < 0
    || !marker.observation_timestamp || !Number.isFinite(Date.parse(marker.observation_timestamp)) || !marker.mapping_method) return false
  if (marker.runtime_mode === 'REAL' && (marker.is_simulated || !REAL_SOURCES.has(marker.source_type))) return false
  if (marker.item_id ? !knownItemIds.has(marker.item_id) : !marker.person_track_id) return false
  if (marker.person_track_id && (!marker.expires_at || !Number.isFinite(Date.parse(marker.expires_at)) || now >= Date.parse(marker.expires_at))) return false
  if (['mapped', 'mapped_unvalidated', 'stale'].includes(marker.position_status) && worldVectorValid(marker.map_position)) return true
  return ['region_only', 'stale'].includes(marker.position_status) && !marker.map_position && marker.region?.approximate === true
    && Array.isArray(marker.region.world_polygon) && marker.region.world_polygon.length >= 3 && marker.region.world_polygon.length <= 32 && marker.region.world_polygon.every(worldVectorValid)
}

export function markerIsStale(marker: SceneMarker, now = Date.now()) {
  return marker.position_status === 'stale' || marker.freshness === 'stale'
    || Boolean(marker.person_track_id && marker.observation_timestamp && now >= Date.parse(marker.observation_timestamp) + 3000)
}

export function createSceneMarker(marker: SceneMarker, selected: boolean, sceneScale: number, now = Date.now()) {
  const group = new THREE.Group()
  const stale = markerIsStale(marker, now)
  const uncertain = marker.position_status !== 'mapped'
  const color = stale ? 0x84938c : selected ? 0xe1893e : marker.person_track_id ? 0x4e93bb : uncertain ? 0xc19a41 : 0x309979
  const material = new THREE.MeshStandardMaterial({ color, transparent: stale || marker.position_status === 'region_only', opacity: stale ? .45 : .7 })
  const size = Math.max(.015, Math.min(.15, sceneScale * .025))
  if (['region_only', 'stale'].includes(marker.position_status) && !marker.map_position && marker.region?.world_polygon) {
    const points = marker.region.world_polygon
    const vertices = points.flatMap(([x, y, z]) => [x, y + size * .06, z])
    const indices = Array.from({ length: points.length - 2 }, (_, index) => [0, index + 1, index + 2]).flat()
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3)); geometry.setIndex(indices); geometry.computeVertexNormals()
    material.side = THREE.DoubleSide; material.depthWrite = false
    group.add(new THREE.Mesh(geometry, material))
    // The label describes the entire backend-supplied region, never a measured point.
    group.userData.labelPosition = new THREE.Vector3(...points.reduce((total, point) => total.map((v, i) => v + point[i] / points.length) as SceneVector, [0, 0, 0] as SceneVector))
  } else if (worldVectorValid(marker.map_position)) {
    const mesh = new THREE.Mesh(marker.person_track_id ? new THREE.CapsuleGeometry(size * .4, size * 1.8, 3, 8) : new THREE.OctahedronGeometry(size), material)
    mesh.position.fromArray(marker.map_position)
    mesh.position.y += size * (marker.person_track_id ? 1.3 : 1.2)
    group.add(mesh)
    group.userData.labelPosition = mesh.position.clone().add(new THREE.Vector3(0, size * 1.5, 0))
  } else material.dispose()
  group.userData = { ...group.userData, itemId: marker.item_id, personTrackId: marker.person_track_id, surfaceId: marker.region?.surface_id, marker }
  for (const child of group.children) child.userData = group.userData
  return group
}

export function disposeSceneObjects(root: THREE.Object3D) {
  root.traverse(object => {
    const label = object as THREE.Object3D & { isCSS2DObject?: boolean; element?: HTMLElement }
    // Removing a parent Group does not dispatch "removed" to its nested labels.
    // Explicitly unlink their DOM, otherwise expired people leave ghost labels.
    if (label.isCSS2DObject) label.element?.remove()
    if (object instanceof THREE.Mesh || object instanceof THREE.LineSegments) {
      object.geometry.dispose()
      for (const material of Array.isArray(object.material) ? object.material : [object.material]) material.dispose()
    }
  })
  root.clear()
}
