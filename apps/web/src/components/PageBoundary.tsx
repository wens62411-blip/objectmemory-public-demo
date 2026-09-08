import { Component, Suspense, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { Loading } from './UI'

class PageErrorBoundary extends Component<{ children: ReactNode; path: string }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() { return { failed: true } }

  componentDidUpdate(previous: Readonly<{ children: ReactNode; path: string }>) {
    if (this.state.failed && previous.path !== this.props.path) this.setState({ failed: false })
  }

  render() {
    if (!this.state.failed) return this.props.children
    // A rejected lazy import stays cached in React. Reload the current document
    // explicitly, not a fake retry or a business mutation, to fetch its new assets.
    return <section className="empty-state" role="alert">
      <h2>页面暂时无法加载</h2>
      <p>连接中断或应用更新可能导致页面文件不可用。请检查连接后重新加载；系统不会因此创建物品事件或重复执行刷写。</p>
      <button type="button" className="button primary" onClick={() => window.location.reload()}>重新加载页面</button>
      <a className="button secondary" href="/">返回首页</a>
    </section>
  }
}

/** Keep navigation and the runtime warning mounted when a page chunk fails. */
export function PageBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  return <PageErrorBoundary path={pathname}><Suspense fallback={<Loading label="正在加载页面…" />}>{children}</Suspense></PageErrorBoundary>
}
