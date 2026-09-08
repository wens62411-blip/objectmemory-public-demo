import { lazy, useEffect } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PageBoundary } from '../src/components/PageBoundary'

describe('页面文件故障恢复（受控静态资源故障，不伪造业务数据）', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('懒加载失败保留导航，不显示原始错误；换页面后可恢复', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const Broken = lazy(() => Promise.reject(new Error('private-internal-asset-path')))
    render(<MemoryRouter initialEntries={['/broken']}>
      <nav><Link to="/safe">可用页面</Link></nav>
      <PageBoundary><Routes><Route path="/broken" element={<Broken />} /><Route path="/safe" element={<h1>正常页面</h1>} /></Routes></PageBoundary>
    </MemoryRouter>)
    expect(await screen.findByRole('alert')).toHaveTextContent('页面暂时无法加载')
    expect(screen.getByRole('button', { name: '重新加载页面' })).toBeEnabled()
    expect(screen.getByRole('link', { name: '返回首页' })).toHaveAttribute('href', '/')
    expect(screen.queryByText(/private-internal/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: '可用页面' }))
    expect(await screen.findByRole('heading', { name: '正常页面' })).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('正常换页不卸载共享导航和健康轮询', () => {
    const mounted = vi.fn(), unmounted = vi.fn()
    function PersistentShell() {
      useEffect(() => { mounted(); return unmounted }, [])
      return <><Link to="/next">下一页</Link><Routes><Route path="/" element={<p>首页</p>} /><Route path="/next" element={<p>下一页内容</p>} /></Routes></>
    }
    render(<MemoryRouter><PageBoundary><PersistentShell /></PageBoundary></MemoryRouter>)
    fireEvent.click(screen.getByRole('link', { name: '下一页' }))
    expect(screen.getByText('下一页内容')).toBeVisible()
    expect(mounted).toHaveBeenCalledTimes(1)
    expect(unmounted).not.toHaveBeenCalled()
  })
})
