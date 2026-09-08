import { expect, test, type APIResponse } from '@playwright/test'
import { createHash } from 'node:crypto'
import { execFile as execFileCallback } from 'node:child_process'
import { readFile, realpath } from 'node:fs/promises'
import { basename, resolve, relative, isAbsolute } from 'node:path'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'

const execFile = promisify(execFileCallback)
const projectRoot = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../..')
const python = process.env.OM_E2E_PYTHON || resolve(projectRoot, '.venv', 'Scripts', 'python.exe')

async function optionalHash(path: string) {
  try { return createHash('sha256').update(await readFile(path)).digest('hex') } catch { return null }
}

async function sqliteEvidence(database: string, eventId?: string) {
  const code = "import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;event=dict(c.execute('SELECT event_id,runtime_mode,source_type,is_simulated,source_session_id,from_zone,to_zone,screenshot_sha256,clip_sha256 FROM movement_events WHERE event_id=?',(sys.argv[2],)).fetchone() or {});state=dict(c.execute('SELECT item_id,current_zone,evidence_event_id,runtime_mode,source_type,is_simulated,status FROM item_current_state WHERE item_id=?',('demo-phone',)).fetchone() or {});print(json.dumps({'events':c.execute('SELECT COUNT(*) FROM movement_events').fetchone()[0],'media':c.execute('SELECT COUNT(*) FROM event_media').fetchone()[0],'event':event,'state':state}));c.close()"
  const { stdout } = await execFile(python, ['-c', code, database, eventId || ''], { cwd: projectRoot })
  return JSON.parse(stdout) as { events: number; media: number; event: Record<string, unknown>; state: Record<string, unknown> }
}

async function body<T>(response: APIResponse) {
  if (!response.ok()) {
    const responseBody = await response.text()
    throw new Error(`${response.url()} 应返回成功响应，实际为 HTTP ${response.status()}：${responseBody}`)
  }
  return response.json() as Promise<T>
}

test.describe.configure({ mode: 'serial' })

