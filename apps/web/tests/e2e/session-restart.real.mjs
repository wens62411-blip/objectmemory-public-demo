// Real process restart regression, independent of the production service.
// From apps/web with the existing production build available:
// node tests/e2e/session-restart.real.mjs
import { chromium, expect } from '@playwright/test'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { closeSync, openSync } from 'node:fs'
import { access, mkdir, rm, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve, relative, isAbsolute } from 'node:path'
import { createServer } from 'node:net'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../..')
const port = await new Promise((resolvePort,reject) => {
  const probe = createServer()
  probe.once('error',reject)
  probe.listen({host:'127.0.0.1',port:0,exclusive:true}, () => {
    const address = probe.address()
    probe.close(error => error ? reject(error) : resolvePort(address.port))
  })
})
if (port === 8018) throw new Error('The production port is forbidden for this test')
const base = `http://127.0.0.1:${port}`
const output = resolve(process.env.OM_E2E_OUTPUT_DIR || resolve(root,'data/verification/session-restart'))
const artifactPath = relative(resolve(root,'data/verification'),output)
if (artifactPath.startsWith('..') || isAbsolute(artifactPath)) throw new Error('Test artifacts must stay inside private verification data')
const data = resolve(root, 'data/temporary', `playwright-session-restart-${process.pid}-${Date.now()}`)
const stopFile = resolve(data, '.shutdown-request'), ackFile = resolve(data, '.shutdown-complete')
const python = process.env.OM_E2E_PYTHON || resolve(root, '.venv/Scripts/python.exe')
const report = { mode:'TEST', real_backend:true, core_api_mock:false, physical_camera:false,
  data_directory:data, port, process_ids:[], shutdowns:[], business_writes:[], passed:false }
const delay = ms => new Promise(resolveDelay => setTimeout(resolveDelay, ms))
let child = null, exited = null, browser = null
const hash = value => createHash('sha256').update(value).digest('hex')
async function start(round) {
  const log = openSync(resolve(output, `session-restart-backend-${round}.log`), 'w')
  child = spawn(python, ['scripts/serve.py','--port',String(port),'--mode','TEST','--no-browser',
    '--shutdown-file',stopFile,'--shutdown-ack-file',ackFile], { cwd:root, windowsHide:true,
    env:{...process.env,OM_DATA_DIR:data,OM_RUNTIME_MODE:'TEST',OM_RUN_MODE:'TEST',OM_ALLOW_LAN:'0',OPENCV_OPENCL_RUNTIME:'disabled'}, stdio:['ignore',log,log] })
  report.process_ids.push(child.pid)
  exited = new Promise((resolveExit,reject) => { child.once('error',reject); child.once('exit',(code,signal) => resolveExit({code,signal})) }).finally(() => closeSync(log))
  const deadline = Date.now()+20000
  while (Date.now()<deadline) {
    if (child.exitCode !== null) throw new Error(`Backend stopped during startup: ${child.exitCode}`)
    try { if ((await fetch(`${base}/api/health`,{signal:AbortSignal.timeout(500)})).ok) return } catch {}
    await delay(100)
  }
  throw new Error('Backend startup exceeded 20 seconds')
}
async function stop() {
  if (!child) return
  if (child.exitCode !== null) throw new Error(`Backend exited before the requested shutdown: ${child.exitCode}`)
  const started = Date.now()
  await writeFile(stopFile,'stop\n')
  let timer
  const result = await Promise.race([exited,new Promise((_,reject) => {timer=setTimeout(()=>reject(new Error('Backend graceful shutdown exceeded 20 seconds')),20000)})]).finally(()=>clearTimeout(timer))
  if (result.code !== 0) throw new Error(`Backend exit was not clean: ${JSON.stringify(result)}`)
  await access(ackFile)
  report.shutdowns.push({pid:child.pid,exit_code:result.code,seconds:(Date.now()-started)/1000,lifecycle_ack:true})
  child = null
}

