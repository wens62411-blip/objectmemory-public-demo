import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { api, ApiError } from './lib/api'
import { ToastProvider } from './components/Toast'
import { AppShell } from './components/AppShell'
import { DashboardPage } from './pages/DashboardPage'
import { SearchPage } from './pages/SearchPage'
import { LivePage } from './pages/LivePage'
import { CamerasPage } from './pages/CamerasPage'
import { ZoneEditorPage } from './pages/ZoneEditorPage'
import { ItemsPage } from './pages/ItemsPage'
import { EventsPage } from './pages/EventsPage'
import { DevicesPage } from './pages/DevicesPage'
import { CompanionPage } from './pages/CompanionPage'
import { SettingsPage } from './pages/SettingsPage'
import { ScenePage } from './pages/ScenePage'
import { CameraDiagnosticsPage } from './pages/CameraDiagnosticsPage'
import { StoragePage } from './pages/StoragePage'
import { ValidationMarkerPage } from './pages/ValidationMarkerPage'
import { RealMovementAcceptancePage } from './pages/RealMovementAcceptancePage'
import { RuntimeProvider } from './contexts/RuntimeContext'
import { LoaderCircle, LockKeyhole, RefreshCw, WifiOff } from 'lucide-react'
import { Brand, BrandMark } from './components/Brand'

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
          <Routes>
            <Route path="/companion" element={<CompanionPage />} />
            <Route path="/companion/validation-marker" element={<ValidationMarkerPage />} />
            <Route element={<AppShell />}>
              <Route index element={<DashboardPage />} />
              <Route path="search" element={<SearchPage />} />
              <Route path="live" element={<LivePage />} />
              <Route path="cameras" element={<CamerasPage />} />
              <Route path="camera-diagnostics" element={<CameraDiagnosticsPage />} />
              <Route path="cameras/:cameraId/zones" element={<ZoneEditorPage />} />
              <Route path="items" element={<ItemsPage />} />
              <Route path="scene" element={<ScenePage />} />
              <Route path="events" element={<EventsPage />} />
              <Route path="devices" element={<DevicesPage />} />
              <Route path="storage" element={<StoragePage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="acceptance/real-movement" element={<RealMovementAcceptancePage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </RuntimeProvider>
      </SessionGate>
    </ToastProvider>
  )
}
