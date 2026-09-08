import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { FirmwareCompatibility } from '../src/components/FirmwareCompatibility'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('Mock contract: labels build capability independently from physical acceptance', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ boards: [{
    board_model: 'xiao_esp32s3_sense', label: 'Seeed XIAO ESP32S3 Sense', chip: 'ESP32-S3',
    connection: 'USB-C，需 Sense 摄像头扩展', source_present: true, build_verified: true,
  }] }), { status: 200, headers: {'Content-Type': 'application/json'} }))
  vi.stubGlobal('fetch', fetcher)
  render(<FirmwareCompatibility />)
  expect(await screen.findByText('Seeed XIAO ESP32S3 Sense')).toBeInTheDocument()
  expect(screen.getByText(/当前编译产物哈希已验证/)).toBeInTheDocument()
  expect(screen.getByText(/此表不代表已接入或刷写了这些板/)).toBeInTheDocument()
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(screen.queryByText('刷写成功')).not.toBeInTheDocument()
})
