import { chromium, expect, test, type APIRequestContext } from '@playwright/test'
import { createHash } from 'node:crypto'
import { execFile as execFileCallback } from 'node:child_process'
import { readFile, realpath, stat } from 'node:fs/promises'
import { basename, resolve, relative, isAbsolute } from 'node:path'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'

const execFile = promisify(execFileCallback)
const projectRoot = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../..')
const python = process.env.OM_E2E_PYTHON || resolve(projectRoot, '.venv', 'Scripts', 'python.exe')

async function optionalHash(path: string) {
  try { return createHash('sha256').update(await readFile(path)).digest('hex') } catch { return null }
}

async function sqliteCounts(database: string) {
  const code = "import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);print(json.dumps({t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('movement_events','item_current_state','tracks','event_media','source_sessions')}));c.close()"
  const { stdout } = await execFile(python, ['-c', code, database], { cwd: projectRoot })
  return JSON.parse(stdout) as Record<string, number>
}

async function json<T>(response: Awaited<ReturnType<APIRequestContext['get']>>) {
  if (!response.ok()) {
    const responseBody = await response.text()
    throw new Error(`${response.url()} 应返回成功响应，实际为 HTTP ${response.status()}：${responseBody}`)
  }
  return response.json() as Promise<T>
}

test.describe.configure({ mode: 'serial' })

test('PR2 真实后端：示例与手机菜单不写入真实记录，关闭后恢复交互', async ({ page, request }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const before = await sqliteCounts(diagnostics.database_path)
  const writes: string[] = []
  page.on('request', value => {
    if (new URL(value.url()).pathname.startsWith('/api/') && !['GET', 'HEAD'].includes(value.method())) writes.push(value.url())
  })
  const backgrounds = new Map<number, string>()
  for (const { width, scheme } of [
    { width: 1360, scheme: 'light' }, { width: 390, scheme: 'light' },
    { width: 1360, scheme: 'dark' }, { width: 390, scheme: 'dark' },
  ] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' })
    await page.setViewportSize({ width, height: 920 })
    await page.goto('/')
    await expect(page.locator('.topbar-status')).toHaveAttribute('data-runtime-mode', 'REAL')
    const trigger = page.getByRole('button', { name: '先看示例', exact: true })
    await trigger.click()
    const dialog = page.getByRole('dialog', { name: '先看看物忆怎样回答', exact: true })
    await expect(dialog.getByText('示例说明 · 非真实记录', { exact: true })).toBeVisible()
    await expect(page.locator('body')).toHaveCSS('overflow', 'hidden')
    await dialog.getByRole('button', { name: '没有记录', exact: true }).click()
    await expect(dialog.getByRole('heading', { name: '示例：还没有钥匙的位置记录', exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
    expect(await page.locator('body').evaluate(element => element.style.overflow)).not.toBe('hidden')
    expect(await page.locator('[inert]').count()).toBe(0)
    if (width === 390) {
      const menu = page.getByRole('button', { name: '打开菜单', exact: true })
      await menu.click()
      await expect(page.getByRole('dialog', { name: '导航菜单' })).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(menu).toBeFocused()
      expect(await page.locator('[inert]').count()).toBe(0)
    }
    const layout = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }))
    expect(layout.content).toBeLessThanOrEqual(layout.viewport + 1)
    const background = await page.locator('body').evaluate(element => getComputedStyle(element).backgroundColor)
    if (scheme === 'light') backgrounds.set(width, background)
    else expect(background).not.toBe(backgrounds.get(width))
    await page.screenshot({ path: resolve(projectRoot, 'data', 'verification', `pr2-first-use-${width}-${scheme}.png`), fullPage: true })
  }
  expect(writes).toEqual([])
  expect(await sqliteCounts(diagnostics.database_path)).toEqual(before)
  expect(await json(await request.get('/api/items'))).toEqual([])
  expect(await json(await request.get('/api/cameras'))).toEqual([])
})

test('场景页面真实 HTTP：离线不会自动建图，桌面和手机都没有虚构位置', async ({ page, request }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const before = await sqliteCounts(diagnostics.database_path)
  await page.goto('/scene')
  await expect(page.getByRole('heading', { name: '从看见，到知道在哪里', exact: true })).toBeVisible()
  await expect(page.getByText('先连接一个摄像头', { exact: true })).toBeVisible()
  for (const width of [1360, 390]) {
    await page.setViewportSize({ width, height: 920 })
    const dimensions = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth }))
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.width + 1)
    await page.screenshot({ path: resolve(projectRoot, 'data', 'verification', `photo-scene-${width}.png`), fullPage: true })
  }
  await page.reload()
  expect(await sqliteCounts(diagnostics.database_path)).toEqual(before)
  expect(await json(await request.get('/api/cameras'))).toEqual([])
})

