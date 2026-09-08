import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { api, ApiError } from './lib/api'
import { ToastProvider } from './components/Toast'
import { AppShell } from './components/AppShell'
import { DashboardPage } from './pages/DashboardPage'
import { Loading } from './components/UI'
import { RuntimeProvider } from './contexts/RuntimeContext'
import { LoaderCircle, LockKeyhole, RefreshCw, WifiOff } from 'lucide-react'
import { Brand, BrandMark } from './components/Brand'

const CompanionPage = lazy(() => import('./pages/CompanionPage').then(m => ({ default: m.CompanionPage })))
const ValidationMarkerPage = lazy(() => import('./pages/ValidationMarkerPage').then(m => ({ default: m.ValidationMarkerPage })))
const pages = [
  { path: 'search', Component: lazy(() => import('./pages/SearchPage').then(m => ({ default: m.SearchPage }))) },
  { path: 'live', Component: lazy(() => import('./pages/LivePage').then(m => ({ default: m.LivePage }))) },
  { path: 'cameras', Component: lazy(() => import('./pages/CamerasPage').then(m => ({ default: m.CamerasPage }))) },
  { path: 'camera-diagnostics', Component: lazy(() => import('./pages/CameraDiagnosticsPage').then(m => ({ default: m.CameraDiagnosticsPage }))) },
  { path: 'cameras/:cameraId/zones', Component: lazy(() => import('./pages/ZoneEditorPage').then(m => ({ default: m.ZoneEditorPage }))) },
  { path: 'items', Component: lazy(() => import('./pages/ItemsPage').then(m => ({ default: m.ItemsPage }))) },
  { path: 'scene', Component: lazy(() => import('./pages/ScenePage').then(m => ({ default: m.ScenePage }))) },
  { path: 'events', Component: lazy(() => import('./pages/EventsPage').then(m => ({ default: m.EventsPage }))) },
  { path: 'devices', Component: lazy(() => import('./pages/DevicesPage').then(m => ({ default: m.DevicesPage }))) },
  { path: 'storage', Component: lazy(() => import('./pages/StoragePage').then(m => ({ default: m.StoragePage }))) },
  { path: 'settings', Component: lazy(() => import('./pages/SettingsPage').then(m => ({ default: m.SettingsPage }))) },
  { path: 'acceptance/real-movement', Component: lazy(() => import('./pages/RealMovementAcceptancePage').then(m => ({ default: m.RealMovementAcceptancePage }))) },
]

function SessionGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<'loading' | 'ready' | 'pin' | 'error'>('loading')
  const [pin, setPin] = useState('')
  const [error, setError] = useState('')
  const [pairing, setPairing] = useState(false)

  const checkSession = useCallback(() => {
    setState('loading')
    setError('')
    api<{ authenticated?: boolean }>('/api/session')
      .then((session) => setState(session.authenticated ? 'ready' : 'pin'))
      .catch((value) => {
        if (value instanceof ApiError && (value.status === 401 || value.status === 403)) setState('pin')
        else { setError('还没有连上这台物忆电脑，请确认电脑仍在运行，手机和电脑连接同一 Wi-Fi。'); setState('error') }
      })
  }, [])
  useEffect(checkSession, [checkSession])

  async function pair(event: React.FormEvent) {
    event.preventDefault()
    setError('')
    setPairing(true)
    try {
      await api('/api/session', { method: 'POST', json: { pin } })
      setState('ready')
    } catch (value) {
      setError(value instanceof Error ? value.message : '配对失败，请检查 PIN')
    } finally { setPairing(false) }
  }

  if (state === 'loading') return <div className="app-loading"><Brand /><LoaderCircle className="spin" /><span>正在连接你的物忆电脑…</span></div>
  if (state === 'error') return <main className="pair-page"><section className="pair-card"><Brand /><WifiOff /><h1>暂时没有连接上</h1><p role="alert">{error}</p><button className="button primary full" onClick={checkSession}><RefreshCw />重新连接</button><small>如果使用扫码链接，请回电脑「设置 → 连接手机」重新确认当前地址。</small></section></main>
  if (state === 'pin') return (
    <main className="pair-page">
      <form className="pair-card" onSubmit={pair}>
        <BrandMark large /><LockKeyhole />
        <p className="eyebrow">欢迎来到物忆</p><h1>和家里的电脑连接</h1>
        <p>请在电脑上打开物忆「设置 → 连接手机」，查看 8 位访问码。验证后，就能在这部手机上使用物忆。</p>
        <label className="field"><span className="field-label">8 位主机访问码</span><input inputMode="numeric" autoComplete="one-time-code" autoFocus maxLength={8} value={pin} onChange={(event) => setPin(event.target.value.replace(/\D/g, ''))} placeholder="输入 8 位数字" /></label>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <button className="button primary full" disabled={pin.length !== 8 || pairing}>{pairing ? '正在验证…' : '安全连接'}</button>
        <small>家庭视频只在主机本地处理，不会上传。</small>
      </form>
    </main>
  )
  return <>{children}</>
}

export function App() {
  return (
    <ToastProvider>
      <SessionGate>
        <RuntimeProvider>
          <Suspense fallback={<Loading label="正在加载页面…" />}>
            <Routes>
              <Route path="/companion" element={<CompanionPage />} />
              <Route path="/companion/validation-marker" element={<ValidationMarkerPage />} />
              <Route element={<AppShell />}>
                <Route index element={<DashboardPage />} />
                {pages.map(({ path, Component }) => <Route key={path} path={path} element={<Component />} />)}
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </RuntimeProvider>
      </SessionGate>
    </ToastProvider>
  )
}
