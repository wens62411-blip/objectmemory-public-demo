import { defineConfig, devices } from '@playwright/test'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = resolve(fileURLToPath(new URL('.', import.meta.url)), '../..')
const python = process.env.OM_E2E_PYTHON || resolve(projectRoot, '.venv', 'Scripts', 'python.exe')
const isolatedData = resolve(projectRoot, 'data', 'temporary', `playwright-demo-${process.pid}-${Date.now()}`)
const shutdownFile = resolve(isolatedData, '.shutdown-request')
const shutdownAckFile = resolve(isolatedData, '.shutdown-complete')
process.env.OM_E2E_DATA_DIR = isolatedData
process.env.OM_E2E_SHUTDOWN_FILE = shutdownFile
process.env.OM_E2E_SHUTDOWN_ACK_FILE = shutdownAckFile
process.env.OM_E2E_HEALTH_URL = 'http://127.0.0.1:8030/api/health'

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: ['app.demo.spec.ts'],
  timeout: 120_000,
  fullyParallel: false,
  reporter: [['list'], ['json', { outputFile: process.env.OM_E2E_REPORT || '../../data/verification/playwright-demo-tests.json' }]],
  use: {
    baseURL: 'http://127.0.0.1:8030',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `"${python}" scripts\\serve.py --port 8030 --mode DEMO --no-browser --shutdown-file "${shutdownFile}" --shutdown-ack-file "${shutdownAckFile}"`,
    cwd: projectRoot,
    env: { OM_DATA_DIR: isolatedData, OM_E2E_DATA_DIR: isolatedData, OM_PORT: '8030', OM_RUNTIME_MODE: 'DEMO', OM_RUN_MODE: 'DEMO' },
    url: 'http://127.0.0.1:8030/api/health',
    reuseExistingServer: false,
    timeout: 120_000,
  },
  globalTeardown: './tests/e2e/cleanup-data.ts',
  projects: [{ name: 'chromium-demo', use: { ...devices['Desktop Chrome'] } }],
})
