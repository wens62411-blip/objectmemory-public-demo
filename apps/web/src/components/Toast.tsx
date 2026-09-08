import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react'
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

type Tone = 'success' | 'danger' | 'info'
interface ToastData { id: number; message: string; tone: Tone }
interface ToastContextValue {
  notify: (message: string, tone?: Tone) => void
  registerOverlay: (element: HTMLElement) => () => void
}

const ToastContext = createContext<ToastContextValue>({ notify: () => undefined, registerOverlay: () => () => undefined })

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastData[]>([])
  const [overlays, setOverlays] = useState<HTMLElement[]>([])
  const registerOverlay = useCallback((element: HTMLElement) => {
    setOverlays(value => [...value, element])
    return () => setOverlays(value => value.filter(entry => entry !== element))
  }, [])
  const target = overlays.at(-1)
  const notify = useCallback((message: string, tone: Tone = 'info') => {
    const id = Date.now() + Math.random()
    setItems((value) => [...value, { id, message, tone }])
    window.setTimeout(() => setItems((value) => value.filter((item) => item.id !== id)), 4200)
  }, [])
  const value = useMemo(() => ({ notify, registerOverlay }), [notify, registerOverlay])
  // Keep the live region and its controls inside the active focus boundary.
  const notifications = <div className="toast-stack" aria-live="polite">
    {items.map((item) => {
      const Icon = item.tone === 'success' ? CheckCircle2 : item.tone === 'danger' ? CircleAlert : Info
      return (
        <div className={`toast toast-${item.tone}`} key={item.id}>
          <Icon size={18} /><span>{item.message}</span>
          <button type="button" className="icon-button compact" aria-label="关闭提示" onClick={() => { target?.focus({ preventScroll: true }); setItems((value) => value.filter((entry) => entry.id !== item.id)) }}><X /></button>
        </div>
      )
    })}
  </div>
  return (
    <ToastContext.Provider value={value}>
      {children}
      {target ? createPortal(notifications, target) : notifications}
    </ToastContext.Provider>
  )
}

export const useToast = () => useContext(ToastContext)
