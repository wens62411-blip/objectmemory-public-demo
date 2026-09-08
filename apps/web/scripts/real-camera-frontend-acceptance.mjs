import { chromium } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const baseURL = process.env.OM_ACCEPTANCE_URL || 'http://127.0.0.1:8033'
const durationSeconds = Math.max(60, Number(process.env.OM_ACCEPTANCE_SECONDS || 60))
const deviceIndex = Number(process.env.OM_CAMERA_INDEX || 0)
const backend = process.env.OM_CAMERA_BACKEND || 'MSMF'
const root = resolve(process.cwd(), '..', '..')
const diagnostics = resolve(root, 'data', 'diagnostics')
const reportPath = resolve(diagnostics, 'real-camera-acceptance.json')
const screenshotPath = resolve(diagnostics, 'real-camera-frontend.png')

await mkdir(diagnostics, { recursive: true })

const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
const frontend = {
  test_kind: 'REAL_FASTAPI_AND_BROWSER_WEBCAM_CHAIN',
  runtime_mode: null,
  source_type: null,
  is_simulated: null,
  base_url: baseURL,
  started_at: new Date().toISOString(),
  requested_duration_seconds: durationSeconds,
  device_index: deviceIndex,
  backend,
  camera_id: null,
  zone_id: null,
  ui_test_c_success: false,
  continuous_display_seconds: 0,
  decoded_frame_requests: 0,
  failed_frame_requests: 0,
  distinct_frame_hashes: 0,
  websocket_messages: 0,
  natural_width: 0,
  natural_height: 0,
  frontend_screenshot_path: screenshotPath,
  stop_status_code: null,
  ownership_released: false,
  restart_success: false,
  cleanup_deleted: false,
  ui_chain_message: '',
  page_url: '',
  console_errors: [],
  failed_requests: [],
  error: null,
}