test('注册页直接快门调用getUserMedia并保存浏览器实际编码JPEG，不触发文件选择或位置事件', async ({ request, baseURL }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const before = await sqliteCounts(diagnostics.database_path)
  const browser = await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] })
  let itemId = ''
  try {
    const context = await browser.newContext({ permissions: ['camera'] })
    await context.addInitScript(() => {
      const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
      const calls: MediaStreamConstraints[] = []
      Object.defineProperty(window, '__registrationCameraCalls', { configurable: true, value: calls })
      navigator.mediaDevices.getUserMedia = async (constraints) => {
        calls.push(constraints || {})
        return original(constraints)
      }
    })
    const page = await context.newPage()
    await page.setViewportSize({ width: 390, height: 844 })
    let fileChoosers = 0
    page.on('filechooser', () => { fileChoosers += 1 })
    await page.goto(`${baseURL}/items`)
    await page.getByRole('button', { name: '添加物品', exact: true }).click()
    await page.getByLabel('物品名称', { exact: true }).fill('浏览器直接快门链路试验')
    await page.getByRole('combobox', { name: '类型', exact: true }).selectOption({ label: '手机' })
    const directCapture = page.getByRole('button', { name: '直接拍照', exact: true })
    expect(await directCapture.evaluate(element => element.tagName)).toBe('BUTTON')
    await directCapture.click()

    const captureDialog = page.getByRole('dialog', { name: '拍摄参考照片', exact: true })
    const video = captureDialog.getByLabel('拍摄参考照片实时预览')
    await expect(video).toBeVisible()
    await expect.poll(async () => video.evaluate(element => (element as HTMLVideoElement).videoWidth)).toBeGreaterThan(0)
    const calls = await page.evaluate(() => (window as unknown as { __registrationCameraCalls: MediaStreamConstraints[] }).__registrationCameraCalls)
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ audio: false, video: { facingMode: { ideal: 'environment' } } })
    await video.evaluate(element => {
      const media = element as HTMLVideoElement
      Object.defineProperty(window, '__registrationCameraTrack', { configurable: true, value: (media.srcObject as MediaStream).getVideoTracks()[0] })
      Object.defineProperty(window, '__registrationCameraVideo', { configurable: true, value: media })
    })
    await captureDialog.getByRole('button', { name: '拍下这张', exact: true }).click()
    await expect(captureDialog.getByAltText('刚拍摄的参考照片预览')).toBeVisible()
    const layout = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }))
    expect(layout.content).toBeLessThanOrEqual(layout.viewport + 1)
    await page.screenshot({ path: resolve(projectRoot, 'data', 'verification', 'direct-camera-capture-390.png'), fullPage: true })
    await expect.poll(() => page.evaluate(() => (window as unknown as { __registrationCameraTrack: MediaStreamTrack }).__registrationCameraTrack.readyState)).toBe('ended')
    expect(await page.evaluate(() => (window as unknown as { __registrationCameraVideo: HTMLVideoElement }).__registrationCameraVideo.srcObject)).toBeNull()
    expect(fileChoosers).toBe(0)
    await captureDialog.getByRole('button', { name: '使用这张照片', exact: true }).click()
    await expect(captureDialog).toHaveCount(0)
    await expect(page.getByText(/camera-.*\.jpg/)).toBeVisible()

    const uploaded = page.waitForResponse(response => response.url().endsWith('/reference-images') && response.request().method() === 'POST')
    await page.getByRole('button', { name: '保存物品', exact: true }).click()
    const uploadResponse = await uploaded
    expect(uploadResponse.ok(), `直拍照片保存应成功，HTTP ${uploadResponse.status()}：${await uploadResponse.text()}`).toBe(true)
    const items = await json<Array<{ id: string; name: string; reference_images?: Array<{ url?: string; path: string; width: number; height: number; sha256: string }> }>>(await request.get('/api/items'))
    const item = items.find(value => value.name === '浏览器直接快门链路试验')
    expect(item).toBeTruthy()
    itemId = item!.id
    expect(item!.reference_images).toHaveLength(1)
    const reference = item!.reference_images![0]
    expect(reference.width).toBeGreaterThan(0)
    expect(reference.height).toBeGreaterThan(0)
    expect(reference.sha256).toMatch(/^[a-f0-9]{64}$/)
    const media = await request.get(reference.url || reference.path)
    expect(media.ok()).toBe(true)
    expect(media.headers()['content-type']).toContain('image/jpeg')
    const jpeg = await media.body()
    expect(jpeg.length).toBeGreaterThan(100)
    expect([...jpeg.subarray(0, 2)]).toEqual([0xff, 0xd8])
    expect(await sqliteCounts(diagnostics.database_path)).toEqual(before)
    expect(await json<unknown[]>(await request.get('/api/events'))).toEqual([])
    console.log('[REGISTRATION_CAMERA_E2E] real getUserMedia/canvas/JPEG/FastAPI/SQLite; Chromium synthetic camera only; no file chooser or movement event; not Android/iPhone proof')
    await context.close()
  } finally {
    await browser.close()
    if (itemId) await request.delete(`/api/items/${itemId}`)
  }
})

