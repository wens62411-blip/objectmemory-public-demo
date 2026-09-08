import { expect, test, type Page, type Route } from '@playwright/test'
import type { ReferenceImage, RecognitionProfile } from '../../src/types'

const phone = { id: 'phone-1', name: '我的手机', type: '手机', description: '', owner: '我', color: '黑色', features: '透明壳', aliases: ['手机'], aruco_id: 1, ring_enabled: true }
const camera = { id: 'camera-1', name: '客厅演示回放', room_name: '客厅', installation: '电视柜', source_type: 'video', source_type_provenance: 'video_file', source: 'demo.avi', config: { loop: true, simulated: true }, enabled: true, inference_fps: 5, save_clips: true, runtime_mode: 'DEMO', is_simulated: true, health: { status: 'ready', fps: 5.1, latency_ms: 18, dropped_frames: 0, reconnects: 0, width: 640, height: 480 } }
const placed = { id: 'event-1', event_id: 'event-1', item_id: phone.id, item_name: phone.name, event_type: 'placed', camera_id: camera.id, camera_name: camera.name, room_name: '客厅', zone_id: 'zone-2', zone_name: '沙发右侧', timestamp_start: '2026-09-03T08:32:00Z', timestamp_end: '2026-09-03T08:32:03Z', confidence: .95, evidence_type: 'test_replay', evidence_status: 'confirmed', final_status: 'confirmed_placed', previous_zone: '桌面', new_zone: '沙发右侧', screenshot_path: '/media/event-images/e1.jpg', clip_path: '/media/event-clips/e1.mp4', detection_mode: 'aruco', runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true, human_review_status: 'unreviewed', notes: null }
const unverifiedDevice = { id: 'network-claim-1', device_id: 'network-claim-1', device_name: '待验证 ESP32', room_name: '客厅', online: true, simulated: false, hardware_type: 'unverified', source_type: 'esp32_unverified', camera_id: 'must-not-open', firmware_version: 'unknown' }

function json(route: Route, body: unknown, status = 200) { return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }) }