try {
  await mkdir(output,{recursive:true}); await mkdir(data,{recursive:true})
  // Refuse a pre-existing listener; this probe never takes over another service.
  let occupied = false
  try { await fetch(`${base}/api/health`,{signal:AbortSignal.timeout(500)}); occupied = true } catch {}
  if (occupied) throw new Error(`Selected test port ${port} is already occupied`)
  await start(1)
  browser = await chromium.launch()
  const context = await browser.newContext()
  const page = await context.newPage()
  let phase = 'before', navigationCount = 0, recoveredSessions = 0
  report.session_responses=[]; report.frame_navigations=[]
  page.on('domcontentloaded', () => navigationCount++)
  page.on('framenavigated', frame => {if (frame === page.mainFrame()) report.frame_navigations.push({phase,url:frame.url()})})
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/') && !['GET','HEAD'].includes(request.method())) report.business_writes.push({path:new URL(request.url()).pathname,method:request.method()})
  })
  page.on('response', response => {if (response.url().endsWith('/api/session')) {
    report.session_responses.push({phase,status:response.status(),method:response.request().method()})
    if (phase === 'after' && response.ok()) recoveredSessions++
  }})
  await page.goto(`${base}/live`)
  await expect(page.locator('.topbar-status')).toHaveAttribute('data-runtime-mode','TEST')
  await page.evaluate(() => {window.__restartSentinel='same-document'})
  const firstCookie = (await context.cookies()).find(cookie => cookie.name === 'om_session')
  if (!firstCookie) throw new Error('Initial authenticated cookie missing')
  report.cookie_before_sha256=hash(firstCookie.value)
  await stop()
  await expect(page.locator('.topbar-status')).toHaveAttribute('data-runtime-mode','UNKNOWN',{timeout:15000})
  report.offline_unknown_observed=true
  phase='after'
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/runtime-config') && response.ok(),{timeout:20000})
  await start(2)
  const oldCookieResponse = await fetch(`${base}/api/runtime-config`,{headers:{Cookie:`om_session=${firstCookie.value}`}})
  report.old_cookie_http=oldCookieResponse.status
  expect(oldCookieResponse.status).toBe(401)
  await refreshed
  await expect(page.locator('.topbar-status')).toHaveAttribute('data-runtime-mode','TEST',{timeout:5000})
  report.navigation_count=navigationCount; report.recovered_session_requests=recoveredSessions
  expect(await page.evaluate(() => window.__restartSentinel)).toBe('same-document')
  expect(navigationCount).toBe(1)
  expect(recoveredSessions).toBe(1)
  expect(report.business_writes).toEqual([])
  const secondCookie = (await context.cookies()).find(cookie => cookie.name === 'om_session')
  expect(secondCookie?.value).toBeTruthy()
  report.cookie_after_sha256=hash(secondCookie.value)
  expect(report.cookie_before_sha256).not.toBe(report.cookie_after_sha256)
  const events = await context.request.get(`${base}/api/events`)
  expect(events.ok()).toBe(true); expect(await events.json()).toEqual([])
  report.events=0
  await page.screenshot({path:resolve(output,'session-restart-same-page.png'),fullPage:true})
  await context.close(); await browser.close(); browser=null
  await stop()
  const inside=relative(resolve(root,'data/temporary'),data)
  if (!inside || inside.startsWith('..') || isAbsolute(inside)) throw new Error('Unsafe temporary cleanup path')
  await rm(data,{recursive:true,force:true,maxRetries:3,retryDelay:100})
  report.isolated_data_removed=true; report.passed=true
} catch (error) { report.error=String(error); process.exitCode=1 }
finally {
  if(browser) await browser.close()
  if(child && child.exitCode === null) {
    try { await stop() } catch(error) {
      report.cleanup_error=String(error); report.forced_cleanup=true; report.passed=false; process.exitCode=1
      child.kill(); await exited
    }
  }
  await writeFile(resolve(output,'session-restart-real.json'),JSON.stringify(report,null,2))
  console.log(JSON.stringify(report))
}