test('照片注册真实 UI → FastAPI → DINO → SQLite，不以合成纹理证明实物识别', async ({ page, request }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const before = await sqliteCounts(diagnostics.database_path)
  const { stdout: encoded } = await execFile(python, ['-B', '-c',
    "import base64,cv2,numpy as np;a=np.random.default_rng(27).integers(0,256,(256,320,3),dtype=np.uint8);print(base64.b64encode(cv2.imencode('.png',a)[1]).decode())"], { cwd: projectRoot })
  await page.goto('/items')
  await page.getByRole('button', { name: '添加物品', exact: true }).click()
  await page.getByLabel('物品名称', { exact: true }).fill('合成纹理流程试验')
  await page.getByRole('combobox', { name: '类型', exact: true }).selectOption({ label: '手机' })
  await page.getByLabel('选择参考照片', { exact: true }).setInputFiles({ name: 'generated-pipeline-only.png', mimeType: 'image/png', buffer: Buffer.from(encoded.trim(), 'base64') })
  await page.getByRole('button', { name: '保存物品', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认这是我的物品', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '确认这是我的物品', exact: true }).click()
  await expect(page.getByRole('button', { name: '目标区域已确认', exact: true })).toBeVisible()
  const buildResponse = page.waitForResponse(response => response.url().endsWith('/profile/build') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '建立 / 更新识别档案', exact: true }).click()
  expect((await buildResponse).status()).toBe(200)
  await expect(page.getByText('当前服务模型已加载', { exact: true })).toBeVisible()
  await expect(page.getByRole('dialog').getByText('档案已就绪 · 实时摄像头尚未加载当前版本', { exact: true })).toBeVisible()
  const items = await json<Array<{ id: string; name: string }>>(await request.get('/api/items'))
  const item = items.find(value => value.name === '合成纹理流程试验')!
  expect(item).toBeTruthy()
  const profile = await json<{ registration_status: string; profile_version: number; loaded_camera_ids: string[]; model_id: string }>(await request.get(`/api/items/${item.id}/profile`))
  expect(profile.registration_status).toBe('ready')
  expect(profile.model_id).toBe('onnx-community/dinov2-small')
  expect(profile.loaded_camera_ids).toEqual([])
  const { stdout: vectors } = await execFile(python, ['-B', '-c',
    "import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);r=c.execute('SELECT dimension,embeddings FROM item_recognition_profiles WHERE item_id=?',(sys.argv[2],)).fetchone();v=json.loads(r[1]);print(json.dumps({'dimension':r[0],'count':len(v),'norm':sum(x*x for x in v[0])**.5}));c.close()",
    diagnostics.database_path, item.id], { cwd: projectRoot })
  expect(JSON.parse(vectors)).toMatchObject({ dimension: 384, count: 1 })
  expect(JSON.parse(vectors).norm).toBeCloseTo(1, 5)
  expect(await sqliteCounts(diagnostics.database_path)).toEqual(before)
  await page.reload()
  await page.getByRole('article').filter({ has: page.getByRole('heading', { name: item.name, exact: true }) }).getByRole('button', { name: '管理照片 / 测试识别' }).click()
  await expect(page.getByRole('button', { name: '目标区域已确认', exact: true })).toBeVisible()
  for (const width of [1360, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/items')
    await page.getByRole('article').filter({ has: page.getByRole('heading', { name: item.name, exact: true }) }).getByRole('button', { name: '管理照片 / 测试识别' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText('档案版本', { exact: true }).locator('..')).toContainText(String(profile.profile_version))
    await expect(page.getByRole('status', { name: '下一步注册操作' })).toContainText('注册档案已建立')
    const sameProfile = await json<{ profile_version: number }>(await request.get(`/api/items/${item.id}/profile`))
    expect(sameProfile.profile_version).toBe(profile.profile_version)
    const dimensions = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }))
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport + 1)
  }
  await json(await request.delete(`/api/items/${item.id}`))
  console.log('[PHOTO_REAL_E2E] real HTTP + actual local model + persisted 384D; same registered profile at desktop1360/mobile390 viewports (not Android device); generated image only; no camera opened; no movement created')
})

