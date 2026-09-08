import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DeviceNetworkCheck } from '../src/components/DeviceNetworkCheck'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function setup(band: string, observed: boolean) {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ status: 'connected', connections: [
    { ssid: 'My hotspot', band, channel: 149, observed_2_4ghz: observed },
  ] }), { headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

it('warns on 5 GHz and a misspelled SSID without claiming a board connected', async () => {
  const fetcher = setup('5 GHz', false)
  render(<DeviceNetworkCheck mode="REAL" ssid="My hotspott" />)
  await screen.findByText('My hotspot')
  expect(screen.getByRole('alert')).toHaveTextContent('拼写')
  expect(screen.getByText(/没有观测到当前热点的 2.4 GHz/)).toBeInTheDocument()
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('does not reject dual band merely because the PC uses 5 GHz', async () => {
  setup('5 GHz', true)
  render(<DeviceNetworkCheck mode="REAL" ssid="My hotspot" />)
  expect(await screen.findByText(/已观测到 2.4 GHz/)).toHaveTextContent('还需后续真实配网验证')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.queryByText(/没有观测到当前热点的/)).not.toBeInTheDocument()
})

it.each(['TEST', 'DEMO'])('does not inspect the host in %s', (mode) => {
  const fetcher = setup('5 GHz', false)
  render(<DeviceNetworkCheck mode={mode} ssid="" />)
  expect(fetcher).not.toHaveBeenCalled()
  expect(screen.queryByRole('region')).not.toBeInTheDocument()
})
