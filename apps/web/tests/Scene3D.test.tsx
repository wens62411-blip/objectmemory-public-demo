import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Scene3D from '../src/components/Scene3D'
import * as geometry from '../src/components/scene3dGeometry'
import type { Item, SceneDocument, SceneMarker } from '../src/types'
import type { Camera, Scene } from 'three'

// Real geometry and labels; stub only the GPU renderer in jsdom.
vi.mock('three', async (load) => ({
  ...await load<typeof import('three')>(),
  WebGLRenderer: class {
    setPixelRatio() {} setSize() {} dispose() {} forceContextLoss() {}
    render(scene: Scene, camera: Camera) { scene.updateMatrixWorld(); camera.updateMatrixWorld() }
  },
}))
const now = Date.parse('2026-09-06T10:00:00Z')
const scene = {
  camera_id: 'cam', scene_version: 1, runtime_mode: 'REAL', is_simulated: false, calibration_status: 'calibrated',
  world_geometry: { schema: 'scene3d_v1', coordinate_system: 'right_handed_y_up', status: 'confirmed', unit: 'm', objects: [
    { id: 'table', surface_id: 'table', source: 'user_confirmed_geometry', shape: 'box', surface_type: 'table', position: [0, .7, 0], size: [1, .1, 1], rotation_y: 0 },
  ] },
} as SceneDocument
const marker: SceneMarker = { item_id: 'phone', camera_id: 'cam', scene_version: 1, runtime_mode: 'REAL', is_simulated: false, source_type: 'opencv_camera', source_session_id: 's1', frame_id: 1, observation_timestamp: new Date(now).toISOString(), mapping_method: 'homography', position_status: 'mapped', map_position: [.2, .75, .2] }
const props = { scene, items: [{ id: 'phone', name: '手机' } as Item], markers: [marker], persons: [] as SceneMarker[], selectedItemId: null, onSelectItem: vi.fn() }

describe('三维标记资源更新契约（不是GPU或实物识别验收）', () => {
  beforeEach(() => {
    vi.useFakeTimers(); vi.setSystemTime(now)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as WebGL2RenderingContext)
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(Date.now()), 0))
    vi.stubGlobal('cancelAnimationFrame', clearTimeout)
  })
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('更新来源帧不重建静止标记，移动或改名仍更新；来源失效立即移除', () => {
    const create = vi.spyOn(geometry, 'createSceneMarker')
    const validate = vi.spyOn(geometry, 'usableSceneMarker')
    const view = render(<Scene3D {...props} />)
    act(() => { vi.advanceTimersByTime(1) })
    expect(create).toHaveBeenCalledTimes(1)
    const checks = validate.mock.calls.length
    act(() => { vi.advanceTimersByTime(5000) })
    expect(validate).toHaveBeenCalledTimes(checks)
    const newer = { ...marker, frame_id: 2, observation_timestamp: new Date(now + 1000).toISOString() }
    view.rerender(<Scene3D {...props} markers={[newer]} />)
    expect(create).toHaveBeenCalledTimes(1)
    view.rerender(<Scene3D {...props} markers={[{ ...newer, map_position: [.4, .75, .2] }]} />)
    expect(create).toHaveBeenCalledTimes(2)
    view.rerender(<Scene3D {...props} items={[{ ...props.items[0], name: '新名称' }]} />)
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.getByText('新名称')).toBeInTheDocument()
    expect(create).toHaveBeenCalledTimes(3)
    view.rerender(<Scene3D {...props} markers={[{ ...marker, source_type: 'mock' }]} />)
    expect(screen.queryByText('新名称')).not.toBeInTheDocument()
    expect(create).toHaveBeenCalledTimes(3)
  })

  it('匿名人物仍在三秒后变灰、六秒到期移除', () => {
    const person = { ...marker, item_id: undefined, person_track_id: 'person', expires_at: new Date(now + 6000).toISOString() }
    render(<Scene3D {...props} markers={[]} persons={[person]} />)
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.getByText('匿名人物')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.getByText('匿名人物 · 已过时')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.queryByText('匿名人物 · 已过时')).not.toBeInTheDocument()
  })
})
