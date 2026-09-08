import { chromium } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const baseURL = process.env.OM_DEMO_URL || 'http://127.0.0.1:8031'
const root = resolve(process.cwd(), '..', '..')
const screenshotDir = resolve(root, 'data', 'verification', 'screenshots')
const reportPath = resolve(root, 'data', 'verification', 'no-hardware-ui.json')
await mkdir(screenshotDir, { recursive: true })

const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
const report = { base_url: baseURL, passed: false, statements: {}, backend: {}, screenshots: {}, error: null }
try {
  const session = await context.request.get('/api/session')
  if (!session.ok() || !(await session.json()).authenticated) throw new Error('本机管理会话建立失败')
  const virtual = await context.request.get('/api/virtual-device/status')
  const firmware = await context.request.get('/api/firmware/status')
  const runtime = await context.request.get('/api/runtime-config')
  if (!virtual.ok() || !firmware.ok() || !runtime.ok()) throw new Error('状态接口不可用')
  report.backend.virtual = await virtual.json()
  report.backend.firmware = await firmware.json()
  report.backend.runtime = await runtime.json()
  if (report.backend.runtime.runtime_mode !== 'DEMO' || report.backend.runtime.is_simulated !== true) throw new Error('无硬件验收必须运行在隔离的 DEMO 模式')

  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await page.getByRole('region', { name: '无硬件演示状态' }).waitFor()
  const dashboardText = await page.getByRole('region', { name: '无硬件演示状态' }).innerText()
  const required = {
    mode: '演示模式，当前事件不来自真实家庭摄像头',
    video: '视频来源',
    esp32: 'ESP32 状态',
    virtual: '虚拟设备',
    firmware: '已编译，未真机刷写',
  }
  for (const [key, value] of Object.entries(required)) {
    report.statements[key] = dashboardText.includes(value)
    if (!report.statements[key]) throw new Error(`首页缺少状态文案：${value}`)
  }
  const dashboardPath = resolve(screenshotDir, 'no-hardware-dashboard.png')
  await page.screenshot({ path: dashboardPath, fullPage: true })
  report.screenshots.dashboard = dashboardPath

  await page.goto('/devices', { waitUntil: 'domcontentloaded' })
  await page.getByText('虚拟设备 · 非真实硬件', { exact: true }).first().waitFor({ timeout: 15_000 })
  const devicePath = resolve(screenshotDir, 'no-hardware-device.png')
  await page.screenshot({ path: devicePath, fullPage: true })
  report.screenshots.device = devicePath
  report.backend_truthful = report.backend.runtime.runtime_mode === 'DEMO'
    && report.backend.runtime.is_simulated === true
    && report.backend.virtual.backend_online === true
    && report.backend.firmware.compile_passed === true
    && report.backend.firmware.physical_board_detected === false
    && report.backend.firmware.physical_flash_performed === false
    && report.backend.firmware.serial_provision_performed === false
    && report.backend.firmware.real_video_verified === false
  if (!report.backend_truthful) throw new Error('状态接口没有同时证明虚拟在线和四项真机状态为 false')
  report.passed = true
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error)
} finally {
  await writeFile(reportPath, JSON.stringify(report, null, 2), 'utf8')
  await context.close()
  await browser.close()
  process.stdout.write(JSON.stringify(report, null, 2) + '\n')
  if (!report.passed) process.exitCode = 2
}
