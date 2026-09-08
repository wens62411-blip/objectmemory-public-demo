import { expect, test } from '@playwright/test'
import { execFile } from 'node:child_process'
import { resolve } from 'node:path'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../..')
async function countRows(path: string) {
  const { stdout } = await promisify(execFile)(resolve(root, '.venv/Scripts/python.exe'), ['-B', '-c',
    "import json,sqlite3,sys;from pathlib import Path;c=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True);print(json.dumps({t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('movement_events','item_current_state','event_media','scenes')}));c.close()", path], { cwd: root })
  return JSON.parse(stdout)
}

test('语音输入硬件替身 → 真实FastAPI搜索 → 独立TEST SQLite无数据回答；非真实麦克风验收', async ({ page, request }) => {
  expect((await request.get('/api/session')).ok()).toBe(true)
  const diagnostics = await (await request.get('/api/system/diagnostics')).json()
  expect(diagnostics.runtime_mode).toBe('TEST')
  const before = await countRows(diagnostics.database_path)
  const changes: string[] = [], requests: string[] = []
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/search') requests.push(request.postData() || '')
    else if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(request.method()) && new URL(request.url()).pathname.startsWith('/api/')) changes.push(new URL(request.url()).pathname)
  })
  // ONLY the browser speech input/output hardware is replaced. No route(),
  // fetch stub, business API response, location, or DB data is supplied here.
  await page.addInitScript(() => {
    class Recognition {
      static available = async () => 'available'
      onstart: (() => void) | null = null
      onresult: ((event: unknown) => void) | null = null
      onerror: unknown; onend: unknown
      start() { (window as unknown as Record<string, unknown>).__voiceInput = this; this.onstart?.() }
      abort() { (window as unknown as Record<string, unknown>).__voiceAborts = Number((window as unknown as Record<string, unknown>).__voiceAborts || 0) + 1 }
    }
    Object.defineProperty(Recognition.prototype, 'processLocally', { value: false, writable: true })
    Object.defineProperty(window, 'SpeechRecognition', { configurable: true, value: Recognition })
    Object.defineProperty(window, 'SpeechSynthesisUtterance', { configurable: true, value: class { constructor(public text: string) {} } })
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: { getVoices: () => [{ lang: 'zh-CN', localService: true }], cancel: () => undefined,
      speak: (value: { text: string; onend?: () => void }) => { (window as unknown as Record<string, unknown>).__voiceSpoken = value.text; value.onend?.() } } })
  })
  await page.goto('/search')
  await expect(page.getByText('测试模式，当前数据只供自动化验收，不进入真实数据库。', { exact: true })).toBeVisible()
  const panel = page.getByRole('region', { name: '语音寻找物品' })
  await expect(panel.getByText('未监听', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__voiceInput)).toBeUndefined()
  await panel.getByRole('button', { name: '点击说出你要找的物品' }).click()
  await expect(panel.getByText('正在聆听', { exact: true })).toBeVisible()
  const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/search' && response.request().method() === 'POST')
  await page.evaluate(() => {
    const value = (window as unknown as { __voiceInput: { onresult: (event: unknown) => void } }).__voiceInput
    value.onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: '我的手机在哪里' } }] })
  })
  const actual = await response
  expect(actual.ok()).toBe(true)
  expect((await actual.json()).results).toEqual([])
  await expect(panel.getByLabel('本次语音回答')).toContainText('这是自动化测试数据')
  await expect(panel.getByLabel('本次语音回答')).toContainText('没有可验证的位置')
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__voiceSpoken)).toContain('没有可验证的位置')
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__voiceAborts)).toBe(1)
  expect(requests.map(value => JSON.parse(value))).toEqual([{ query: '我的手机在哪里' }])
  expect(changes).toEqual([])
  expect(await countRows(diagnostics.database_path)).toEqual(before)
  await page.reload()
  await expect(panel.getByText('未监听', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__voiceInput)).toBeUndefined()
  expect(await countRows(diagnostics.database_path)).toEqual(before)
  console.log('[VOICE_REAL_API] hardware_mock=true api_mock=false runtime_mode=TEST real_sqlite=true microphone_verified=false data_mutations=0')
})