async function mockApi(page: Page) {
  const state = { cameras: [camera], items: [phone], events: [placed], jobReads: 0 }
  // In-memory UI contracts only: no actual feature encoder or successful identity.
  const references: Record<string, ReferenceImage[]> = {}
  const profiles: Record<string, RecognitionProfile> = {}
  const profileFor = (itemId: string): RecognitionProfile => profiles[itemId] || { registration_status: references[itemId]?.length ? 'images_saved' : 'needs_photos', profile_version: references[itemId]?.length ? 1 : 0, reference_count: references[itemId]?.length || 0, ready_reference_count: 0, loaded_profile_version: null, loaded_camera_ids: [], model_id: null, model_version: null, error: null }
  await page.route('**/api/**', async (route) => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname; const method = request.method()
    if (path === '/api/runtime-config') return json(route, { runtime_mode: 'DEMO', mode: 'DEMO', backend_healthy: true, source_type: 'service', is_simulated: true })
    if (path === '/api/session') return json(route, { authenticated: true, local: true, lan_urls: ['http://192.168.1.8:8029'] })
    if (path === '/api/health') return json(route, { status: 'ok', version: '0.1.0', stats: { cameras: state.cameras.length, online_cameras: 1, items: state.items.length, events: state.events.length, today_events: state.events.length, fps: 5.1, latency_ms: 18 } })
    if (path === '/api/cameras' && method === 'GET') return json(route, state.cameras)
    if (path === '/api/cameras' && method === 'POST') { const input = request.postDataJSON(); const created = { ...camera, ...input, id: `camera-${state.cameras.length + 1}`, health: { status: 'stopped', fps: 0 } }; state.cameras.push(created); return json(route, created, 201) }
    if (/^\/api\/cameras\/[^/]+$/.test(path) && method === 'PATCH') { const found = state.cameras.find((item) => path.endsWith(item.id)); Object.assign(found || {}, request.postDataJSON()); return json(route, found) }
    if (/\/api\/cameras\/[^/]+\/test$/.test(path)) return json(route, { success: true, status: 'ready', fps: 5.2, width: 640, height: 480, latency_ms: 15, dropped_frames: 0, reconnects: 0, message: '已读取到画面' })
    if (/\/api\/cameras\/[^/]+\/(start|stop)$/.test(path)) return json(route, { status: 'ok', message: '操作完成' })
    if (/\/api\/cameras\/[^/]+\/zones$/.test(path) && method === 'GET') return json(route, [{ id: 'zone-1', camera_id: camera.id, name: '桌面', points: [[.05,.05],[.48,.05],[.48,.9],[.05,.9]], priority: 10, enabled: true }])
    if (/\/api\/cameras\/[^/]+\/zones$/.test(path) && method === 'POST') return json(route, { id: 'zone-new', camera_id: camera.id, ...request.postDataJSON() }, 201)
    if (/\/api\/cameras\/[^/]+\/frame$/.test(path)) return route.fulfill({ status: 200, contentType: 'image/jpeg', body: Buffer.from('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k=', 'base64') })
    if (/\/api\/cameras\/[^/]+\/stream$/.test(path)) return route.fulfill({ status: 404 })
    if (path === '/api/items' && method === 'GET') return json(route, state.items.map((item) => ({ ...item, reference_images: references[item.id] || [], recognition_profile: profileFor(item.id) })))
    if (path === '/api/items' && method === 'POST') { const created = { id: `item-${state.items.length + 1}`, ...request.postDataJSON() }; state.items.push(created); return json(route, created, 201) }
    const itemId = path.match(/^\/api\/items\/([^/]+)/)?.[1]
    if (itemId && /^\/api\/items\/[^/]+$/.test(path) && method === 'GET') {
      const item = state.items.find((value) => value.id === itemId)
      return item ? json(route, { ...item, reference_images: references[itemId] || [], recognition_profile: profileFor(itemId) }) : json(route, { detail: '物品不存在' }, 404)
    }
    if (itemId && /^\/api\/items\/[^/]+\/reference-images$/.test(path) && method === 'POST') {
      const count = (request.postDataBuffer()?.toString('latin1').match(/name="files"; filename=/g) || []).length
      if (count < 1 || count > 12) return json(route, { detail: '请选择 1 至 12 张照片' }, 422)
      const saved: ReferenceImage[] = Array.from({ length: count }, (_, index) => ({ id: `${itemId}-ref-${index + 1}`, path: `/media/registered-items/${itemId}-ref-${index + 1}.png`, width: 320, height: 240, region: null, region_confirmed: false, suggested_regions: [], status: 'image_saved' }))
      references[itemId] = saved
      profiles[itemId] = { ...profileFor(itemId), registration_status: 'images_saved', profile_version: 1, reference_count: saved.length }
      return json(route, saved)
    }
    if (itemId && /^\/api\/items\/[^/]+\/reference-images\/[^/]+$/.test(path) && method === 'PATCH') {
      const reference = references[itemId]?.find((value) => path.endsWith(`/${value.id}`))
      if (!reference) return json(route, { detail: '参考照片不存在' }, 404)
      const body = request.postDataJSON()
      if (body.confirmed !== true) return json(route, { detail: '必须明确确认目标' }, 422)
      reference.region = body.region; reference.region_confirmed = true; reference.status = 'region_confirmed'
      profiles[itemId] = { ...profileFor(itemId), registration_status: 'images_saved', profile_version: 2 }
      return json(route, reference)
    }
    if (itemId && path.endsWith('/profile') && method === 'GET') return json(route, profileFor(itemId))
    if (itemId && path.endsWith('/profile/build') && method === 'POST') {
      profiles[itemId] = { ...profileFor(itemId), registration_status: 'failed', error: 'Mock 合同测试：本地外观模型未准备，没有建立特征或身份结果。' }
      return json(route, { detail: profiles[itemId].error }, 422)
    }
    if (/\/api\/items\/[^/]+\/last-location$/.test(path)) return json(route, { item: phone, status: 'placed', answer: '最后确认在沙发右侧', last_confirmed: placed, last_seen: placed, last_picked_up: null, last_occluded: null, last_exited: null, evidence: placed })
    if (path === '/api/items/phone-1/ring') return json(route, { delivered: true })
    if (path === '/api/items/phone-1/ring/stop') return json(route, { delivered: true })
    if (path === '/api/events') return json(route, state.events)
    if (/\/api\/events\/[^/]+\/correct$/.test(path)) { const correction = { ...placed, id: 'event-correction', event_type: 'manual_correction', ...request.postDataJSON() }; state.events.unshift(correction); return json(route, correction, 201) }
    if (path === '/api/search') return json(route, { query: '我的手机在哪里', results: [{ item: phone, status: 'placed', answer: '我的手机最后一次确认放在客厅 · 沙发右侧。', last_confirmed: placed, last_seen: placed, last_picked_up: null, last_occluded: null, last_exited: null, evidence: placed }] })
    if (path === '/api/settings' && method === 'GET') return json(route, { detection_mode: 'aruco', inference_fps: 5, static_seconds: 2, pre_seconds: 5, post_seconds: 5, retention_days: 7, save_clips: true, show_hands: true, privacy_mode: true, record_events: true })
    if (path === '/api/firmware/ports') return json(route, { platformio: { available: true, version: '6.1.19', python: 'Python 3.12' }, ports: [{ device: 'COM5', description: 'Bluetooth 串口', eligible: false, rejection_reason: '不是可刷写开发板' }] })
    if (path === '/api/firmware/status') return json(route, { source_present: true, compile_passed: false, manifest_hashes_verified: false, physical_board_detected: false, physical_flash_performed: false, serial_provision_performed: false, real_video_verified: false })
    if (path === '/api/virtual-device/status') return json(route, { running: false, source: 'video', backend_online: false })
    if (path === '/api/firmware/build' && method === 'POST') return json(route, { id: 'job-1', job_type: 'build', status: 'queued', progress: 0, stage: '等待执行', logs: [], error: null })
    if (path === '/api/firmware/jobs/job-1') { state.jobReads += 1; return json(route, state.jobReads > 0 ? { id: 'job-1', job_type: 'build', status: 'succeeded', progress: 100, stage: '编译完成', logs: ['PlatformIO Core 6.1.19', 'SUCCESS'], error: null, result: { message: '固件产物已生成。' } } : { id: 'job-1', job_type: 'build', status: 'running', progress: 50, stage: '编译中', logs: ['Compiling…'], error: null }) }
    if (path === '/api/devices') return json(route, [unverifiedDevice])
    return json(route, { detail: `未模拟 ${method} ${path}` }, 404)
  })
  await page.route('**/media/**', (route) => route.fulfill({ status: 200, contentType: route.request().url().endsWith('.mp4') ? 'video/mp4' : 'image/jpeg', body: Buffer.from('') }))
}

