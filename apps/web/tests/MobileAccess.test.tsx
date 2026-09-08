import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import { MobileAccess, safeMobileUrl } from '../src/components/MobileAccess'

describe('手机入口：真实二维码生成与安全链接契约（非手机实机可达证明）', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it.each(['http://localhost:8018', 'http://127.0.0.1:8018', 'http://[::1]:8018', 'javascript:alert(1)', 'https://example.com', 'http://10.evil.example', 'http://user:secret@192.168.1.2:8018'])('拒绝不可供家庭手机扫码的地址 %s', (url) => {
    expect(safeMobileUrl(url)).toBe('')
    render(<MemoryRouter><MobileAccess url={url} /></MemoryRouter>)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '复制链接' })).not.toBeInTheDocument()
    expect(screen.getByText('还没有可供手机访问的地址')).toBeInTheDocument()
  })

  it('QR、显示及复制使用同一清洗地址，永远不带访问码/任意query', async () => {
    const copy = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText: copy } })
    const url = 'http://192.168.1.102:8018/companion/validation-marker?item=phone&camera=cam&run=run1&pin=12345678&token=secret#secret'
    render(<MemoryRouter><MobileAccess url={url} marker /></MemoryRouter>)
    const safe = 'http://192.168.1.102:8018/companion/validation-marker?item=phone&camera=cam&run=run1'
    expect(screen.getByRole('textbox', { name: '手机访问链接' })).toHaveValue(safe)
    expect(screen.queryByText(/12345678|secret/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复制链接' }))
    await screen.findByRole('button', { name: '已复制链接' })
    expect(copy).toHaveBeenCalledWith(safe)
    expect(screen.getByText('连接同一 Wi-Fi')).toBeInTheDocument()
    expect(screen.getByText('扫码，在浏览器打开')).toBeInTheDocument()
    expect(screen.getByText('输入电脑上的访问码')).toBeInTheDocument()
  })

  it('真实 SVG 像素经 OpenCV 二维码解码还原完整手机链接，无 QR 库 Mock', () => {
    const url = 'http://192.168.1.102:8018/companion/validation-marker?item=phone-1&camera=cam-1&run=run-1'
    render(<MemoryRouter><MobileAccess url={url} marker /></MemoryRouter>)
    const svg = screen.getByRole('img', { name: '用手机打开 Marker 页的二维码' })
    const size = Number(svg.getAttribute('viewBox')?.split(' ')[2])
    const path = svg.querySelector('path')?.getAttribute('d')
    const probe = spawnSync(resolve('../..', '.venv/Scripts/python.exe'), ['-B', '-c',
      'import sys,json,re,cv2,numpy as np; p=json.load(sys.stdin); a=np.full((p["size"],p["size"]),255,np.uint8); points=re.findall(r"M(\\d+) (\\d+)h1v1h-1z",p["path"]); [(a.__setitem__((int(y),int(x)),0)) for x,y in points]; a=cv2.resize(a,None,fx=8,fy=8,interpolation=cv2.INTER_NEAREST); print(cv2.QRCodeDetector().detectAndDecode(a)[0])'],
    { input: JSON.stringify({ size, path }), encoding: 'utf8', timeout: 15_000 })
    expect(probe.status, probe.stderr).toBe(0)
    expect(probe.stdout.trim()).toBe(url)
  })

  it('普通 HTTP 无 Clipboard API 时使用选择复制，失败不虚报复制成功', async () => {
    vi.stubGlobal('navigator', {})
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(false) })
    render(<MemoryRouter><MobileAccess url="http://192.168.1.2:8018" /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: '复制链接' }))
    await screen.findByText('链接已选中，请长按复制，或按 Ctrl+C。')
    expect(screen.queryByRole('button', { name: '已复制链接' })).not.toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByRole('textbox'))
  })

  it('地址失效后不继续显示上一次二维码', async () => {
    const { rerender } = render(<MemoryRouter><MobileAccess url="http://192.168.1.2:8018" /></MemoryRouter>)
    expect(screen.getByRole('img')).toBeInTheDocument()
    rerender(<MemoryRouter><MobileAccess url="" /></MemoryRouter>)
    await waitFor(() => expect(screen.queryByRole('img')).not.toBeInTheDocument())
  })
})
