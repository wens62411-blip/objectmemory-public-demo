import { access, mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

import cleanupData from './e2e/cleanup-data'

describe('E2E isolated data cleanup gate', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    for (const key of ['OM_E2E_DATA_DIR', 'OM_E2E_SHUTDOWN_FILE', 'OM_E2E_SHUTDOWN_ACK_FILE', 'OM_E2E_HEALTH_URL']) {
      delete process.env[key]
    }
  })

  it('does not treat one transient health failure as process exit', async () => {
    const target = resolve(process.cwd(), '..', '..', 'data', 'temporary', `playwright-cleanup-unit-${process.pid}-${Date.now()}`)
    const shutdownAck = resolve(target, '.shutdown-complete')
    await mkdir(target, { recursive: true })
    await writeFile(shutdownAck, 'stopped\n', 'utf8')
    process.env.OM_E2E_DATA_DIR = target
    process.env.OM_E2E_SHUTDOWN_FILE = resolve(target, '.shutdown-request')
    process.env.OM_E2E_SHUTDOWN_ACK_FILE = shutdownAck
    process.env.OM_E2E_HEALTH_URL = 'http://127.0.0.1:65530/api/health'
    vi.spyOn(AbortSignal, 'timeout').mockReturnValue(new AbortController().signal)
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error('transient timeout'))
      .mockResolvedValueOnce(new Response('{}', { status: 200 }))
      .mockRejectedValueOnce(new Error('stopped'))
      .mockRejectedValueOnce(new Error('stopped'))
      .mockRejectedValueOnce(new Error('stopped'))
    vi.stubGlobal('fetch', fetchMock)

    await cleanupData()

    expect(fetchMock).toHaveBeenCalledTimes(5)
    await expect(access(target)).rejects.toBeTruthy()
  })
})