test('真实 FastAPI + 独立 SQLite：REAL 无视频源始终是 0 事件', async ({ page, request }) => {
  const canonicalReal = resolve(projectRoot, 'data', 'database', 'objectmemory.sqlite')
  const canonicalHashBefore = await optionalHash(canonicalReal)
  const session = await json<{ authenticated: boolean; local: boolean }>(await request.get('/api/session'))
  expect(session).toMatchObject({ authenticated: true, local: true })
  const runtime = await json<{ runtime_mode: string; is_simulated: boolean }>(await request.get('/api/runtime-config'))
  expect(runtime).toMatchObject({ runtime_mode: 'REAL', is_simulated: false })

  const diagnostics = await json<{ database_path: string; runtime_mode: string }>(await request.get('/api/system/diagnostics'))
  expect(diagnostics.runtime_mode).toBe('REAL')
  const databasePath = await realpath(diagnostics.database_path)
  const isolatedRoot = await realpath(resolve(databasePath, '..', '..'))
  const temporaryRoot = await realpath(resolve(projectRoot, 'data', 'temporary'))
  const isolatedRelative = relative(temporaryRoot, isolatedRoot)
  expect(isolatedRelative.startsWith('..') || isAbsolute(isolatedRelative)).toBe(false)
  expect(basename(isolatedRoot)).toMatch(/^playwright-real-/)
  const databaseRelative = relative(isolatedRoot, databasePath)
  expect(databaseRelative.startsWith('..') || isAbsolute(databaseRelative)).toBe(false)
  expect(databasePath.endsWith('objectmemory.sqlite')).toBe(true)
  expect((await stat(databasePath)).size).toBeGreaterThan(0)
  expect(await sqliteCounts(databasePath)).toMatchObject({ movement_events: 0, item_current_state: 0, tracks: 0, event_media: 0 })

  const cameras = await json<unknown[]>(await request.get('/api/cameras'))
  const eventsBefore = await json<unknown[]>(await request.get('/api/events?limit=100'))
  expect(cameras).toHaveLength(0)
  expect(eventsBefore).toHaveLength(0)

  const storage = await json<{ runtime_mode: string; screenshot_size: number; clip_size: number; retention_policy: string }>(await request.get('/api/storage/status'))
  expect(storage).toMatchObject({ runtime_mode: 'REAL', screenshot_size: 0, clip_size: 0, retention_policy: 'MINIMAL' })

  await page.goto('/')
  await expect(page.getByText('REAL · 真实模式', { exact: true })).toBeVisible()
  await page.getByText('查看系统概览与识别速度', { exact: true }).click()
  await expect(page.getByText('已注册物品', { exact: true })).toBeVisible()
  await expect(page.getByText('已记忆物品', { exact: true })).toHaveCount(0)
  await expect(page.getByText(/还没有历史移动事件。最后看到的位置可能已经存在/)).toBeVisible()
  await expect(page.getByText(/演示模式，当前事件不来自真实家庭摄像头/)).toHaveCount(0)

  await page.goto('/search')
  await page.getByRole('textbox', { name: '寻找物品', exact: true }).fill('我的手机在哪里')
  await page.getByRole('button', { name: '帮我找' }).click()
  await expect(page.getByRole('heading', { name: '还没有添加物品', exact: true })).toBeVisible()
  await expect(page.getByText(/桌面|沙发|91%|16:32/)).toHaveCount(0)

  await page.reload()
  await expect(page.getByRole('heading', { name: '还没有添加物品', exact: true })).toBeVisible()
  expect(await json<unknown[]>(await request.get('/api/events?limit=100'))).toHaveLength(0)

  const seed = await request.post('/api/system/demo-seed')
  expect(seed.status()).toBe(409)
  expect((await seed.json()).detail).toMatch(/DEMO|演示种子/)
  const virtualDevice = await request.post('/api/virtual-device/start', { data: { source: 'video' } })
  expect(virtualDevice.status()).toBe(409)
  expect((await virtualDevice.json()).detail).toMatch(/DEMO|虚拟设备/)
  expect(await json<unknown[]>(await request.get('/api/events?limit=100'))).toHaveLength(0)
  expect(await sqliteCounts(databasePath)).toMatchObject({ movement_events: 0, item_current_state: 0, tracks: 0, event_media: 0 })

  await page.goto('/cameras?add=1')
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByRole('button', { name: /视频文件回放/ })).toHaveCount(0)
  await expect(page.getByText(/测试视频入口只在 DEMO 模式出现/)).toBeVisible()

  await page.goto('/devices')
  await expect(page.getByRole('heading', { name: 'Virtual ESP32-CAM' })).toHaveCount(0)
  await expect(page.getByText('真机刷写').first()).toBeVisible()
  await expect(page.getByText('未执行', { exact: true }).first()).toBeVisible()

  await page.goto('/storage')
  await expect(page.getByRole('heading', { name: '存储管理' })).toBeVisible()
  await expect(page.getByText('MINIMAL', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('heading', { name: '删除所有真实记录' })).toBeVisible()
  const finalEvents = await json<unknown[]>(await request.get('/api/events?limit=100'))
  const finalCounts = await sqliteCounts(databasePath)
  const finalStorage = await json<{ database_size: number; screenshot_size: number; clip_size: number; log_size: number }>(await request.get('/api/storage/status'))
  console.log(`[REAL_E2E_EVIDENCE] ${JSON.stringify({
    runtime_mode: runtime.runtime_mode,
    database_path: databasePath,
    database_size_bytes: (await stat(databasePath)).size,
    api_event_count: finalEvents.length,
    camera_count: cameras.length,
    sqlite_counts: finalCounts,
    storage: finalStorage,
  })}`)
  expect(await optionalHash(canonicalReal)).toBe(canonicalHashBefore)
})

