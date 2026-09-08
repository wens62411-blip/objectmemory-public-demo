import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js'
import type { Item, SceneDocument, SceneMarker } from '../types'
import { createSceneMarker, createSceneWorld, disposeSceneObjects, markerIsStale, usableSceneMarker } from './scene3dGeometry'
import './Scene3D.css'

type ViewAction = 'top' | 'side' | 'perspective' | 'zoom-in' | 'zoom-out' | 'left' | 'right' | 'reset'
interface Props { scene: SceneDocument; markers: SceneMarker[]; persons: SceneMarker[]; items: Item[]; selectedItemId: string | null; onSelectItem: (id: string) => void }

/** A real world-space renderer. Viewer motion never calls a calibration/mutation API. */
export default function Scene3D(props: Props) {
  const mount = useRef<HTMLDivElement>(null)
  const latest = useRef(props); latest.current = props
  const command = useRef<(action: ViewAction) => void>()
  const refreshMarkers = useRef<() => void>()
  const [error, setError] = useState('')
  const [ready, setReady] = useState(false)
  const geometryKey = JSON.stringify(props.scene.world_geometry || null)

  useEffect(() => {
    const host = mount.current
    if (!host) return
    setError(''); setReady(false)
    const world = createSceneWorld(latest.current.scene.world_geometry)
    if (!world.children.length) { if (latest.current.scene.world_geometry?.objects.length) setError('服务端几何格式或来源无效，已停止显示；没有创建替代模型。'); disposeSceneObjects(world); return }
    const canvas = document.createElement('canvas')
    let renderer: THREE.WebGLRenderer
    try {
      const context = canvas.getContext('webgl2', { antialias: true, alpha: false })
      if (!context) throw new Error('WebGL 2 unavailable')
      renderer = new THREE.WebGLRenderer({ canvas, context, antialias: true })
    } catch {
      disposeSceneObjects(world)
      setError('此浏览器没有可用的 WebGL 2，三维视图未启动。请开启硬件加速或换用支持 WebGL 2 的浏览器；没有用照片替代三维。')
      return
    }
    const stage = new THREE.Scene()
    const dark = window.matchMedia('(prefers-color-scheme: dark)')
    stage.background = new THREE.Color(dark.matches ? 0x15251f : 0xeef1e9)
    stage.add(world, new THREE.HemisphereLight(0xf7fbff, 0x6c7765, 2.5))
    const light = new THREE.DirectionalLight(0xfff5df, 3)
    light.position.set(4, 9, 5); stage.add(light)
    const bounds = new THREE.Box3().setFromObject(world)
    const center = bounds.getCenter(new THREE.Vector3())
    const scale = Math.max(.1, bounds.getSize(new THREE.Vector3()).length())
    const camera = new THREE.PerspectiveCamera(42, 1, Math.max(.001, scale / 1000), scale * 100)
    const controls = new OrbitControls(camera, canvas)
    controls.enableDamping = false; controls.minDistance = scale * .08; controls.maxDistance = scale * 12
    controls.target.copy(center)
    canvas.tabIndex = 0; canvas.setAttribute('aria-label', '交互式三维场景：拖动旋转，滚轮缩放，右键或双指平移，方向键平移')
    controls.listenToKeyEvents(canvas)
    const labels = new CSS2DRenderer()
    labels.domElement.className = 'scene3d-label-layer'
    host.append(canvas, labels.domElement)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    let frame = 0, disposed = false
    const render = () => {
      if (disposed || frame || document.visibilityState === 'hidden') return
      frame = requestAnimationFrame(() => { frame = 0; if (!disposed) { renderer.render(stage, camera); labels.render(stage, camera) } })
    }
    const resize = () => {
      const width = Math.max(host.clientWidth, 1), height = Math.max(host.clientHeight, 1)
      renderer.setSize(width, height); labels.setSize(width, height)
      camera.aspect = width / height; camera.updateProjectionMatrix(); render()
    }
    const view = (action: ViewAction) => {
      if (action === 'zoom-in') controls.dollyIn(1.25)
      else if (action === 'zoom-out') controls.dollyOut(1.25)
      else if (action === 'left') controls.rotateLeft(Math.PI / 8)
      else if (action === 'right') controls.rotateLeft(-Math.PI / 8)
      else {
        controls.target.copy(center)
        const offset = action === 'top' ? new THREE.Vector3(0, 1.5, .001) : action === 'side' ? new THREE.Vector3(1.5, .08, 0) : new THREE.Vector3(1, .85, 1.1)
        camera.position.copy(center).add(offset.multiplyScalar(scale))
      }
      controls.update(); render()
    }
    command.current = view
    controls.addEventListener('change', render)
    const markerGroup = new THREE.Group(); stage.add(markerGroup)
    const knownItems = () => new Set(latest.current.items.map(item => item.id))
    let lastMarkerKey = ''
    const updateMarkers = () => {
      const current = latest.current, now = Date.now()
      const valid = [...current.markers, ...current.persons].filter(marker => usableSceneMarker(marker, current.scene, knownItems(), now))
      const key = JSON.stringify([valid, current.selectedItemId, valid.map(marker => markerIsStale(marker, now))])
      if (lastMarkerKey === key) return
      lastMarkerKey = key; disposeSceneObjects(markerGroup)
      for (const marker of valid) {
        const group = createSceneMarker(marker, marker.item_id === current.selectedItemId, scale, now)
        const label = document.createElement('span'); label.className = `scene3d-label${markerIsStale(marker, now) ? ' stale' : ''}`
        const name = marker.item_id ? current.items.find(item => item.id === marker.item_id)?.name || '注册物品' : '匿名人物'
        label.textContent = `${name}${marker.region && !marker.map_position ? ' · 仅区域' : marker.position_status === 'mapped_unvalidated' ? ' · 未独立验证' : ''}${markerIsStale(marker, now) ? ' · 已过时' : ''}`
        const object = new CSS2DObject(label)
        if (group.userData.labelPosition instanceof THREE.Vector3) object.position.copy(group.userData.labelPosition)
        group.add(object); markerGroup.add(group)
      }
      render()
    }
    refreshMarkers.current = updateMarkers
    const raycaster = new THREE.Raycaster()
    let down: { x: number; y: number } | null = null
    const pointerDown = (event: PointerEvent) => { down = { x: event.clientX, y: event.clientY } }
    const pointerUp = (event: PointerEvent) => {
      if (!down || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5) { down = null; return }
      down = null
      const rect = canvas.getBoundingClientRect()
      raycaster.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), camera)
      const hit = raycaster.intersectObjects(markerGroup.children, true).find(entry => entry.object.userData.itemId)
      if (hit) latest.current.onSelectItem(hit.object.userData.itemId)
    }
    const theme = () => { stage.background = new THREE.Color(dark.matches ? 0x15251f : 0xeef1e9); render() }
    const contextLost = (event: Event) => { event.preventDefault(); setReady(false); setError('三维图形资源暂时不可用，正在等待浏览器恢复；最后位置记录未修改。') }
    const contextRestored = () => { setReady(true); setError(''); render() }
    canvas.addEventListener('pointerdown', pointerDown); canvas.addEventListener('pointerup', pointerUp)
    canvas.addEventListener('webglcontextlost', contextLost); canvas.addEventListener('webglcontextrestored', contextRestored)
    dark.addEventListener('change', theme); document.addEventListener('visibilitychange', render)
    const observer = new ResizeObserver(resize); observer.observe(host)
    const expiryTimer = window.setInterval(() => { if (document.visibilityState !== 'hidden') updateMarkers() }, 500)
    resize(); view('perspective'); updateMarkers(); setReady(true)
    return () => {
      disposed = true; cancelAnimationFrame(frame); clearInterval(expiryTimer); observer.disconnect()
      command.current = undefined; refreshMarkers.current = undefined
      canvas.removeEventListener('pointerdown', pointerDown); canvas.removeEventListener('pointerup', pointerUp)
      canvas.removeEventListener('webglcontextlost', contextLost); canvas.removeEventListener('webglcontextrestored', contextRestored)
      dark.removeEventListener('change', theme); document.removeEventListener('visibilitychange', render)
      controls.removeEventListener('change', render); controls.dispose()
      disposeSceneObjects(markerGroup); disposeSceneObjects(world); stage.clear()
      renderer.dispose(); renderer.forceContextLoss(); canvas.remove(); labels.domElement.remove()
    }
  }, [geometryKey])
  useEffect(() => { refreshMarkers.current?.() }, [props.markers, props.persons, props.selectedItemId, props.items, props.scene])

  const hasGeometry = Boolean(props.scene.world_geometry?.objects.length)
  return <section className="scene3d" aria-label="真实三维几何视图">
    {props.scene.world_geometry?.status === 'draft' && <p className="scene-message" role="status">由家具候选估计，尺寸 / 深度未确认，不用于位置映射。请在下方采用候选并核对尺寸。</p>}
    <div className="scene3d-toolbar" aria-label="三维视角控制">{([['perspective', '斜视'], ['top', '俯视'], ['side', '侧视'], ['zoom-in', '放大'], ['zoom-out', '缩小'], ['left', '左转'], ['right', '右转'], ['reset', '复位']] as const).map(([action, label]) => <button type="button" key={action} disabled={!ready} onClick={() => command.current?.(action)}>{label}</button>)}</div>
    <div ref={mount} className="scene3d-viewport" data-testid="scene3d-viewport">
      {!hasGeometry && <div className="scene3d-empty"><strong>还没有已确认的三维家具</strong><p>展开下方校准，输入尺寸与支撑面高度；保存后由服务端生成实体几何。不会预置一个房间。</p></div>}
      {error && <p role="alert" className="scene3d-empty">{error}</p>}
    </div>
    <p className="scene3d-help">拖动旋转 · 滚轮缩放 · 右键 / 双指平移。视角操作不改变标定。{props.scene.world_geometry?.unit === 'm' ? '尺寸采用用户输入的米，非自动实测。' : '相对单位：形状和比例由用户确认，不代表实际米数。'}</p>
    <div className="scene3d-legend"><span>◆ 物品位置符号（不是物品外形重建）</span><span>蓝色：匿名人物</span><span>黄色：未经独立点验证 / 仅区域</span><span>灰色：过时位置</span></div>
  </section>
}
