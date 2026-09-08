import { Activity, Camera, ChevronDown, Clock3, Cpu, Database, House, Map, Menu, PackageSearch, Radio, ScanLine, Search, Settings, Smartphone, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { api } from '../lib/api'
import type { HealthResponse } from '../types'
import { Badge } from './UI'
import { useRuntime } from '../contexts/RuntimeContext'
import { RuntimeModeBanner } from './RuntimeModeBanner'
import { Brand } from './Brand'

const navigation = [
  { to: '/', label: '总览', icon: House, end: true },
  { to: '/search', label: '寻找物品', icon: Search },
  { to: '/live', label: '实时监控', icon: Radio },
  { to: '/events', label: '事件时间线', icon: Clock3 },
  { to: '/items', label: '物品管理', icon: PackageSearch },
  { to: '/scene', label: '家庭场景', icon: Map },
  { to: '/cameras', label: '摄像头', icon: Camera },
  { to: '/settings', label: '设置', icon: Settings },
]
const advancedNavigation = [
  { to: '/camera-diagnostics', label: '摄像头诊断', icon: Activity },
  { to: '/devices', label: '设备中心', icon: Cpu },
  { to: '/storage', label: '存储管理', icon: Database },
  { to: '/acceptance/real-movement', label: '真实移动验收', icon: ScanLine },
]

export function AppShell() {
  const [open, setOpen] = useState(false)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const runtime = useRuntime()
  const location = useLocation()
  useEffect(() => { setOpen(false) }, [location.pathname, location.hash])
  useEffect(() => {
    if (!open) return
    const onEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', onEscape)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', onEscape); document.body.style.overflow = previous }
  }, [open])
  useEffect(() => {
    const load = () => api<HealthResponse>('/api/health').then(setHealth).catch(() => setHealth(null))
    void load(); const timer = window.setInterval(load, 10_000); return () => window.clearInterval(timer)
  }, [])
  return (
    <div className={`app-shell ${runtime.mode !== 'REAL' ? 'runtime-flagged' : ''}`}>
      <a className="skip-link" href="#main-content">跳转到主要内容</a>
      <aside id="app-sidebar" className={`sidebar ${open ? 'sidebar-open' : ''}`}>
        <div className="sidebar-brand"><Link to="/" aria-label="物忆首页"><Brand /></Link><button className="icon-button sidebar-close" onClick={() => setOpen(false)} aria-label="关闭菜单"><X /></button></div>
        <nav aria-label="主导航">
          {navigation.map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => isActive ? 'active' : ''}><Icon /><span>{label}</span></NavLink>)}
        </nav>
        <details className="advanced-nav" open={advancedNavigation.some(({ to }) => location.pathname.startsWith(to)) || undefined}>
          <summary>高级工具<ChevronDown /></summary>
          <nav aria-label="高级工具">{advancedNavigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} className={({ isActive }) => isActive ? 'active' : ''}><Icon /><span>{label}</span></NavLink>)}</nav>
        </details>
        <div className="sidebar-bottom">
          <div className="privacy-chip"><span className={`status-dot ${health ? 'online' : ''}`} /><div><strong>{health ? '本地服务正常' : '正在重连'}</strong><span>视频不上传云端</span></div></div>
          <Link className="companion-link" to="/settings#mobile-access" onClick={() => setOpen(false)}><Smartphone /><span><strong>连接手机</strong><small>扫码打开，无需下载</small></span></Link>
        </div>
      </aside>
      {open && <button className="nav-scrim" onClick={() => setOpen(false)} aria-label="关闭菜单" />}
      <div className="main-column">
        <RuntimeModeBanner mode={runtime.mode} />
        <header className="topbar">
          <button className="icon-button menu-button" onClick={() => setOpen(true)} aria-label="打开菜单" aria-expanded={open} aria-controls="app-sidebar"><Menu /></button>
          <Link className="mobile-brand" to="/" aria-label="物忆首页"><Brand compact /></Link>
          <Link className="topbar-phone-link" to="/settings#mobile-access"><Smartphone />连接手机</Link>
          <div className="topbar-status" data-runtime-mode={runtime.mode}><Badge tone={runtime.mode === 'REAL' ? 'success' : runtime.mode === 'UNKNOWN' ? 'danger' : 'warning'}>{runtime.mode === 'REAL' ? 'REAL · 真实模式' : runtime.mode}</Badge><Badge tone={health ? 'success' : 'warning'}>{health ? '本地处理' : '后端未连接'}</Badge><span>{health?.version ? `v${health.version}` : '家庭视频留在本机'}</span></div>
        </header>
        <main id="main-content" className="page-content" tabIndex={-1}><Outlet /></main>
      </div>
      <nav className="mobile-nav" aria-label="移动端主导航">
        {navigation.slice(0, 4).map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => isActive ? 'active' : ''}><Icon /><span>{label}</span></NavLink>)}
        <button onClick={() => setOpen(true)}><Menu /><span>更多</span></button>
      </nav>
    </div>
  )
}