test('P0 真实控制面：Marker 可解码、页面刷新不创建套件或事件、离线不能开始', async ({ page, request }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const item = await json<{ id: string }>(await request.post('/api/items', {
    data: { name: '验收测试手机', type: 'phone', aliases: ['测试手机'] },
  }))
  const camera = await json<{ id: string }>(await request.post('/api/cameras', {
    data: { name: '不启动的验收配置', source_type: 'webcam', source: '0', enabled: true },
  }))
  const bound = await json<{ aruco_id: number }>(await request.post(`/api/acceptance/items/${item.id}/marker`, { data: {} }))
  const markerResponse = await request.get(`/api/acceptance/markers/${bound.aruco_id}.png`)
  expect(markerResponse.ok()).toBe(true)
  const markerBytes = await markerResponse.body()
  const decoder = "import base64,cv2,json,numpy as np,sys;im=cv2.imdecode(np.frombuffer(base64.b64decode(sys.argv[1]),np.uint8),0);d=cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50));c,ids,r=d.detectMarkers(im);print(json.dumps(ids.flatten().tolist() if ids is not None else []))"
  const { stdout } = await execFile(python, ['-c', decoder, markerBytes.toString('base64')], { cwd: projectRoot })
  expect(JSON.parse(stdout)).toEqual([bound.aruco_id])

  const pageWrites: string[] = []
  page.on('request', (outgoing) => {
    if (outgoing.url().includes('/api/') && !['GET', 'HEAD'].includes(outgoing.method())) pageWrites.push(outgoing.url())
  })
  await page.goto(`/acceptance/real-movement?item=${item.id}&camera=${camera.id}&runs=fabricated-pass&current=fabricated-pass`)
  await expect(page.getByRole('heading', { name: '真实移动验收', exact: true })).toBeVisible()
  await expect(page.getByText('先确认本机 OpenCV 正在连续取流', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: '真实移动验收', exact: true })).toBeVisible()
  await page.goto(`/companion/validation-marker?item=${item.id}&camera=${camera.id}`)
  await expect(page.getByRole('img', { name: `ArUco Marker ${bound.aruco_id}`, exact: true })).toBeVisible()
  await expect(page.getByText('后端尚未识别', { exact: true })).toBeVisible()
  expect(pageWrites).toEqual([])
  expect(await json<unknown[]>(await request.get('/api/acceptance/suites'))).toHaveLength(0)
  expect(await json<unknown[]>(await request.get('/api/acceptance/runs'))).toHaveLength(0)
  const preflight = await json<{ ready: boolean }>(await request.get(`/api/acceptance/preflight?camera_id=${camera.id}`))
  expect(preflight.ready).toBe(false)
  expect(await sqliteCounts(diagnostics.database_path)).toMatchObject({ movement_events: 0, item_current_state: 0, event_media: 0, source_sessions: 0 })
  console.log(`[P0_REAL_CONTROL_EVIDENCE] ${JSON.stringify({ marker_id: bound.aruco_id, marker_sha256: createHash('sha256').update(markerBytes).digest('hex'), marker_decoded: true, page_business_writes: pageWrites.length, offline_ready: preflight.ready, movement_events: 0, physical_movement_test: false })}`)
})

