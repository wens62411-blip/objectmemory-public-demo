import { defineConfig, devices } from '@playwright/test'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = resolve(fileURLToPath(new URL('.', import.meta.url)), '../..')
const isolatedData = resolve(projectRoot, 'data', 'temporary', `playwright-real-${process.pid}-${Date.now()}`)
const shutdownFile = resolve(isolatedData, '.shutdown-request')
const shutdownAckFile = resolve(isolatedData, '.shutdown-complete')
process.env.OM_E2E_DATA_DIR = isolatedData
process.env.OM_E2E_SHUTDOWN_FILE = shutdownFile
process.env.OM_E2E_SHUTDOWN_ACK_FILE = shutdownAckFile
process.env.OM_E2E_HEALTH_URL = 'http://127.0.0.1:8029/api/health'

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: ['app.real.spec.ts'],
  timeout: 30_000,
  fullyParallel: false,
  reporter: [['list'], ['json', { outputFile: process.env.OM_E2E_REPORT || '../../data/verification/playwright-tests.json' }]],
  use: {
    baseURL: 'http://127.0.0.1:8029',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `.venv\\Scripts\\python.exe scripts\\serve.py --port 8029 --mode REAL --no-browser --shutdown-file "${shutdownFile}" --shutdown-ack-file "${shutdownAckFile}"`,
    cwd: projectRoot,
    env: { OM_DATA_DIR: isolatedData, OM_E2E_DATA_DIR: isolatedData, OM_PORT: '8029', OM_RUNTIME_MODE: 'REAL', OM_RUN_MODE: 'REAL' },
    url: 'http://127.0.0.1:8029/api/health',
    reuseExistingServer: false,
    timeout: 120_000,
  },
  globalTeardown: './tests/e2e/cleanup-data.ts',
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
