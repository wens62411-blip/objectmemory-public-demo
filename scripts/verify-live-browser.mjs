/** Read-only production-page probe. No camera start, API mocks, or image storage. */
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
const root = fileURLToPath(new URL('..', import.meta.url))
const require = createRequire(resolve(root, 'apps/web/package.json'))
const { chromium } = require('@playwright/test')
const [url = 'http://127.0.0.1:8018', destination = 'data/verification/core-vision/real-browser.json', secondsText = '30'] = process.argv.slice(2)
const sampleSeconds = Number(secondsText)
assert.ok(Number.isInteger(sampleSeconds) && sampleSeconds >= 5 && sampleSeconds <= 60)
const performanceAcceptance = sampleSeconds >= 20
const requiredSamples = Math.min(20, Math.max(3, Math.floor(sampleSeconds * 2)))
assert.equal(new URL(url).hostname, '127.0.0.1')
assert.equal(new URL(url).protocol, 'http:')
const output = resolve(root, destination)
assert.ok(output.startsWith(resolve(root, 'data/verification') + sep))
const report = { started_at: new Date().toISOString(), api_mock: false, read_only: true,
  images_saved: 0, physical_item_accuracy_proven: false, errors: [], writes: [], views: [],
  requested_sample_seconds_per_view: sampleSeconds, acceptance_scope: performanceAcceptance ? 'live_browser_performance' : 'short_same_frame_smoke_only',
  passing_performance_floor_fps: 20, strict_requested_target_fps: 30,
  passed: false, completed: false, partial: true, stage: 'browser_launch', failed_stage: null }
