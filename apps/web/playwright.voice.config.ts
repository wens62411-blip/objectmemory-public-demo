import { defineConfig } from '@playwright/test'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import base from './playwright.config'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../..')
const isolatedData = resolve(root, 'data/temporary', `playwright-voice-${process.pid}-${Date.now()}`)
const shutdownFile = resolve(isolatedData, '.shutdown-request')
const shutdownAckFile = resolve(isolatedData, '.shutdown-complete')
Object.assign(process.env, { OM_E2E_DATA_DIR: isolatedData, OM_E2E_SHUTDOWN_FILE: shutdownFile, OM_E2E_SHUTDOWN_ACK_FILE: shutdownAckFile, OM_E2E_HEALTH_URL: 'http://127.0.0.1:8037/api/health' })

export default defineConfig({
  ...base,
  testMatch: ['voice.real.spec.ts'],
  reporter: [['list'], ['json', { outputFile: process.env.OM_E2E_REPORT || '../../data/verification/voice-real-api-tests.json' }]],
  use: { ...base.use, baseURL: 'http://127.0.0.1:8037' },
  webServer: {
    command: `.venv\\Scripts\\python.exe scripts\\serve.py --port 8037 --mode TEST --no-browser --shutdown-file "${shutdownFile}" --shutdown-ack-file "${shutdownAckFile}"`,
    cwd: root,
    env: { OM_DATA_DIR: isolatedData, OM_E2E_DATA_DIR: isolatedData, OM_PORT: '8037', OM_RUNTIME_MODE: 'TEST', OM_RUN_MODE: 'TEST' },
    url: 'http://127.0.0.1:8037/api/health', reuseExistingServer: false, timeout: 120_000,
  },
})