test.describe('ui-mock：仅验证界面交互，不得计为真实链路 E2E', () => {
test.beforeEach(async ({ page }) => { await mockApi(page); await page.goto('/') })

test('模拟 API 界面契约：从首页查找手机并展示证据状态', async ({ page }) => {
  await page.getByRole('button', { name: '我的手机在哪里' }).click()
  await expect(page).toHaveURL(/\/search\?q=/)
  await expect(page.getByText('客厅 · 沙发右侧').first()).toBeVisible()
  await expect(page.getByText('测试视频').first()).toBeVisible()
  await expect(page.getByText(/可信度 95%/)).toBeVisible()
  await page.getByRole('button', { name: /让手机响铃/ }).click()
  await expect(page.getByText('响铃指令已送达手机伴侣')).toBeVisible()
})

test('模拟 API 界面契约：添加摄像头必须先通过连接测试', async ({ page }) => {
  await page.getByRole('link', { name: '摄像头', exact: true }).click()
  await page.getByRole('button', { name: /添加摄像头/ }).first().click()
  await expect(page.getByText('选择来源', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /下一步：测试连接/ }).click()
  await expect(page.getByRole('button', { name: /连接成功，继续/ })).toBeDisabled()
  await page.getByRole('dialog').getByRole('button', { name: '测试连接' }).click()
  await expect(page.getByText('已读取到摄像头画面；当前 DEMO 事件仍标记为模拟').first()).toBeVisible()
  await page.getByRole('button', { name: /连接成功，继续/ }).click()
  await page.getByLabel('摄像头名称').fill('书房 USB 摄像头')
  await page.getByLabel('房间名称').fill('书房')
  await page.getByRole('button', { name: /保存并划分区域/ }).click()
  await expect(page.getByRole('heading', { name: '书房 · 书房 USB 摄像头' })).toBeVisible()
  await expect(page.getByRole('link', { name: /现在划分区域/ })).toBeVisible()
})

test('模拟 API 界面契约：单张照片无标签注册、确认目标、模型未准备不冒充可识别', async ({ page }) => {
  const businessWrites: Array<{ path: string; body: string | null }> = []
  page.on('request', (request) => { if (request.method() !== 'GET' && new URL(request.url()).pathname.startsWith('/api/')) businessWrites.push({ path: new URL(request.url()).pathname, body: request.postData() }) })
  // This generated PNG is a decodable UI fixture, not a physical wallet photo.
  const pngDataUrl = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 320; canvas.height = 240
    const context = canvas.getContext('2d')!
    context.fillStyle = '#ece9df'; context.fillRect(0, 0, 320, 240)
    context.fillStyle = '#32617f'; context.fillRect(60, 50, 190, 140)
    return canvas.toDataURL('image/png')
  })
  const png = Buffer.from(pngDataUrl.split(',')[1], 'base64')
  await page.route('**/media/registered-items/**', (route) => route.fulfill({ status: 200, contentType: 'image/png', body: png }))
  await page.getByRole('link', { name: '物品管理' }).click()
  await page.getByRole('button', { name: '添加物品' }).click()
  await page.getByLabel('物品名称').fill('蓝色钱包')
  await page.getByLabel('类型').selectOption('钱包')
  await expect(page.getByLabel('ArUco 标签编号')).not.toBeVisible()
  await page.getByLabel('选择参考照片').setInputFiles({ name: 'wallet-ui-fixture.png', mimeType: 'image/png', buffer: png })
  await expect(page.getByText('1 / 12 张')).toBeVisible()
  await page.getByRole('button', { name: '保存物品' }).click()
  const dialog = page.getByRole('dialog', { name: '蓝色钱包 · 照片与识别' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('照片已保存 · 等待建立档案')).toBeVisible()
  await expect(dialog.getByText('可识别 · 已加载当前档案')).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '建立 / 更新识别档案' })).toBeDisabled()
  expect(businessWrites.filter((write) => write.path.endsWith('/profile/build'))).toHaveLength(0)
  const created = businessWrites.find((write) => write.path === '/api/items')!
  expect(JSON.parse(created.body!).aruco_id).toBeNull()
  const upload = businessWrites.find((write) => write.path.endsWith('/reference-images'))!
  expect(upload.body?.match(/name="files"; filename=/g)).toHaveLength(1)
  await expect(dialog.getByAltText('待确认的原始参考照片')).toHaveJSProperty('naturalWidth', 320)
  await dialog.getByLabel('左边距（%）').fill('10')
  await dialog.getByLabel('上边距（%）').fill('10')
  await dialog.getByLabel('宽度（%）').fill('60')
  await dialog.getByLabel('高度（%）').fill('70')
  await dialog.getByRole('button', { name: '确认这是我的物品' }).click()
  await expect(dialog.getByText('目标区域已确认。请建立或更新识别档案，让新照片参与匹配。')).toBeVisible()
  const patch = businessWrites.find((write) => /\/reference-images\/[^/]+$/.test(write.path))!
  expect(JSON.parse(patch.body!)).toEqual({ region: [.1, .1, .6, .7], confirmed: true })
  await dialog.getByRole('button', { name: '建立 / 更新识别档案' }).click()
  await expect(dialog.getByText('Mock 合同测试：本地外观模型未准备，没有建立特征或身份结果。')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '测试当前画面识别' })).toBeDisabled()
  await expect(dialog.getByText('可识别 · 已加载当前档案')).toHaveCount(0)
  expect(businessWrites.filter((write) => write.path.endsWith('/recognition-test'))).toHaveLength(0)
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(page.getByRole('heading', { name: '蓝色钱包', exact: true })).toBeVisible()
})

