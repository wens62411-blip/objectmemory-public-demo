import { writeFile } from 'node:fs/promises'
import { chromium } from '@playwright/test'

const [baseUrl, cameraId, durationRaw, output] = process.argv.slice(2)
const durationMs = Number(durationRaw)
if (!baseUrl || !cameraId || !Number.isFinite(durationMs) || durationMs <= 0 || !output) {
  throw new Error('usage: runtime-stability-browser.mjs <baseUrl> <cameraId> <durationMs> <output>')
}

const result = {
  started_at: new Date().toISOString(),
  ended_at: null,
  url: `${baseUrl}/live?camera=${encodeURIComponent(cameraId)}`,
  samples: [],
  console_errors: [],
  page_errors: [],
  failed_requests: [],
  expected_stream_aborts: [],
  mode_banner_visible: false,
  mode_text: '',
  runtime_config: null,
  live_page_visible: false,
  elapsed_ms: 0,
}

const startedMonotonic = Date.now()
let closing = false
const browser = await chromium.launch({ headless: true })
try {
  const context = await browser.newContext()
  const page = await context.newPage()
  page.on('console', (message) => {
    if (message.type() === 'error') result.console_errors.push(message.text())
  })
  page.on('pageerror', (error) => result.page_errors.push(String(error)))
  page.on('requestfailed', (request) => {
    if (closing) return
    const failure = request.failure()
    const failed = { url: request.url(), error: failure?.errorText || 'unknown' }
    if (failed.error === 'net::ERR_ABORTED' && /\/api\/cameras\/[^/]+\/stream(?:\?|$)/.test(failed.url)) {
      result.expected_stream_aborts.push(failed)
      return
    }
    result.failed_requests.push(failed)
  })
  await page.goto(result.url, { waitUntil: 'domcontentloaded', timeout: 30_000 })
  result.live_page_visible = await page.getByRole('heading', { name: '看见，也记得发生过什么' }).isVisible().catch(() => false)
  await page.locator('img[alt*="实时画面"]').waitFor({ state: 'visible', timeout: 30_000 })
  await page.waitForFunction(() => {
    const image = document.querySelector('img[alt*="实时画面"]')
    return image instanceof HTMLImageElement && image.naturalWidth > 0 && image.naturalHeight > 0
  }, undefined, { timeout: 30_000 })
  await page.waitForFunction(() => document.querySelector('.topbar-status')?.textContent?.includes('REAL · 真实模式'), undefined, { timeout: 30_000 })
  const modeStatus = page.locator('.topbar-status').first()
  result.mode_text = await modeStatus.innerText()
  result.runtime_config = await page.evaluate(async () => {
    const response = await fetch('/api/runtime-config', { credentials: 'same-origin' })
    if (!response.ok) throw new Error(`runtime config returned ${response.status}`)
    return response.json()
  })
  result.mode_banner_visible = await modeStatus.isVisible()
    && result.mode_text.includes('REAL · 真实模式')
    && result.runtime_config?.runtime_mode === 'REAL'
  const cdp = await context.newCDPSession(page)
  await cdp.send('Performance.enable')
  const deadline = Date.now() + durationMs
  const sampleDelayMs = Math.min(30_000, Math.max(1_000, Math.floor(durationMs / 4)))
  while (Date.now() < deadline) {
    const metrics = await cdp.send('Performance.getMetrics')
    const values = Object.fromEntries(metrics.metrics.map(({ name, value }) => [name, value]))
    result.samples.push({
      captured_at: new Date().toISOString(),
      js_heap_used_bytes: values.JSHeapUsedSize ?? null,
      js_heap_total_bytes: values.JSHeapTotalSize ?? null,
      nodes: values.Nodes ?? null,
      documents: values.Documents ?? null,
      frames: values.Frames ?? null,
      task_duration_seconds: values.TaskDuration ?? null,
    })
    await page.waitForTimeout(Math.min(sampleDelayMs, Math.max(1, deadline - Date.now())))
  }
} catch (error) {
  result.error = String(error)
  process.exitCode = 1
} finally {
  result.ended_at = new Date().toISOString()
  result.elapsed_ms = Date.now() - startedMonotonic
  closing = true
  await browser.close()
  await writeFile(output, `${JSON.stringify(result, null, 2)}\n`, 'utf8')
}
