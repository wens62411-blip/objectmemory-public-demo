import { access, mkdir, rm, writeFile } from 'node:fs/promises'
import { basename, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

export default async function cleanupData() {
  const raw = process.env.OM_E2E_DATA_DIR
  if (!raw) return
  let projectRoot: string
  try {
    projectRoot = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../..')
  } catch {
    // Vitest serves modules through Vite; the real Playwright teardown always
    // uses the file URL branch above.
    projectRoot = resolve(process.cwd(), '../..')
  }
  const temporaryRoot = resolve(projectRoot, 'data', 'temporary')
  const target = resolve(raw)
  if (!target.startsWith(`${temporaryRoot}${sep}`) || !basename(target).startsWith('playwright-')) {
    throw new Error(`拒绝清理未验证的 E2E 路径：${target}`)
  }
  const shutdownFile = resolve(process.env.OM_E2E_SHUTDOWN_FILE || resolve(target, '.shutdown-request'))
  if (!shutdownFile.startsWith(`${target}${sep}`)) {
    throw new Error(`拒绝写入未验证的 E2E 停机路径：${shutdownFile}`)
  }
  const shutdownAckFile = resolve(process.env.OM_E2E_SHUTDOWN_ACK_FILE || '')
  if (!shutdownAckFile || !shutdownAckFile.startsWith(`${target}${sep}`)) {
    throw new Error(`缺少受限的 E2E 停机完成凭据路径：${shutdownAckFile}`)
  }
  // Playwright runs globalTeardown before it terminates webServer. Ask the
  // local launcher to finish FastAPI's lifespan first so camera/firmware
  // workers release the mode lease before this isolated tree is removed.
  await mkdir(target, { recursive: true })
  await writeFile(shutdownFile, 'stop\n', 'utf8')
  const deadline = Date.now() + 20_000
  const healthUrl = process.env.OM_E2E_HEALTH_URL
  if (!healthUrl || !/^http:\/\/127\.0\.0\.1:\d+\/api\/health$/.test(healthUrl)) {
    throw new Error(`缺少受限的 E2E 健康检查地址：${String(healthUrl)}`)
  }
  let stopped = false
  let consecutiveHealthFailures = 0
  while (Date.now() < deadline) {
    try {
      await fetch(healthUrl, { signal: AbortSignal.timeout(500) })
      consecutiveHealthFailures = 0
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 100))
    } catch {
      consecutiveHealthFailures += 1
      let lifecycleComplete = false
      try {
        await access(shutdownAckFile)
        lifecycleComplete = true
      } catch {
        // The launcher writes this only after FastAPI lifespan and mode-lease release.
      }
      if (lifecycleComplete && consecutiveHealthFailures >= 3) {
        stopped = true
        break
      }
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 100))
    }
  }
  if (!stopped) {
    throw new Error(`隔离后端在 20 秒内未完成生命周期停机和模式锁释放：${healthUrl}`)
  }
  const deleteDeadline = Date.now() + 5_000
  let lastError: unknown
  while (Date.now() < deleteDeadline) {
    try {
      await rm(target, { recursive: true, force: true, maxRetries: 1, retryDelay: 100 })
      console.log(`[E2E_CLEANUP] 后端已正常停止并删除隔离测试目录 ${target}`)
      return
    } catch (error) {
      lastError = error
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 100))
    }
  }
  throw new Error(`隔离后端已停止，但在 5 秒内未释放测试目录 ${target}：${String(lastError)}`)
}