let cameraId
const progress = (stage) => process.stdout.write(`[real-camera-frontend] ${stage}\n`)
page.on('console', (message) => {
  if (message.type() === 'error') frontend.console_errors.push(message.text().slice(0, 500))
})
page.on('response', (response) => {
  if (response.status() >= 400) frontend.failed_requests.push({ status: response.status(), url: response.url() })
})
try {
  progress('health-and-session')
  const health = await context.request.get('/api/health')
  if (!health.ok()) throw new Error(`后端健康检查失败：HTTP ${health.status()}`)
  const session = await context.request.get('/api/session')
  if (!session.ok() || !(await session.json()).authenticated) {
    throw new Error(`本机管理会话建立失败：HTTP ${session.status()}`)
  }
  const runtime = await context.request.get('/api/runtime-config')
  if (!runtime.ok()) throw new Error(`运行时配置失败：HTTP ${runtime.status()}`)
  const runtimeBody = await runtime.json()
  if (runtimeBody.runtime_mode !== 'REAL' || runtimeBody.is_simulated !== false) {
    throw new Error(`真摄像头验收必须运行在 REAL 模式：${JSON.stringify(runtimeBody)}`)
  }
  frontend.runtime_mode = runtimeBody.runtime_mode
  frontend.is_simulated = runtimeBody.is_simulated
  if (runtimeBody.api_base_url !== `${baseURL}/api` || runtimeBody.websocket_base_url !== baseURL.replace(/^http/, 'ws') + '/ws') {
    throw new Error(`运行时端口不一致：${JSON.stringify(runtimeBody)}`)
  }

  await page.goto('/')
  await page.waitForLoadState('domcontentloaded')
  const created = await context.request.post('/api/cameras', { data: {
    name: '真实摄像头前端验收', room_name: '真实验收房间', installation: '电脑内置摄像头',
    source_type: 'webcam', source: String(deviceIndex),
    config: { index: deviceIndex, backend, width: 1280, height: 720 },
    enabled: true, inference_fps: 5, save_clips: false,
  } })
  if (!created.ok()) throw new Error(`创建摄像头失败：HTTP ${created.status()} ${await created.text()}`)
  const createdBody = await created.json()
  if (createdBody.runtime_mode !== 'REAL' || createdBody.is_simulated !== false || createdBody.source_type_provenance !== 'opencv_camera') {
    throw new Error(`摄像头来源证明不完整：${JSON.stringify(createdBody)}`)
  }
  cameraId = createdBody.id
  frontend.camera_id = cameraId
  frontend.source_type = createdBody.source_type_provenance
  const zone = await context.request.post(`/api/cameras/${encodeURIComponent(cameraId)}/zones`, { data: {
    name: '真实画面验收区域', points: [[0.05, 0.08], [0.95, 0.08], [0.95, 0.92], [0.05, 0.92]], priority: 10, enabled: true,
  } })
  if (!zone.ok()) throw new Error(`创建区域失败：HTTP ${zone.status()} ${await zone.text()}`)
  frontend.zone_id = (await zone.json()).id

  progress('ui-test-c')
  await page.goto('/camera-diagnostics')
  const cameraSelect = page.getByLabel('完整链路摄像头')
  await cameraSelect.selectOption(cameraId)
  await page.getByRole('button', { name: '开始测试 C' }).click()
  await page.getByText(/系统摄像头连接成功：连续解码/).waitFor({ timeout: 25_000 })
  frontend.ui_test_c_success = true

  // Keep the proven test-C preview mounted. Navigating away while this MJPEG
  // response is open can make Chromium wait indefinitely for network teardown.
  progress('continuous-browser-display')
  const image = page.getByAltText('测试 C 的后端实时连续画面')
  await image.waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForFunction(() => {
    const value = document.querySelector('img[alt="测试 C 的后端实时连续画面"]')
    return value instanceof HTMLImageElement && value.naturalWidth > 0 && value.naturalHeight > 0
  }, undefined, { timeout: 15_000 })
  const dimensions = await image.evaluate((value) => ({ width: value.naturalWidth, height: value.naturalHeight }))
  frontend.natural_width = dimensions.width
  frontend.natural_height = dimensions.height

  await page.evaluate((id) => {
    const state = { messages: 0, errors: 0, socket: null }
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${protocol}//${location.host}/ws/cameras/${encodeURIComponent(id)}`)
    state.socket = socket
    socket.onmessage = () => { state.messages += 1 }
    socket.onerror = () => { state.errors += 1 }
    window.__omRealCameraAcceptance = state
  }, cameraId)

  const hashes = new Set()
  const continuousStarted = Date.now()
  while ((Date.now() - continuousStarted) / 1000 < durationSeconds) {
    const sample = await page.evaluate(async (id) => {
      try {
        const response = await fetch(`/api/cameras/${encodeURIComponent(id)}/frame?t=${Date.now()}-${Math.random()}`, { cache: 'no-store' })
        if (!response.ok || !response.headers.get('content-type')?.includes('image/jpeg')) return { ok: false, status: response.status }
        const bytes = new Uint8Array(await response.arrayBuffer())
        let hash = 2166136261
        for (let offset = 0; offset < bytes.length; offset += 97) { hash ^= bytes[offset]; hash = Math.imul(hash, 16777619) }
        const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/jpeg' }))
        const result = { ok: bitmap.width > 0 && bitmap.height > 0, width: bitmap.width, height: bitmap.height, hash: hash >>> 0 }
        bitmap.close()
        return result
      } catch (error) { return { ok: false, error: String(error) } }
    }, cameraId)
    if (sample.ok) {
      frontend.decoded_frame_requests += 1
      hashes.add(sample.hash)
      frontend.natural_width = sample.width
      frontend.natural_height = sample.height
    } else frontend.failed_frame_requests += 1
    await page.waitForTimeout(1000)
  }
  frontend.continuous_display_seconds = Math.round((Date.now() - continuousStarted) / 100) / 10
  progress('continuous-browser-display-complete')
  frontend.distinct_frame_hashes = hashes.size
  const websocket = await page.evaluate(() => {
    const state = window.__omRealCameraAcceptance
    state?.socket?.close()
    return { messages: state?.messages || 0, errors: state?.errors || 0 }
  })
  frontend.websocket_messages = websocket.messages
  if (websocket.errors || websocket.messages < 30) throw new Error(`摄像头WebSocket不稳定：messages=${websocket.messages}, errors=${websocket.errors}`)
  if (frontend.decoded_frame_requests < 55 || frontend.failed_frame_requests > 0 || hashes.size < 2) {
    throw new Error(`60秒画面验收不足：decoded=${frontend.decoded_frame_requests}, failed=${frontend.failed_frame_requests}, unique=${hashes.size}`)
  }
  frontend.page_url = page.url()
  await page.screenshot({ path: screenshotPath, fullPage: true, timeout: 15_000 })

  progress('stop-release-restart')
  const stopped = await context.request.post(`/api/cameras/${encodeURIComponent(cameraId)}/stop`)
  if (!stopped.ok()) throw new Error(`停止摄像头失败：HTTP ${stopped.status()}`)
  frontend.stop_status_code = (await stopped.json()).health?.status_code
  const diagnostic = await context.request.get('/api/camera-diagnostics')
  const owners = diagnostic.ok() ? (await diagnostic.json()).owners || [] : []
  frontend.ownership_released = !owners.some((item) => item.key === `webcam:${deviceIndex}`)

  const restarted = await context.request.post(`/api/cameras/${encodeURIComponent(cameraId)}/start`)
  if (!restarted.ok()) throw new Error(`停止后再次启动失败：HTTP ${restarted.status()} ${await restarted.text()}`)
  await page.waitForTimeout(1200)
  const restartFrame = await context.request.get(`/api/cameras/${encodeURIComponent(cameraId)}/frame?t=${Date.now()}`)
  frontend.restart_success = restartFrame.ok() && (await restartFrame.body()).length > 100
  if (!frontend.restart_success) throw new Error('停止后再次启动未返回可解码JPEG')
  await context.request.post(`/api/cameras/${encodeURIComponent(cameraId)}/stop`)
  progress('complete')
} catch (error) {
  frontend.error = error instanceof Error ? error.message : String(error)
  frontend.page_url = page.url()
  frontend.ui_chain_message = await page.locator('.chain-result').textContent().catch(() => '') || ''
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {})
} finally {
  if (cameraId) {
    try { await context.request.post(`/api/cameras/${encodeURIComponent(cameraId)}/stop`) } catch { /* cleanup */ }
    try {
      const removed = await context.request.delete(`/api/cameras/${encodeURIComponent(cameraId)}`)
      frontend.cleanup_deleted = removed.ok()
    } catch { frontend.cleanup_deleted = false }
  }
  frontend.completed_at = new Date().toISOString()
  frontend.success = !frontend.error && frontend.runtime_mode === 'REAL' && frontend.source_type === 'opencv_camera' && frontend.is_simulated === false && frontend.ui_test_c_success && frontend.continuous_display_seconds >= 60 && frontend.failed_frame_requests === 0 && frontend.ownership_released && frontend.restart_success && frontend.cleanup_deleted
  await writeFile(reportPath, JSON.stringify({ frontend_chain_verified: frontend.success, frontend_chain: frontend }, null, 2), 'utf8')
  await context.close()
  await browser.close()
  process.stdout.write(JSON.stringify(frontend, null, 2) + '\n')
  if (!frontend.success) process.exitCode = 2
}