let browser
try {
  browser = await chromium.launch({ headless: true })
  report.stage = 'context_create'
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const servedAssets = async () => {
    const response = await context.request.get(url + '/')
    assert.equal(response.status(), 200)
    return [...(await response.text()).matchAll(/(?:src|href)="(\/assets\/[^"?#]+)"/g)].map(match => match[1]).sort()
  }
  report.served_assets_before = await servedAssets()
  report.stage = 'session_read'
  assert.equal((await (await context.request.get(url + '/api/session')).json()).runtime_mode, 'REAL')
  report.stage = 'camera_read'
  const cameras = await (await context.request.get(url + '/api/cameras')).json()
  const camera = cameras.find(row => row.enabled && row.source_type === 'webcam')
  assert.ok(camera, 'No existing enabled physical camera; this probe never starts hardware')
  report.camera_id = camera.id
  const captureHealth = async () => {
    const rows = await (await context.request.get(url + '/api/cameras')).json()
    const health = rows.find(row => row.id === camera.id)?.health || {}
    return Object.fromEntries(['status', 'source_session_id', 'frames_read', 'capture_fps',
      'raw_read_fps', 'unique_pixel_fps', 'duplicate_source_reads',
      'pixel_cadence_is_exposure_proof', 'deduplicate_identical_frames',
      'preview_fps', 'inference_fps', 'requested_capture_fps', 'reported_capture_fps',
      'backend', 'reconnects', 'preview_frame_sequence'].map(key => [key, health[key] ?? null]))
  }
  report.stage = 'events_before_read'
  report.events_before = (await (await context.request.get(url + '/api/events')).json()).length
  for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile_layout', { width: 390, height: 844 }]]) {
    const packets = new Map(), acknowledgements = [], samples = [], errors = []
    const value = { name, emulated_layout_not_mobile_hardware: name === 'mobile_layout', viewport,
      sockets: 0, decoded_ack_frames: 0, decoded_ack_fps: 0, samples, errors, loaded_assets: [],
      acknowledgements, source_packets: [], received_frames: 0,
      completed: false, partial: true, stage: 'page_create', failed_stage: null,
      passed_same_frame: false, target_20_fps_met: false, target_30_fps_met: false }
    report.views.push(value)
    report.stage = name
    value.capture_before = await captureHealth()
    let page
    const start = performance.now()
    try {
    page = await context.newPage()
    await page.setViewportSize(viewport)
    await page.addInitScript(() => {
      const result = { started: false, ticks: 0, blank_ticks: 0, multiple_visible_ticks: 0, distinct_image_nodes: 0, displayed_frame_changes: 0, first_paint_ms: null, last_paint_ms: null }
      const nodes = new WeakSet(); let previousKey = ''
      window.__objectMemoryPaint = result
      function samplePaint() {
        const images = [...document.querySelectorAll('.live-recognition-pane .vision-frame-primary .vision-frame-image > img')]
        const visible = images.filter(image => image.complete && image.naturalWidth > 0 && getComputedStyle(image).visibility === 'visible' && getComputedStyle(image).display !== 'none')
        if (visible.length) result.started = true
        if (result.started) {
          result.first_paint_ms ??= performance.now(); result.last_paint_ms = performance.now()
          result.ticks++
          if (!visible.length) result.blank_ticks++
          if (visible.length > 1) result.multiple_visible_ticks++
          for (const image of images) if (!nodes.has(image)) { nodes.add(image); result.distinct_image_nodes++ }
          if (visible.length === 1) {
            const key = `${visible[0].dataset.sourceSession}:${visible[0].dataset.sourceFrame}`
            if (key !== previousKey) { result.displayed_frame_changes++; previousKey = key }
          }
        }
        requestAnimationFrame(samplePaint)
      }
      requestAnimationFrame(samplePaint)
    })
    page.on('pageerror', error => errors.push(error.message))
    page.on('request', request => {
      if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) report.writes.push(new URL(request.url()).pathname)
      const requested = new URL(request.url())
      if (requested.origin === url && requested.pathname.startsWith('/assets/') && !value.loaded_assets.includes(requested.pathname)) value.loaded_assets.push(requested.pathname)
    })
    page.on('websocket', socket => {
      if (!socket.url().endsWith(`/ws/cameras/${camera.id}/vision`)) return
      value.sockets++
      socket.on('socketerror', error => errors.push(String(error)))
      socket.on('framereceived', ({ payload }) => {
        try {
          if (typeof payload === 'string') { errors.push('source_unavailable'); return }
          const size = payload.readUInt32BE(0)
          assert.ok(size >= 2 && size <= 131072 && size + 7 < payload.length)
          const meta = JSON.parse(payload.subarray(4, 4 + size).toString('utf8'))
          const source = { camera_id: meta.camera_id, runtime_mode: meta.runtime_mode,
            source_type: meta.source_type, is_simulated: meta.is_simulated,
            source_session_id: meta.source_session_id, source_frame: meta.source_frame,
            source_timestamp: meta.source_timestamp, width: meta.width, height: meta.height,
            jpeg_sha256: createHash('sha256').update(payload.subarray(4 + size)).digest('hex'),
            at_ms: performance.now() - start, validated: false }
          value.received_frames++
          value.source_packets.push(source)
          if (value.source_packets.length > 150) value.source_packets.shift()
          assert.equal(meta.runtime_mode, 'REAL'); assert.equal(meta.source_type, 'opencv_camera'); assert.equal(meta.is_simulated, false)
          source.validated = true
          const key = `${meta.source_session_id}:${meta.source_frame}`
          packets.set(key, { meta, hash: source.jpeg_sha256 })
          while (packets.size > 150) packets.delete(packets.keys().next().value)
        } catch (error) { errors.push(error.message) }
      })
      socket.on('framesent', ({ payload }) => {
        try {
          const ack = JSON.parse(payload.toString())
          const matched = packets.has(`${ack.source_session_id}:${ack.ack}`)
          acknowledgements.push({ source_session_id: ack.source_session_id, source_frame: ack.ack,
            at_ms: performance.now() - start, matched_source_packet: matched })
          assert.ok(matched)
        } catch (error) { errors.push(error.message) }
      })
    })
    value.stage = 'navigation'
    await page.goto(`${url}/live?camera=${camera.id}`)
    value.stage = 'first_decoded_image'
    await page.locator('img[alt="同帧识别原图"][src^="blob:"]').waitFor({ state: 'visible', timeout: 15000 })
    value.stage = 'same_frame_sampling'
    const until = performance.now() + sampleSeconds * 1000
    while (performance.now() < until) {
      const sample = await page.evaluate(async () => {
        const image = document.querySelector('img[alt="同帧识别原图"][src^="blob:"]')
        if (!image?.complete || !image.naturalWidth || getComputedStyle(image).visibility !== 'visible') return null
        const src = image.src, rect = image.getBoundingClientRect()
        const row = { key: `${image.dataset.sourceSession}:${image.dataset.sourceFrame}`,
          dimensions: [image.naturalWidth, image.naturalHeight], width: rect.width, height: rect.height,
          viewport: [innerWidth, innerHeight], horizontal_overflow: document.documentElement.scrollWidth > innerWidth + 1,
          default_shape_overlay_count: document.querySelectorAll('.live-recognition-pane svg[aria-label="真实关键点叠加"] g[aria-label^="外形像手机"]').length,
          displayed_fps: Number(document.body.innerText.match(/实际显示\s+([\d.]+)\s+FPS/)?.[1]) }
        try {
          const bytes = await (await fetch(src)).arrayBuffer()
          row.hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(x => x.toString(16).padStart(2, '0')).join('')
          return row
        } catch { return null }
      })
      if (sample && packets.has(sample.key)) {
        const packet = packets.get(sample.key)
        value.pending_sample = { ...sample, source_jpeg_sha256: packet.hash,
          source_dimensions: [packet.meta.width, packet.meta.height], at_ms: performance.now() - start }
        assert.equal(sample.hash, packet.hash, 'Actually decoded DOM image must match source JPEG')
        assert.deepEqual(sample.dimensions, [packet.meta.width, packet.meta.height])
        assert.equal(sample.horizontal_overflow, false)
        assert.equal(sample.default_shape_overlay_count, 0, 'Default live overlay must not present geometry-only hints as object detections')
        sample.source_geometry_candidates = (packet.meta.candidates || []).filter(candidate => candidate.category_evidence === 'geometry_only' || candidate.proposal_backend === 'phone_shape_proposal').length
        samples.push({ ...sample, at_ms: performance.now() - start })
        delete value.pending_sample
      }
      // Sampling cadence only; success depends on actual decoded hashes and ACKs.
      await new Promise(done => setTimeout(done, 250))
    }
    value.paint = await page.evaluate(() => window.__objectMemoryPaint)
    const paintElapsed = value.paint.last_paint_ms - value.paint.first_paint_ms
    value.paint.visible_source_frame_fps = paintElapsed > 0 ? Math.max(0, value.paint.displayed_frame_changes - 1) * 1000 / paintElapsed : 0
    value.capture_after = await captureHealth()
    value.passed_paint_continuity = value.paint.ticks >= 100 && value.paint.blank_ticks === 0 && value.paint.multiple_visible_ticks === 0
    value.source_geometry_samples = samples.filter(sample => sample.source_geometry_candidates > 0).length
    value.passed_default_shape_visibility = samples.length >= requiredSamples && samples.every(sample => sample.default_shape_overlay_count === 0)
    value.completed = true; value.partial = false; value.stage = 'page_close'
    } catch (error) {
      value.failed_stage = value.stage; errors.push(error.message)
      throw error
    } finally {
      try { await page?.close() } catch (error) {
        value.failed_stage ??= 'page_close'; errors.push(error.message)
      }
      const validAcks = acknowledgements.filter(ack => ack.matched_source_packet)
      const rate = validAcks.length > 1 ? (validAcks.length - 1) * 1000 / (validAcks.at(-1).at_ms - validAcks[0].at_ms) : 0
      value.decoded_ack_frames = validAcks.length; value.decoded_ack_fps = rate
      value.passed_same_frame = value.completed && samples.length >= requiredSamples && errors.length === 0
      // Decode ACKs alone do not establish visible refresh. Do not silently
      // turn a 29 FPS result into a 30 FPS pass by applying an unnamed tolerance.
      const visibleRate = value.paint?.visible_source_frame_fps ?? 0
      value.target_20_fps_met = value.completed && rate >= 20 && visibleRate >= 20
      value.target_30_fps_met = value.completed && rate >= 30 && visibleRate >= 30
      if (!value.failed_stage && !value.passed_same_frame) value.failed_stage = 'same_frame_acceptance'
      if (!value.failed_stage && !value.passed_paint_continuity) value.failed_stage = 'paint_continuity_acceptance'
      if (performanceAcceptance && !value.failed_stage && !value.target_20_fps_met) value.failed_stage = '20_fps_acceptance'
      value.stage = value.failed_stage ? 'failed' : 'complete'
      value.ended_at = new Date().toISOString()
    }
  }
  report.stage = 'events_after_read'
  report.events_after = (await (await context.request.get(url + '/api/events')).json()).length
  report.served_assets_after = await servedAssets()
  report.same_build_throughout_measurement = JSON.stringify(report.served_assets_before) === JSON.stringify(report.served_assets_after)
  report.completed = true; report.partial = false
  report.passed = report.same_build_throughout_measurement && report.writes.length === 0 && report.views.every(view => view.passed_same_frame && (!performanceAcceptance || view.target_20_fps_met) && view.passed_paint_continuity && view.passed_default_shape_visibility)
  if (!report.passed) report.failed_stage = report.writes.length ? 'read_only_acceptance' : 'view_acceptance'
} catch (error) { report.failed_stage = report.stage; report.errors.push(error.message); report.passed = false }
finally {
  try { await browser?.close() } catch (error) {
    report.failed_stage ??= 'browser_close'; report.errors.push(error.message); report.passed = false
  }
  report.stage = report.passed ? 'complete' : 'failed'
  report.ended_at = new Date().toISOString()
  await mkdir(resolve(output, '..'), { recursive: true })
  await writeFile(output, JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: report.passed, partial: report.partial, failed_stage: report.failed_stage, errors: report.errors,
    views: report.views.map(({ samples, source_packets, acknowledgements, pending_sample, ...rest }) => rest) }))
  if (!report.passed) process.exitCode = 1
}