test('模拟 API 界面契约：事件纠正保存为新证据而不是覆盖原记录', async ({ page }) => {
  await page.getByRole('link', { name: '事件时间线' }).click()
  await expect(page.getByRole('heading', { name: /我的手机 · 确认放下/ })).toBeVisible()
  await page.getByRole('button', { name: /纠正位置/ }).click()
  await page.getByLabel('区域').fill('沙发左侧')
  await page.getByLabel('备注').fill('现场人工确认')
  await page.getByRole('button', { name: '保存人工纠正' }).click()
  await expect(page.getByText('人工纠正已作为新证据保存')).toBeVisible()
})

test('模拟 API 界面契约：无开发板时禁用刷写并呈现编译 job 状态', async ({ page }) => {
  await page.getByText('高级工具', { exact: true }).click()
  await page.getByRole('link', { name: '设备中心', exact: true }).click()
  await expect(page.getByText(/当前没有检测到可刷写硬件/)).toBeVisible()
  await expect(page.getByText(/当前 DEMO 模式不允许 USB 刷写、安装或物理设备配网/)).toBeVisible()
  await expect(page.getByRole('button', { name: '一键安装并绑定' })).toBeDisabled()
  await expect(page.getByText('设备类型未验证').first()).toBeVisible()
  await expect(page.getByText('在线 · 类型未验证')).toBeVisible()
  await expect(page.getByRole('link', { name: /查看.*画面/ })).toHaveCount(0)
  await page.getByRole('button', { name: /备用热点网络申领/ }).click()
  await expect(page.getByText(/不能证明物理硬件，也不能生成 REAL 确认事件/)).toBeVisible()
  await expect(page.getByText(/仍需在本机通过 USB 串口收到 OMREADY/)).toBeVisible()
  await expect(page.getByRole('button', { name: '生成 10 分钟配对码' })).toBeDisabled()
  await page.getByRole('button', { name: '只编译固件' }).click()
  await expect(page.getByText('固件编译任务已完成')).toBeVisible({ timeout: 5000 })
  await expect(page.getByText('SUCCESS')).toBeVisible()
})

test('模拟 API 界面契约：窄屏使用底部导航并可打开完整菜单', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload()
  await expect(page.getByRole('navigation', { name: '移动端主导航' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /东西放哪了/ })).toBeVisible()
  await page.getByRole('navigation', { name: '移动端主导航' }).getByRole('button', { name: '更多' }).click()
  await page.locator('.advanced-nav summary').click()
  await expect(page.getByRole('link', { name: '设备中心', exact: true })).toBeVisible()
  await expect(page.locator('.sidebar').getByRole('link', { name: /连接手机/ })).toHaveAttribute('href', '/settings#mobile-access')
})
})