test('易用性真实后端：手机入口、窄屏布局、重复摄像头拒绝且无事件', async ({ page, request }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const countsBefore = await sqliteCounts(diagnostics.database_path)
  const camerasBefore = await json<Array<{ id: string; source: string }>>(await request.get('/api/cameras'))
  expect(camerasBefore).toHaveLength(1)
  const started = Date.now()
  const duplicate = await request.post('/api/cameras', { data: { name: '不应重复创建', source_type: 'webcam', source: '0', enabled: true } })
  expect(duplicate.status()).toBe(409)
  expect(Date.now() - started).toBeLessThan(2000)
  expect(await json<unknown[]>(await request.get('/api/cameras'))).toHaveLength(1)
  await json(await request.patch(`/api/cameras/${camerasBefore[0].id}`, { data: { name: '仅重命名，不打开硬件' } }))

  await page.goto('/')
  await expect(page.getByRole('link', { name: '物忆首页' })).toBeVisible()
  await page.getByRole('link', { name: '连接手机', exact: true }).first().click()
  await expect(page).toHaveURL(/\/settings#mobile-access$/)
  await expect(page.getByRole('region', { name: '连接手机' })).toBeVisible()
  // This isolated server intentionally binds localhost: never fabricate a usable phone QR.
  await expect(page.getByText(/开启带访问码的局域网模式/)).toBeVisible()
  await expect(page.getByRole('img', { name: '手机打开物忆的二维码' })).toHaveCount(0)
  await expect(page.getByLabel('主机访问码')).toHaveText('•••• ••••')

  for (const width of [390, 360]) {
    await page.setViewportSize({ width, height: 844 })
    await expect(page.getByRole('region', { name: '连接手机' })).toBeVisible()
    await expect(page.locator('.topbar-status').getByText('REAL · 真实模式', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '打开菜单', exact: true })).toBeVisible()
    const dimensions = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth }))
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.width + 1)
  }
  // Desktop/sidebar and drawer/menu use one 900px boundary: 920px must not lose both.
  await page.setViewportSize({ width: 920, height: 844 })
  await expect(page.locator('.sidebar')).toBeVisible()
  await expect(page.locator('.sidebar').getByRole('link', { name: '寻找物品', exact: true })).toBeVisible()
  await expect(page.locator('.topbar-status').getByText('REAL · 真实模式', { exact: true })).toBeVisible()
  const tabletWidth = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth }))
  expect(tabletWidth.content).toBeLessThanOrEqual(tabletWidth.width + 1)
  await page.goto('/cameras')
  await expect(page.getByText('仅重命名，不打开硬件', { exact: true }).first()).toBeVisible()
  await page.reload()
  expect(await sqliteCounts(diagnostics.database_path)).toEqual(countsBefore)
  const cameraDiagnostics = await json<{ diagnostic_running: boolean }>(await request.get('/api/camera-diagnostics'))
  expect(cameraDiagnostics.diagnostic_running).toBe(false)
  console.log(`[USABILITY_REAL_EVIDENCE] ${JSON.stringify({ duplicate_http: duplicate.status(), camera_count: 1, mobile_widths: [390, 360], tablet_width: 920, runtime_mode_visible: true, no_fake_lan_qr: true, sqlite_unchanged: true, physical_camera_opened: false })}`)
})

