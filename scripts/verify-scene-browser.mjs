/** Real TEST API + real WebGL renderer; no route interception or fixture APIs. */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const require = createRequire(resolve(root, 'apps/web/package.json'))
const { chromium, expect } = require('@playwright/test')
const [url, camera, output] = process.argv.slice(2)
assert.equal(new URL(url).hostname, '127.0.0.1')
assert.match(camera, /^[a-zA-Z0-9_-]+$/)
const directory = resolve(output)
assert.ok(directory.startsWith(resolve(root, 'data/verification') + '\\'))
await mkdir(directory, { recursive: true })
const browser = await chromium.launch({ headless: true })
const report = { passed: false, api_mock: false, actual_webgl: false, physical_camera: false,
  evidence_scope: 'TEST replay of a previously captured local frame; not measured room calibration', errors: [], writes: [] }
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } })
  const session = await context.request.get(url + '/api/session')
  assert.equal((await session.json()).runtime_mode, 'TEST')
  const endpoint = url + `/api/cameras/${camera}/scene`
  const before = await (await context.request.get(endpoint)).json()
  assert.equal(before.scene.world_geometry.status, 'draft')
  assert.ok(before.scene.world_geometry.objects.length > 0)
  assert.equal(before.scene.world_geometry.eligible_for_mapping, false)
  const page = await context.newPage()
  page.on('pageerror', error => report.errors.push(error.message))
  page.on('request', request => {
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) report.writes.push({ method: request.method(), path: new URL(request.url()).pathname })
  })
  await page.goto(url + `/scene?camera=${camera}`)
  const canvas = page.getByLabel('交互式三维场景：拖动旋转，滚轮缩放，右键或双指平移，方向键平移', { exact: true })
  await expect(canvas).toBeVisible({ timeout: 15000 })
  report.actual_webgl = await canvas.evaluate(node => Boolean(node.getContext('webgl2')))
  assert.equal(report.actual_webgl, true)
  await expect(page.getByRole('button', { name: '俯视', exact: true })).toBeEnabled()
  const perspective = await canvas.screenshot({ path: resolve(directory, 'draft-perspective.png') })
  await page.getByRole('button', { name: '俯视', exact: true }).click()
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const top = await canvas.screenshot({ path: resolve(directory, 'draft-top.png') })
  const digest = value => createHash('sha256').update(value).digest('hex')
  report.views = { perspective: digest(perspective), top: digest(top) }
  assert.notEqual(report.views.perspective, report.views.top, 'Viewer camera must change actual canvas pixels')
  for (const name of ['侧视', '放大', '缩小', '左转', '右转', '复位']) await page.getByRole('button', { name, exact: true }).click()
  const after = await (await context.request.get(endpoint)).json()
  assert.deepEqual(after.scene, before.scene, 'Viewer controls must not mutate physical calibration')
  assert.equal(report.writes.length, 0)
  assert.equal(report.errors.length, 0)
  report.geometry = before.scene.world_geometry
  report.passed = true
} catch (error) {
  report.errors.push(error.message)
  process.exitCode = 1
} finally {
  await browser.close()
  await writeFile(resolve(directory, 'result.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: report.passed, actual_webgl: report.actual_webgl, errors: report.errors }))
}
