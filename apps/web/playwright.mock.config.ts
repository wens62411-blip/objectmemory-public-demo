import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: ['app.mock.spec.ts'],
  timeout: 30_000,
  fullyParallel: false,
  reporter: [['list'], ['json', { outputFile: process.env.OM_E2E_REPORT || '../../data/verification/playwright-mock-contract-tests.json' }]],
  use: { baseURL: 'http://127.0.0.1:8031', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: 'npm run preview -- --host 127.0.0.1 --port 8031',
    cwd: '.',
    url: 'http://127.0.0.1:8031',
    reuseExistingServer: false,
    timeout: 60_000,
  },
  projects: [{ name: 'chromium-mock-contract', use: { ...devices['Desktop Chrome'] } }],
})