test('页面资源断开时保留真实模式与导航，重新加载恢复；核心API不Mock', async ({ page, request }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const before = await sqliteCounts(diagnostics.database_path)
  const writes: string[] = []
  page.on('request', outgoing => {
    if (outgoing.url().includes('/api/') && !['GET', 'HEAD'].includes(outgoing.method())) writes.push(outgoing.url())
  })
  const deviceChunk = /\/assets\/DevicesPage-[^/]+\.js$/
  // Fault-inject only the static page chunk; API, sessions and SQLite stay real.
  await page.route(deviceChunk, route => route.abort('failed'))
  await page.goto('/')
  await page.locator('details.advanced-nav summary').filter({ hasText: '高级工具' }).click()
  await page.getByRole('link', { name: '设备中心', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('页面暂时无法加载')
  await expect(page.locator('.topbar-status').getByText('REAL · 真实模式', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '物忆首页' })).toBeVisible()
  await page.unroute(deviceChunk)
  await page.getByRole('button', { name: '重新加载页面', exact: true }).click()
  await expect(page.getByRole('heading', { name: '插上 USB，一键装好摄像头', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(writes).toEqual([])
  expect(await sqliteCounts(diagnostics.database_path)).toEqual(before)
})

test('手动作真实 HTTP：离线缓存为空、显示与分析独立保存、手机桌面无溢出', async ({ page, request }) => {
  await json(await request.get('/api/session'))
  const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  const before = await sqliteCounts(diagnostics.database_path)
  expect(before).toEqual({ movement_events: 0, item_current_state: 0, tracks: 0, event_media: 0, source_sessions: 0 })
  const cameras = await json<Array<{ id: string }>>(await request.get('/api/cameras'))
  expect(cameras).toHaveLength(1)
  const cameraId = cameras[0].id
  const actions = await json<Record<string, unknown>>(await request.get(`/api/cameras/${cameraId}/actions`))
  expect(actions).toMatchObject({ camera_id: cameraId, runtime_mode: 'REAL', is_simulated: false, fresh: false,
    source_session_id: null, source_frame: null, timestamp: null, hands: [], interactions: [], profile_count: 0, reason: 'camera_not_running',
    hand_status: { enabled: true, available: false } })

  const outgoingWrites: Array<{ path: string; method: string }> = []
  page.on('request', (outgoing) => {
    if (outgoing.url().includes('/api/') && !['GET', 'HEAD'].includes(outgoing.method())) outgoingWrites.push({ path: new URL(outgoing.url()).pathname, method: outgoing.method() })
  })
  await page.goto(`/live?camera=${cameraId}`)
  const card = page.getByRole('region', { name: '当前手部动作', exact: true })
  await expect(card).toBeVisible()
  await expect(card.getByText('摄像头未运行，暂无当前动作', { exact: true })).toBeVisible()
  await expect(card.getByRole('list', { name: '当前几何手动作' })).toHaveCount(0)
  await expect(card.getByRole('list', { name: '手与物品的交互线索' })).toHaveCount(0)
  await expect(card.getByText(/张手|握拳|捏合|拿起成功|放下成功/)).toHaveCount(0)
  const screenshots: Array<{ width: number; path: string; sha256: string }> = []
  for (const width of [1360, 360]) {
    await page.setViewportSize({ width, height: 920 })
    await expect(card).toBeVisible()
    await expect(page.locator('.topbar-status').getByText('REAL · 真实模式', { exact: true })).toBeVisible()
    const dimensions = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth }))
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.width + 1)
    const path = resolve(projectRoot, 'data', 'verification', `hand-actions-offline-${width}.png`)
    const bytes = await page.screenshot({ path, fullPage: true })
    screenshots.push({ width, path, sha256: createHash('sha256').update(bytes).digest('hex') })
  }
  expect(outgoingWrites).toEqual([])

  await page.goto('/settings')
  const analysis = page.getByRole('checkbox', { name: '启用本地手部分析', exact: true })
  const overlay = page.getByRole('checkbox', { name: '显示手部关键点', exact: true })
  await expect(analysis).toBeChecked()
  await overlay.uncheck()
  const saved = page.waitForResponse((response) => response.url().endsWith('/api/settings') && response.request().method() === 'PATCH')
  await page.getByRole('button', { name: '保存设置', exact: true }).click()
  const savedResponse = await saved
  expect(savedResponse.ok(), `设置保存应成功，HTTP ${savedResponse.status()}：${await savedResponse.text()}`).toBe(true)
  expect(savedResponse.request().postDataJSON()).toMatchObject({ show_hands: false, hand_detection_enabled: true })
  expect(await savedResponse.json()).toMatchObject({ show_hands: false, hand_detection_enabled: true })
  await page.reload()
  await expect(analysis).toBeChecked()
  await expect(overlay).not.toBeChecked()
  expect(await json(await request.get('/api/settings'))).toMatchObject({ show_hands: false, hand_detection_enabled: true })
  const { stdout } = await execFile(python, ['-B', '-c',
    "import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);v=json.loads(c.execute(\"SELECT value FROM settings WHERE id='main'\").fetchone()[0]);print(json.dumps({k:v.get(k) for k in ('show_hands','hand_detection_enabled')}));c.close()",
    diagnostics.database_path], { cwd: projectRoot })
  expect(JSON.parse(stdout)).toEqual({ show_hands: false, hand_detection_enabled: true })
  expect(outgoingWrites).toEqual([{ path: '/api/settings', method: 'PATCH' }])
  expect(await sqliteCounts(diagnostics.database_path)).toEqual(before)
  expect(await json<unknown[]>(await request.get('/api/cameras'))).toHaveLength(1)
  expect(await json(await request.get(`/api/cameras/${cameraId}/actions`))).toMatchObject({ fresh: false, hands: [], interactions: [], reason: 'camera_not_running' })
  console.log(`[HAND_ACTIONS_REAL_EVIDENCE] ${JSON.stringify({ cache_read_only: true, physical_camera_opened: false, mock_core_api: false,
    sqlite_counts: before, settings_persisted: { show_hands: false, hand_detection_enabled: true }, screenshots })}`)
})

