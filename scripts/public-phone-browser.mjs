/** Actual React page + actual TEST API. No route interception or mock results. */
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const require = createRequire(resolve(root, 'apps/web/package.json'))
const { chromium, expect } = require('@playwright/test')
const { createServer } = await import(pathToFileURL(resolve(require.resolve('vite/package.json'), '../dist/node/index.js')).href)
const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, values) => {
  if (value.startsWith('--')) pairs.push([value.slice(2), values[index + 1]])
  return pairs
}, []))
const target = new URL(args.api)
assert.equal(target.hostname, '127.0.0.1', 'Only a specifically owned localhost TEST backend is allowed')
assert.equal(target.protocol, 'http:')
assert.match(args.item, /^[a-zA-Z0-9_-]+$/)
assert.match(args.camera, /^[a-zA-Z0-9_-]+$/)
const output = resolve(args.output)
const report = { passed: false, api_mock: false, video_source_only: true,
  physical_camera: false, document_navigation_count: 0, lifecycle: [], console_errors: [] }
let vite
let browser
try {
  const health = await (await fetch(new URL('/api/health', target))).json()
  assert.equal(health.runtime_mode, 'TEST', 'Never run this mutation harness against REAL/DEMO')
  process.env.OM_API_PORT = target.port
  vite = await createServer({ root: resolve(root, 'apps/web'), configFile: resolve(root, 'apps/web/vite.config.ts'),
    server: { host: '127.0.0.1', port: 0, strictPort: true }, logLevel: 'error' })
  await vite.listen()
  const address = vite.httpServer.address()
  assert.equal(typeof address, 'object')
  const url = `http://127.0.0.1:${address.port}`
  browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } })
  context.setDefaultTimeout(12000)
  context.setDefaultNavigationTimeout(12000)
  const page = await context.newPage()
  let searchPosts = 0
  let locationGets = 0
  page.on('request', request => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) report.document_navigation_count++
    if (request.url().endsWith('/api/search') && request.method() === 'POST') searchPosts++
    if (request.url().endsWith(`/api/items/${args.item}/last-location`) && request.method() === 'GET') locationGets++
  })
  page.on('pageerror', error => report.console_errors.push(error.message))
  const session = await context.request.get(`${url}/api/session`)
  assert.equal(session.status(), 200)
  const item = await (await context.request.get(`${url}/api/items/${args.item}`)).json()
  const initial = await (await context.request.get(`${url}/api/items/${args.item}/last-location`)).json()
  assert.equal(initial.runtime_mode, 'TEST')
  assert.equal(initial.current_state, null)
  const camera = await (await context.request.get(`${url}/api/cameras/${args.camera}`)).json()
  assert.equal(camera.source_type, 'video', 'Never open physical hardware from this public-file harness')
  await page.goto(`${url}/search?q=${encodeURIComponent(item.name)}`)
  const card = page.locator('.result-card')
  await expect(card).toContainText('还没有可验证记录', { timeout: 15000 })
  const beforeText = await card.innerText()
  const searchPostsBeforeStart = searchPosts
  const draft = '正在输入下一条查询，不应被轮询改掉'
  await page.getByRole('textbox', { name: '寻找物品', exact: true }).fill(draft)
  const start = await context.request.post(`${url}/api/cameras/${args.camera}/start`)
  assert.equal(start.status(), 200, await start.text())
  await expect(card.getByRole('img', { name: `${item.name}最后可见截图`, exact: true })).toBeVisible({ timeout: 12000 })
  await expect(card).toContainText('最后看到')
  await expect(card).toContainText('并不是“确认放下”')
  await expect(page.getByRole('textbox', { name: '寻找物品', exact: true })).toHaveValue(draft)
  assert.equal(searchPosts, searchPostsBeforeStart, 'Polling must use GET last-location, not resubmit search')
  assert.equal(report.document_navigation_count, 1, 'Only initial document navigation; no reload may produce the new result')
  assert.ok(locationGets >= 1, 'The visible page must perform a real location refresh')
  const image = card.getByRole('img', { name: `${item.name}最后可见截图`, exact: true })
  assert.equal(await image.evaluate(node => node.complete && node.naturalWidth > 0), true)
  const current = await (await context.request.get(`${url}/api/items/${args.item}/last-location`)).json()
  assert.equal(current.status, 'last_seen')
  assert.equal(current.source_type, 'video_file')
  assert.equal(current.is_simulated, true)
  assert.equal(current.last_confirmed_placement, null)
  const evidence = current.last_observed
  const bytes = await (await context.request.get(new URL(evidence.screenshot_path, url).href)).body()
  assert.equal(createHash('sha256').update(bytes).digest('hex'), evidence.screenshot_sha256)
  const events = await (await context.request.get(`${url}/api/events`)).json()
  assert.equal(events.length, 0)
  await mkdir(output, { recursive: true })
  await page.screenshot({ path: resolve(output, 'public-phone-auto-updated.png'), fullPage: true })
  report.passed = true
  Object.assign(report, { before_text: beforeText, after_text: await card.innerText(),
    query_draft_preserved: true, search_post_count_before: searchPostsBeforeStart,
    search_post_count_after: searchPosts, location_get_count: locationGets,
    current_state: current.current_state, evidence, events: events.length,
    screenshot: resolve(output, 'public-phone-auto-updated.png') })

  // A separate document preserves the search page's no-reload acceptance gate.
  // Observe the actual WebSocket transport only: never intercept routes,
  // replace pixels, synthesize ACKs, or inject candidate/model results.
  const livePage = await context.newPage()
  const actualSnapshots = new Map()
  const displayedFrames = new Map()
  const receivedFrames = []
  const acknowledgedFrames = []
  const wsErrors = []
  const wsStatuses = []
  let visionSockets = 0
  let snapshotFallbackResponses = 0
  const sampleStarted = performance.now()
  report.live_stream = { passed: false, transport: 'binary_websocket', api_mock: false,
    source: 'actual Playwright framereceived/framesent observations plus decoded DOM image',
    physical_camera: false, errors: wsErrors, statuses: wsStatuses,
    received_timeline: receivedFrames, ack_timeline: acknowledgedFrames, samples: [] }
  let liveNavigations = 0
  livePage.on('request', request => {
    if (request.isNavigationRequest() && request.frame() === livePage.mainFrame()) liveNavigations++
  })
  livePage.on('pageerror', error => report.console_errors.push(`live: ${error.message}`))
  livePage.on('response', response => {
    // A startup fallback can be observed, but cannot satisfy the WS gate.
    if (response.url().endsWith(`/api/cameras/${args.camera}/vision-snapshot`) && response.status() === 200) snapshotFallbackResponses++
  })
  livePage.on('websocket', socket => {
    if (new URL(socket.url()).pathname !== `/ws/cameras/${args.camera}/vision`) return
    visionSockets++
    socket.on('socketerror', error => { if (wsErrors.length < 20) wsErrors.push(String(error)) })
    socket.on('framereceived', ({ payload }) => {
      try {
        if (typeof payload === 'string') {
          if (wsStatuses.length < 10) wsStatuses.push(JSON.parse(payload))
          return
        }
        assert.ok(Buffer.isBuffer(payload) && payload.length >= 8 && payload.length <= 8 * 1024 * 1024 + 131076)
        const headerLength = payload.readUInt32BE(0)
        assert.ok(headerLength >= 2 && headerLength <= 131072 && headerLength + 7 <= payload.length)
        const snapshot = JSON.parse(payload.subarray(4, 4 + headerLength).toString('utf8'))
        const jpeg = payload.subarray(4 + headerLength)
        assert.equal(snapshot.type, 'frame'); assert.equal(snapshot.camera_id, args.camera)
        assert.equal(snapshot.runtime_mode, 'TEST'); assert.equal(snapshot.source_type, 'video_file'); assert.equal(snapshot.is_simulated, true)
        assert.equal(snapshot.display_only, true); assert.equal(snapshot.coordinate_space, 'source_normalized')
        assert.equal(typeof snapshot.source_session_id, 'string'); assert.ok(snapshot.source_session_id.length > 0)
        assert.ok(Number.isSafeInteger(snapshot.source_frame) && snapshot.source_frame >= 0)
        assert.ok(Number.isFinite(Date.parse(snapshot.source_timestamp)))
        assert.ok(Number.isInteger(snapshot.width) && snapshot.width > 0 && Number.isInteger(snapshot.height) && snapshot.height > 0)
        assert.ok(Array.isArray(snapshot.candidates)); assert.deepEqual([...jpeg.subarray(0, 3)], [255, 216, 255])
        const key = `${snapshot.source_session_id}:${snapshot.source_frame}`
        const record = { ...snapshot, jpeg_sha256: createHash('sha256').update(jpeg).digest('hex'),
          jpeg_bytes: jpeg.length, received_at_ms: performance.now() - sampleStarted }
        actualSnapshots.set(key, record)
        while (actualSnapshots.size > 240) actualSnapshots.delete(actualSnapshots.keys().next().value)
        if (receivedFrames.length < 1000) receivedFrames.push({ key, source_frame: snapshot.source_frame,
          source_session_id: snapshot.source_session_id, source_timestamp: snapshot.source_timestamp,
          jpeg_sha256: record.jpeg_sha256, at_ms: record.received_at_ms })
        report.live_stream.received_frames = receivedFrames.length
      } catch (error) { if (wsErrors.length < 20) wsErrors.push(String(error.message || error)) }
    })
    socket.on('framesent', ({ payload }) => {
      try {
        const ack = JSON.parse(typeof payload === 'string' ? payload : payload.toString('utf8'))
        assert.ok(Number.isSafeInteger(ack.ack) && typeof ack.source_session_id === 'string')
        const key = `${ack.source_session_id}:${ack.ack}`
        assert.ok(actualSnapshots.has(key), 'The application ACK must belong to an actually received source frame')
        if (acknowledgedFrames.length < 1000) acknowledgedFrames.push({ key, source_frame: ack.ack, source_session_id: ack.source_session_id, at_ms: performance.now() - sampleStarted })
        report.live_stream.acknowledged_frames = acknowledgedFrames.length
      } catch (error) { if (wsErrors.length < 20) wsErrors.push(String(error.message || error)) }
    })
  })
  await livePage.goto(`${url}/live?camera=${encodeURIComponent(args.camera)}`)
  const panel = livePage.getByRole('region', { name: '同帧物品和手部关键点', exact: true })
  await expect(panel).toBeVisible()
  await expect(panel.getByRole('checkbox', { name: '显示识别框', exact: true })).toBeChecked()
  let binding
  await expect.poll(async () => {
    const displayed = await panel.evaluate(async (element, expectedItemName) => {
      const image = element.querySelector('img[alt="同帧识别原图"]')
      const svg = element.querySelector('svg[aria-label="真实关键点叠加"]')
      if (!image || !svg || !image.complete || !image.naturalWidth || getComputedStyle(image).visibility !== 'visible') return null
      const group = [...svg.querySelectorAll('g[aria-label]')].find(node => {
        const label = node.getAttribute('aria-label')
        return label === '候选手机（身份未确认）' || label === `已匹配 ${expectedItemName}`
      })
      const box = group?.querySelector('rect')
      if (!box) return null
      const rectangle = node => {
        const value = node.getBoundingClientRect()
        return { x: value.x, y: value.y, width: value.width, height: value.height }
      }
      const source = image.getAttribute('src') || ''
      if (!source.startsWith('blob:')) return null
      // Snapshot ALL geometry synchronously before awaiting a Blob read. A
      // later frame may render while hashing, but cannot change this sample.
      const sample = {
        natural_width: image.naturalWidth, natural_height: image.naturalHeight,
        image_rect: rectangle(image), svg_rect: rectangle(svg), box_rect: rectangle(box),
        viewport: { width: innerWidth, height: innerHeight, scroll_x: scrollX, scroll_y: scrollY },
        primary_image_count: [...document.querySelectorAll('.live-recognition-pane > .vision-frame > .vision-frame-stage img')].filter(node => getComputedStyle(node).visibility === 'visible').length,
        primary_image_buffers: document.querySelectorAll('.live-recognition-pane > .vision-frame > .vision-frame-stage img').length,
        redundant_mjpeg_images: document.querySelectorAll('img[src*="/stream"]').length,
        page_width: document.documentElement.scrollWidth,
        recognition_rect: rectangle(document.querySelector('.live-recognition-pane')),
        bbox_pixels: ['x', 'y', 'width', 'height'].map(key => Number(box.getAttribute(key))),
        view_box: svg.getAttribute('viewBox').split(/\s+/).map(Number),
        frame_number: Number(image.dataset.sourceFrame), source_session_id: image.dataset.sourceSession,
        displayed_fps: Number(element.textContent.match(/实际显示\s+([\d.]+)\s+FPS/)?.[1]),
        observed_at_ms: performance.now(),
        label: group.getAttribute('aria-label'), panel_text: element.innerText,
      }
      try {
        // Reading this actual, decoded img's Blob is read-only. No image is
        // replaced or drawn over and no application function is called.
        const decodedBytes = await (await fetch(source)).arrayBuffer()
        const digest = await crypto.subtle.digest('SHA-256', decodedBytes)
        return { ...sample, decoded_image_sha256: [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join(''), decoded_image_bytes: decodedBytes.byteLength }
      } catch { return null } // A revoked/superseded Blob is not evidence.
    }, item.name)
    if (!displayed || displayed.image_rect.width <= 0 || displayed.image_rect.height <= 0) return false
    const key = `${displayed.source_session_id}:${displayed.frame_number}`
    const snapshot = actualSnapshots.get(key)
    if (!snapshot) return false
    assert.equal(displayed.decoded_image_sha256, snapshot.jpeg_sha256, 'Decoded DOM image must be the exact JPEG received with this source frame')
    assert.equal(displayed.decoded_image_bytes, snapshot.jpeg_bytes)
    assert.ok(Number.isFinite(displayed.displayed_fps), 'The actual continuous-display FPS label must be finite')
    const candidate = snapshot.candidates.find(value => {
      const phone = value.category === 'cell phone' || (value.accepted && value.best_item_id === args.item)
      return phone && value.category_evidence !== 'geometry_only' && value.proposal_backend !== 'phone_shape_proposal'
        && value.bbox.every((number, index) => Math.abs(number * (index % 2 === 0 ? snapshot.width : snapshot.height) - displayed.bbox_pixels[index]) < .001)
    })
    if (!candidate) return false
    assert.equal(snapshot.runtime_mode, 'TEST')
    assert.equal(snapshot.source_type, 'video_file')
    assert.equal(snapshot.is_simulated, true)
    assert.equal(displayed.natural_width, snapshot.width)
    assert.equal(displayed.natural_height, snapshot.height)
    assert.deepEqual(displayed.view_box, [0, 0, snapshot.width, snapshot.height])
    assert.equal(displayed.viewport.scroll_y, 0, 'Desktop acceptance must not scroll the recognition panel into view')
    assert.ok(displayed.image_rect.y >= 0 && displayed.image_rect.y + displayed.image_rect.height <= displayed.viewport.height,
      'The decoded recognition image must fit the first desktop viewport')
    assert.ok(displayed.box_rect.y >= 0 && displayed.box_rect.y + displayed.box_rect.height <= displayed.viewport.height,
      'The source-bound phone box must be inside the first desktop viewport')
    assert.equal(displayed.primary_image_count, 1, 'Desktop must have exactly one visible primary source image')
    assert.ok(displayed.primary_image_buffers >= 1 && displayed.primary_image_buffers <= 2, 'Decoded display may use at most two image buffers')
    assert.equal(displayed.redundant_mjpeg_images, 0, 'Do not download a second raw video stream')
    assert.ok(displayed.page_width <= displayed.viewport.width + 1, 'Desktop page must not overflow horizontally')
    for (const dimension of ['x', 'y', 'width', 'height']) assert.ok(Math.abs(displayed.image_rect[dimension] - displayed.svg_rect[dimension]) < 1, `Image/SVG ${dimension} offset`)
    const normalized = [
      (displayed.box_rect.x - displayed.image_rect.x) / displayed.image_rect.width,
      (displayed.box_rect.y - displayed.image_rect.y) / displayed.image_rect.height,
      displayed.box_rect.width / displayed.image_rect.width,
      displayed.box_rect.height / displayed.image_rect.height,
    ]
    candidate.bbox.forEach((value, index) => assert.ok(Math.abs(value - normalized[index]) < .003, `Rendered bbox dimension ${index} does not map to the source frame`))
    displayedFrames.set(key, { key, source_frame: snapshot.source_frame, source_session_id: snapshot.source_session_id,
      source_timestamp: snapshot.source_timestamp, jpeg_sha256: snapshot.jpeg_sha256,
      displayed_fps: displayed.displayed_fps, browser_observed_at_ms: displayed.observed_at_ms,
      received_at_ms: snapshot.received_at_ms, bbox: candidate.bbox, tracker_backend: snapshot.tracker_backend,
      candidate_backend: candidate.proposal_backend, category_evidence: candidate.category_evidence,
      accepted_identity: candidate.accepted === true, best_item_id: candidate.best_item_id,
      natural_width: displayed.natural_width, natural_height: displayed.natural_height,
      image_decoded: true, same_frame_binding: true })
    const geometry = displayed
    binding = { passed: true, api_mock: false, model_mock: false, physical_camera: false, source_type: snapshot.source_type,
      runtime_mode: snapshot.runtime_mode, is_simulated: snapshot.is_simulated, source_session_id: snapshot.source_session_id,
      source_frame: snapshot.source_frame, source_timestamp: snapshot.source_timestamp,
      decoded_image_sha256: displayed.decoded_image_sha256, transport: 'binary_websocket',
      candidate, geometry, default_visible_without_controls: true, first_viewport_visible: true,
      desktop_side_by_side: false, desktop_single_primary: true, image_decoded: true, same_frame_binding: true }
    const samples = [...displayedFrames.values()]
    const distinctPixels = new Set(samples.map(value => value.jpeg_sha256)).size
    Object.assign(report.live_stream, { samples, unique_displayed_source_frames: samples.length, unique_displayed_jpegs: distinctPixels })
    // Twenty different frame IDs alone are insufficient: require actual JPEG
    // content changes and two seconds of observations, not a fixed sleep.
    return samples.length >= 20 && distinctPixels >= 20
      && samples.at(-1).browser_observed_at_ms - samples[0].browser_observed_at_ms >= 2000
  }, { timeout: 16000, intervals: [20, 30, 50], message: 'Actual WS must show at least 20 distinct decoded phone frames, each bound to its own source metadata and bbox' }).toBe(true)
  assert.deepEqual(wsErrors, [])
  const frameSamples = [...displayedFrames.values()]
  const fps = frameSamples.map(value => value.displayed_fps).sort((a, b) => a - b)
  const rate = values => values.length > 1 && values.at(-1).at_ms > values[0].at_ms ? (values.length - 1) * 1000 / (values.at(-1).at_ms - values[0].at_ms) : null
  Object.assign(report.live_stream, { passed: true, websocket_connections: visionSockets,
    http_snapshot_fallback_responses: snapshotFallbackResponses,
    received_frames: receivedFrames.length, acknowledged_frames: acknowledgedFrames.length,
    unique_displayed_source_frames: frameSamples.length, unique_displayed_jpegs: new Set(frameSamples.map(value => value.jpeg_sha256)).size,
    samples: frameSamples, received_timeline: receivedFrames, ack_timeline: acknowledgedFrames,
    ui_displayed_fps: { min: fps[0], median: fps[Math.floor(fps.length / 2)], max: fps.at(-1) },
    received_rate_fps: rate(receivedFrames), application_decode_ack_rate_fps: rate(acknowledgedFrames),
    sampled_duration_ms: frameSamples.at(-1).browser_observed_at_ms - frameSamples[0].browser_observed_at_ms,
    target_fps: 20, target_fps_met: fps[Math.floor(fps.length / 2)] >= 20,
    performance_scope: 'Measured in this headless public-file browser run. Distinct-frame count is not FPS and does not establish physical-camera performance.',
    same_frame_binding: true, real_jpeg_decoded: true, continuous_identity_reinference: false })
  assert.equal(liveNavigations, 1)
  assert.equal(report.document_navigation_count, 1)
  await expect(panel).toContainText('测试视频')
  await livePage.screenshot({ path: resolve(output, 'public-phone-live-box.png'), fullPage: true })
  Object.assign(binding, { document_navigation_count: liveNavigations, screenshot: resolve(output, 'public-phone-live-box.png'),
    screenshot_note: 'Live UI screenshot; exact source-frame binding is independently recorded by decoded DOM Blob hashes and observed WebSocket metadata above.' })
  await livePage.setViewportSize({ width: 390, height: 920 })
  const jump = livePage.getByRole('link', { name: '查看物品识别', exact: true })
  await expect(jump).toBeVisible()
  const mobileLayout = await livePage.locator('.live-view-single').evaluate(element => {
    const recognition = element.querySelector('.live-recognition-pane').getBoundingClientRect()
    return { recognition_x: recognition.x, recognition_y: recognition.y, recognition_width: recognition.width,
      primary_images: [...element.querySelectorAll('.live-recognition-pane > .vision-frame > .vision-frame-stage img')].filter(node => getComputedStyle(node).visibility === 'visible').length,
      primary_image_buffers: element.querySelectorAll('.live-recognition-pane > .vision-frame > .vision-frame-stage img').length,
      redundant_mjpeg_images: element.querySelectorAll('img[src*="/stream"]').length,
      page_width: document.documentElement.scrollWidth, viewport_width: innerWidth }
  })
  assert.equal(mobileLayout.primary_images, 1)
  assert.ok(mobileLayout.primary_image_buffers >= 1 && mobileLayout.primary_image_buffers <= 2)
  assert.equal(mobileLayout.redundant_mjpeg_images, 0)
  assert.ok(mobileLayout.recognition_width > 0)
  assert.ok(mobileLayout.page_width <= mobileLayout.viewport_width + 1, 'Mobile page must not overflow horizontally')
  await jump.click()
  await expect(livePage).toHaveURL(/#live-recognition$/)
  await expect(panel).toBeInViewport()
  binding.mobile_layout = { passed: true, ...mobileLayout, anchor_reaches_recognition: true }
  const afterLive = await (await context.request.get(`${url}/api/items/${args.item}/last-location`)).json()
  const afterLiveEvents = await (await context.request.get(`${url}/api/events`)).json()
  assert.equal(afterLive.runtime_mode, 'TEST'); assert.equal(afterLive.source_type, 'video_file'); assert.equal(afterLive.is_simulated, true)
  assert.equal(afterLive.last_confirmed_placement, null); assert.equal(afterLiveEvents.length, 0, 'Viewing/ACKing tracking frames cannot create placement or movement history')
  report.live_stream.after_viewing = { runtime_mode: afterLive.runtime_mode, source_type: afterLive.source_type,
    is_simulated: afterLive.is_simulated, movement_events: afterLiveEvents.length, last_confirmed_placement: null,
    current_state: afterLive.current_state }
  report.live_recognition = binding
  await writeFile(resolve(output, 'public-phone-live-box.json'), JSON.stringify(binding, null, 2))
  assert.deepEqual(report.console_errors, [])
} catch (error) {
  report.passed = false
  report.error = String(error.stack || error)
  process.exitCode = 1
} finally {
  if (browser) { await browser.close(); report.lifecycle.push('owned_browser_closed') }
  if (vite) { await vite.close(); report.lifecycle.push('owned_vite_closed') }
  await mkdir(output, { recursive: true })
  await writeFile(resolve(output, 'public-phone-browser.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: report.passed, error: report.error, report: resolve(output, 'public-phone-browser.json') }))
}