test('真实 FastAPI + 真实文件视频：DEMO 证据全程标记为模拟来源', async ({ page, request }) => {
  test.setTimeout(120_000)
  const canonicalReal = resolve(projectRoot, 'data', 'database', 'objectmemory.sqlite')
  const canonicalHashBefore = await optionalHash(canonicalReal)
  const session = await body<{ authenticated: boolean; local: boolean }>(await request.get('/api/session'))
  expect(session).toMatchObject({ authenticated: true, local: true })
  const runtime = await body<{ runtime_mode: string; is_simulated: boolean }>(await request.get('/api/runtime-config'))
  expect(runtime).toMatchObject({ runtime_mode: 'DEMO', is_simulated: true })
  const diagnostics = await body<{ database_path: string; media_dir: string; runtime_mode: string }>(await request.get('/api/system/diagnostics'))
  const databasePath = await realpath(diagnostics.database_path)
  const isolatedRoot = await realpath(resolve(databasePath, '..', '..'))
  const temporaryRoot = await realpath(resolve(projectRoot, 'data', 'temporary'))
  const isolatedRelative = relative(temporaryRoot, isolatedRoot)
  expect(isolatedRelative.startsWith('..') || isAbsolute(isolatedRelative)).toBe(false)
  expect(basename(isolatedRoot)).toMatch(/^playwright-demo-/)
  const databaseRelative = relative(isolatedRoot, databasePath)
  expect(databaseRelative.startsWith('..') || isAbsolute(databaseRelative)).toBe(false)
  expect(databasePath.endsWith('objectmemory-demo.sqlite')).toBe(true)
  expect(diagnostics.runtime_mode).toBe('DEMO')
  expect(await body<unknown[]>(await request.get('/api/events?limit=100'))).toHaveLength(0)

  await page.goto('/')
  await expect(page.getByText('演示模式，当前事件不来自真实家庭摄像头。', { exact: true })).toBeVisible()
  await page.goto('/cameras?add=1')
  await expect(page.getByText('演示模式，当前事件不来自真实家庭摄像头。', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /视频文件回放/ })).toBeVisible()

  const seeded = await body<{ camera_id: string; runtime_mode: string; source_type: string; is_simulated: boolean }>(await request.post('/api/system/demo-seed'))
  expect(seeded).toMatchObject({ runtime_mode: 'DEMO', source_type: 'demo_seed', is_simulated: true })
  expect(await body<unknown[]>(await request.get('/api/events?limit=100'))).toHaveLength(0)

  const startedAt = new Date().toISOString()
  const started = await request.post(`/api/cameras/${encodeURIComponent(seeded.camera_id)}/start`)
  expect(started.ok()).toBeTruthy()
  try {
    await expect.poll(async () => {
      const events = await body<Array<{ item_id: string; timestamp_start: string; evidence_status: string; source_type: string; runtime_mode: string; is_simulated: boolean }>>(await request.get(`/api/events?camera_id=${encodeURIComponent(seeded.camera_id)}&limit=100`))
      return events.filter((event) => event.item_id === 'demo-phone' && event.timestamp_start >= startedAt && event.evidence_status === 'confirmed')
    }, { timeout: 75_000, intervals: [500, 1000, 2000] }).toHaveLength(1)

    const demoEvents = await body<Array<{
      id: string; event_id: string; movement_session_id: string; item_id: string; event_type: string; from_zone: string; to_zone: string; timestamp_start: string
      runtime_mode: string; source_type: string; is_simulated: boolean; source_session_id: string; source_frame_start: number; source_frame_end: number
      screenshot_path: string; before_screenshot: string; after_screenshot: string; screenshot_sha256: string; clip_path: string; clip_sha256: string; evidence_status: string
    }>>(await request.get(`/api/events?camera_id=${encodeURIComponent(seeded.camera_id)}&limit=100`))
    const event = demoEvents.find((value) => value.item_id === 'demo-phone' && value.timestamp_start >= startedAt && value.evidence_status === 'confirmed')
    expect(event).toBeTruthy()
    expect(event).toMatchObject({ event_type: 'movement', runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true, evidence_status: 'confirmed', from_zone: '桌面', to_zone: '沙发右侧' })
    expect(event!.event_id).toBeTruthy()
    expect(event!.movement_session_id).toBeTruthy()
    expect(event!.source_session_id).toBeTruthy()
    expect(event!.source_frame_end).toBeGreaterThan(event!.source_frame_start)
    expect(event!.screenshot_sha256).toMatch(/^[a-f0-9]{64}$/)
    expect(event!.clip_sha256).toMatch(/^[a-f0-9]{64}$/)
    expect(event!.before_screenshot).toBe(event!.screenshot_path)
    expect(event!.after_screenshot).toBe(event!.screenshot_path)

    const screenshot = await request.get(event!.screenshot_path)
    expect(screenshot.ok()).toBeTruthy()
    expect(screenshot.headers()['content-type']).toContain('image/jpeg')
    const screenshotBytes = await screenshot.body()
    expect(screenshotBytes.byteLength).toBeGreaterThan(1000)
    expect(createHash('sha256').update(screenshotBytes).digest('hex')).toBe(event!.screenshot_sha256)
    const clip = await request.get(event!.clip_path)
    expect(clip.ok()).toBeTruthy()
    expect(clip.headers()['content-type']).toContain('video')
    const clipBytes = await clip.body()
    expect(clipBytes.byteLength).toBeGreaterThan(1000)
    expect(createHash('sha256').update(clipBytes).digest('hex')).toBe(event!.clip_sha256)

    const decodedImage = await page.evaluate(async (source) => {
      const response = await fetch(source); if (!response.ok) throw new Error(`截图 HTTP ${response.status}`)
      const bitmap = await createImageBitmap(await response.blob())
      try { return { width: bitmap.width, height: bitmap.height } } finally { bitmap.close() }
    }, event!.screenshot_path)
    expect(decodedImage.width).toBeGreaterThanOrEqual(decodedImage.height * 2)

    const decoded = await page.evaluate(async (source) => {
      const video = document.createElement('video')
      video.muted = true; video.preload = 'metadata'; document.body.appendChild(video)
      try {
        await new Promise<void>((resolve, reject) => {
          const timeout = window.setTimeout(() => reject(new Error('短视频无法解码')), 10_000)
          video.onloadedmetadata = () => { window.clearTimeout(timeout); resolve() }
          video.onerror = () => { window.clearTimeout(timeout); reject(new Error(video.error?.message || '解码失败')) }
          video.src = source
        })
        const target = Math.min(Math.max(video.duration / 2, 0.05), Math.max(video.duration - 0.01, 0.05))
        await new Promise<void>((resolve, reject) => {
          const timeout = window.setTimeout(() => reject(new Error('短视频无法 seek 到真实帧')), 10_000)
          video.onseeked = () => { window.clearTimeout(timeout); resolve() }
          video.onerror = () => { window.clearTimeout(timeout); reject(new Error(video.error?.message || 'seek 解码失败')) }
          video.currentTime = target
        })
        const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight
        const context = canvas.getContext('2d'); if (!context) throw new Error('无法创建视频帧画布')
        context.drawImage(video, 0, 0)
        const pixels = context.getImageData(0, 0, Math.min(16, canvas.width), Math.min(16, canvas.height)).data
        return { width: video.videoWidth, height: video.videoHeight, duration: video.duration, sampledBytes: pixels.length }
      } finally { video.removeAttribute('src'); video.load(); video.remove() }
    }, event!.clip_path)
    expect(decoded.width).toBeGreaterThan(0)
    expect(decoded.height).toBeGreaterThan(0)
    expect(decoded.duration).toBeGreaterThan(0)
    expect(decoded.sampledBytes).toBeGreaterThan(0)

    const location = await body<{ current_track: { current_zone: string; status: string; evidence_event_id: string; runtime_mode: string; source_type: string; is_simulated: boolean }; last_confirmed: { event_id: string; to_zone: string } }>(await request.get('/api/items/demo-phone/last-location'))
    expect(location.current_track).toMatchObject({ evidence_event_id: event!.event_id, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: true })
    expect(['桌面', '沙发右侧']).toContain(location.current_track.current_zone)
    if (location.current_track.current_zone === '桌面') expect(location.current_track.status).toBe('last_seen')
    expect(location.last_confirmed).toMatchObject({ event_id: event!.event_id, to_zone: '沙发右侧' })
    const searchResult = await body<{ results: Array<{ item: { id: string }; answer: string; current_state?: { current_zone: string; status: string }; last_confirmed?: { event_id: string; to_zone: string } }> }>(await request.post('/api/search', { data: { query: '我的手机在哪里' } }))
    const phoneResult = searchResult.results.find((value) => value.item.id === 'demo-phone')
    expect(phoneResult?.last_confirmed).toMatchObject({ event_id: event!.event_id, to_zone: '沙发右侧' })
    expect(phoneResult?.answer).toContain('沙发右侧')
    if (phoneResult?.current_state?.current_zone === '桌面') {
      expect(phoneResult.current_state.status).toBe('last_seen')
      expect(phoneResult.answer).toContain('桌面')
    }

    const databaseEvidence = await sqliteEvidence(databasePath, event!.event_id)
    expect(databaseEvidence).toMatchObject({ events: 1, media: 2 })
    expect(databaseEvidence.event).toMatchObject({ event_id: event!.event_id, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: 1, source_session_id: event!.source_session_id, from_zone: '桌面', to_zone: '沙发右侧', screenshot_sha256: event!.screenshot_sha256, clip_sha256: event!.clip_sha256 })
    expect(databaseEvidence.state).toMatchObject({ item_id: 'demo-phone', evidence_event_id: event!.event_id, runtime_mode: 'DEMO', source_type: 'video_file', is_simulated: 1 })
    expect(['桌面', '沙发右侧']).toContain(databaseEvidence.state.current_zone)
    if (databaseEvidence.state.current_zone === '桌面') expect(databaseEvidence.state.status).toBe('last_seen')

    await page.goto(`/search?q=${encodeURIComponent('我的手机在哪里')}`)
    await expect(page.getByText('测试视频', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('模拟确认', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('演示模式，当前事件不来自真实家庭摄像头。', { exact: true })).toBeVisible()

    const stableUntil = Date.now() + 12_000
    await expect.poll(async () => {
      const afterLoop = await body<Array<{ item_id: string; source_session_id: string; evidence_status: string }>>(await request.get(`/api/events?camera_id=${encodeURIComponent(seeded.camera_id)}&limit=100`))
      const count = afterLoop.filter((value) => value.evidence_status === 'confirmed').length
      if (count !== 1) return `重复事件数：${count}`
      return Date.now() >= stableUntil ? '稳定' : '继续观察'
    }, { timeout: 15_000, intervals: [1000] }).toBe('稳定')

    await expect.poll(async () => {
      const current = await body<{ health?: { source_session_id?: string; reconnect_epoch?: number } }>(await request.get(`/api/cameras/${encodeURIComponent(seeded.camera_id)}`))
      return current.health?.source_session_id !== event!.source_session_id && (current.health?.reconnect_epoch || 0) >= 1
    }, { timeout: 20_000, intervals: [500, 1000] }).toBe(true)
    expect((await sqliteEvidence(databasePath, event!.event_id)).events).toBe(1)

    await page.goto('/devices')
    await expect(page.getByRole('heading', { name: 'Virtual ESP32-CAM' })).toBeVisible()
    await expect(page.getByText(/虚拟设备.*非真实硬件/).first()).toBeVisible()
    const finalEvents = await body<Array<{ evidence_status: string }>>(await request.get(`/api/events?camera_id=${encodeURIComponent(seeded.camera_id)}&limit=100`))
    const finalDatabaseEvidence = await sqliteEvidence(databasePath, event!.event_id)
    const finalStorage = await body<{ database_size: number; screenshot_size: number; clip_size: number; log_size: number }>(await request.get('/api/storage/status'))
    console.log(`[DEMO_E2E_EVIDENCE] ${JSON.stringify({
      runtime_mode: runtime.runtime_mode,
      database_path: databasePath,
      media_dir: diagnostics.media_dir,
      api_event_count: finalEvents.length,
      confirmed_event_count: finalEvents.filter((value) => value.evidence_status === 'confirmed').length,
      sqlite_event_count: finalDatabaseEvidence.events,
      event_media_count: finalDatabaseEvidence.media,
      screenshot_count: 1,
      clip_count: 1,
      screenshot_bytes: screenshotBytes.byteLength,
      clip_bytes: clipBytes.byteLength,
      storage: finalStorage,
    })}`)
    expect(await optionalHash(canonicalReal)).toBe(canonicalHashBefore)
  } finally {
    await request.post(`/api/cameras/${encodeURIComponent(seeded.camera_id)}/stop`)
  }
})