test('浏览器采集上传实际JPEG与ACK；只替代硬件输入，不Mock业务API', async ({ request, baseURL }) => {
  await json(await request.get('/api/session'))
  const camera = await json<{ id: string }>(await request.post('/api/cameras', { data: {
    name: '浏览器合成输入链路测试', source_type: 'browser', source: 'browser', enabled: true,
  } }))
  // Chromium's synthetic capture device is explicitly NOT a physical phone.
  // getUserMedia, canvas JPEG encoding, WebSocket, FastAPI and SQLite stay real.
  const browser = await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] })
  try {
    const context = await browser.newContext({ permissions: ['camera'] })
    const page = await context.newPage()
    await page.goto(`${baseURL}/live?camera=${camera.id}`)
    const capture = page.getByRole('region', { name: '浏览器直连摄像头' })
    await capture.getByRole('button', { name: '授权并选择摄像头', exact: true }).click()
    const uploaded = capture.locator('dl > div').filter({ has: page.getByText('已上传', { exact: true }) }).locator('dd')
    await expect.poll(async () => Number((await uploaded.innerText()).match(/\d+/)?.[0] || 0)).toBeGreaterThanOrEqual(3)
    const snapshot = await request.get(`/api/cameras/${camera.id}/frame`)
    expect(snapshot.ok()).toBe(true)
    expect((await snapshot.body()).length).toBeGreaterThan(100)
    expect(snapshot.headers()['content-type']).toContain('image/jpeg')
    await expect(capture.getByText('后端接收中', { exact: true })).toBeVisible()
    expect(await json<unknown[]>(await request.get(`/api/events?camera_id=${camera.id}`))).toEqual([])
    await capture.getByRole('button', { name: '停止浏览器画面', exact: true }).click()
    await expect.poll(async () => (await json<{ health: { status: string } }>(await request.get(`/api/cameras/${camera.id}`))).health.status).toBe('stopped')
    expect(await page.locator('video[aria-label="浏览器摄像头实时预览"]').evaluate(node => (node as HTMLVideoElement).srcObject)).toBeNull()
    await page.reload()
    await expect(capture.getByRole('button', { name: '授权并选择摄像头', exact: true })).toBeVisible()
    expect((await json<{ health: { status: string } }>(await request.get(`/api/cameras/${camera.id}`))).health.status).toBe('stopped')
    console.log('[BROWSER_CAPTURE_E2E] synthetic Chromium camera only; actual getUserMedia/JPEG/ACK/FastAPI; stopped and reload does not resume; not Android or physical identification proof')
    await context.close()
  } finally {
    await browser.close()
    await request.delete(`/api/cameras/${camera.id}`)
  }
})