test('自动场景：实际文件帧→真实家具检测→近似草稿SQLite和图片哈希；刷新及并发条件请求不覆盖', async ({ page, request }) => {
  test.setTimeout(60_000)
  expect((await request.get('/api/session')).ok()).toBe(true)
  const diagnostics = await (await request.get('/api/system/diagnostics')).json()
  expect(diagnostics.runtime_mode).toBe('TEST')
  const video = await readFile(resolve(root, 'data/test-assets/public-phone-lotti.webm'))
  expect(createHash('sha256').update(video).digest('hex')).toBe('c2f8e37ef38c7204bc15f3a911e238edd43c00b1f48f06458291f2e389b0ed48')
  const uploaded = await request.post('/api/cameras/upload-video', { multipart: { file: { name: 'public-phone-lotti.webm', mimeType: 'video/webm', buffer: video } } })
  expect(uploaded.ok()).toBe(true)
  const source = await uploaded.json()
  expect(source).toMatchObject({ runtime_mode: 'TEST', source_type: 'video_file', is_simulated: true })
  const added = await request.post('/api/cameras', { data: { name: '实际文件场景验收', room_name: 'TEST视频场景', source_type: 'video', source: source.source, config: { loop: true }, inference_fps: 5, save_clips: false } })
  expect(added.ok()).toBe(true)
  const camera = await added.json()
  const endpoint = `/api/cameras/${encodeURIComponent(camera.id)}/scene`
  const proposals: string[] = []
  page.on('request', value => { if (value.method() === 'POST' && value.url().includes('/scene/propose')) proposals.push(value.url()) })
  try {
    expect((await request.post(`/api/cameras/${camera.id}/start`)).ok()).toBe(true)
    await expect.poll(async () => {
      const health = (await (await request.get(`/api/cameras/${camera.id}`)).json()).health
      return Boolean(health?.preview_source_session_id && health.preview_frame_sequence >= 0 && health.preview_age_ms >= 0 && health.preview_age_ms <= 1000)
    }, { timeout: 15_000, intervals: [250, 500] }).toBe(true)
    const created = page.waitForResponse(value => value.request().method() === 'POST' && value.url().includes('/scene/propose?only_if_empty=true'))
    await page.goto(`/scene?camera=${encodeURIComponent(camera.id)}`)
    const actual = await created
    expect(actual.ok()).toBe(true)
    const result = await actual.json(), scene = result.scene
    expect(result).toMatchObject({ runtime_mode: 'TEST', source_type: 'video_file', is_simulated: true })
    expect(scene).toMatchObject({ camera_id: camera.id, runtime_mode: 'TEST', is_simulated: true, calibration_status: 'needs_confirmation', surfaces: [] })
    expect(scene.source_session_id).toBeTruthy(); expect(scene.source_frame).toBeGreaterThanOrEqual(0)
    expect(scene.world_geometry.eligible_for_mapping).toBe(false)
    expect(scene.world_geometry.objects.every((object: { confirmed_by_user: boolean; source: string }) => object.confirmed_by_user === false && object.source === 'model_proposal_estimate')).toBe(true)
    const screenshot = await request.get(scene.screenshot_path)
    expect(screenshot.ok()).toBe(true)
    expect(createHash('sha256').update(await screenshot.body()).digest('hex')).toBe(scene.screenshot_sha256)
    const dimensions = await page.evaluate(async path => {
      const image = await createImageBitmap(await (await fetch(path)).blob())
      const size = [image.width, image.height]; image.close(); return size
    }, scene.screenshot_path)
    expect(dimensions).toEqual([scene.geometry.source_width, scene.geometry.source_height])
    expect(await countRows(diagnostics.database_path)).toMatchObject({ scenes: 1, movement_events: 0, item_current_state: 0, event_media: 0 })
    await page.reload()
    await expect(page.getByText('已有场景已保留；需要更新时请明确点击“重新识别场景”。', { exact: true })).toBeVisible()
    expect(proposals).toHaveLength(1)
    const guarded = await request.post(`${endpoint}/propose?only_if_empty=true`)
    expect(guarded.status()).toBe(409)
    const current = (await (await request.get(endpoint)).json()).scene
    expect(current.snapshot_id).toBe(scene.snapshot_id); expect(current.screenshot_sha256).toBe(scene.screenshot_sha256); expect(current.scene_version).toBe(scene.scene_version)
    expect(await countRows(diagnostics.database_path)).toMatchObject({ scenes: 1, movement_events: 0, item_current_state: 0, event_media: 0 })
    console.log(`[SCENE_REAL_API] api_mock=false source=video_file runtime_mode=TEST physical_camera=false proposals=${scene.proposals.length} objects=${scene.world_geometry.objects.length} screenshot_hash_verified=true scenes=1 confirmation_writes=0`)
  } finally { await page.goto('/search'); await request.post(`/api/cameras/${camera.id}/stop`) }
})

test('USB自动接板真实API权限及模式边界：TEST刷新不绑定、不生成任务、不刷物理板', async ({ page, request }) => {
  await request.get('/api/session')
  const status = await (await request.get('/api/firmware/auto-usb')).json()
  expect(status).toMatchObject({ runtime_mode: 'TEST', enabled: false, binding: null, physical_flash_performed: false, listener_running: false })
  const writes: string[] = []
  page.on('request', value => { if (value.method() === 'POST' && new URL(value.url()).pathname.startsWith('/api/firmware')) writes.push(value.url()) })
  await page.goto('/devices')
  const panel = page.getByRole('region', { name: '绑定板卡自动接入' })
  await expect(panel.getByText('当前模式不允许接触物理串口。')).toBeVisible()
  await expect(panel.getByRole('checkbox')).toHaveCount(0)
  await page.reload()
  await expect(panel.getByText('当前模式不允许接触物理串口。')).toBeVisible()
  expect(writes).toEqual([])
  const refused = await request.post('/api/firmware/auto-usb/bind', { data: {
    port: 'COM0', ssid: 'contract-only-not-applied', password: '', backend_url: 'http://192.168.1.20:8018',
    device_name: 'TEST boundary', room_name: 'TEST boundary', board_model: 'ai_thinker_esp32cam', authorize_fixed_firmware: true,
  } })
  expect(refused.status()).toBe(409)
  const after = await (await request.get('/api/firmware/auto-usb')).json()
  expect(after).toMatchObject({ runtime_mode: 'TEST', enabled: false, binding: null, receipt: null, physical_flash_performed: false, listener_running: false })
  console.log('[USB_REAL_API] api_mock=false runtime_mode=TEST real_sqlite=true hardware_opened=false physical_flash_performed=false binding_mutations=0')
})
