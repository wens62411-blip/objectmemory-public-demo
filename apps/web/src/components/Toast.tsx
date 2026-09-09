import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react'
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

type Tone = 'success' | 'danger' | 'info'
interface ToastData { id: number; message: string; tone: Tone }
interface ToastContextValue {
  notify: (message: string, tone?: Tone) => void
  registerOverlay: (element: HTMLElement) => () => void
}

const ToastContext = createContext<ToastContextValue>({ notify: () => undefined, registerOverlay: () => () => undefined })

function canRestoreFocus(element: HTMLElement | null | undefined): element is HTMLElement {
  if (!element?.isConnected || element.matches(':disabled')) return false
  for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.inert) return false
  }
  return true
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastData[]>([])
  const [overlays, setOverlays] = useState<HTMLElement[]>([])
  const registerOverlay = useCallback((element: HTMLElement) => {
    setOverlays(value => [...value, element])
    return () => setOverlays(value => value.filter(entry => entry !== element))
  }, [])
  const target = overlays.at(-1)
  const currentTarget = useRef(target)
  currentTarget.current = target
  const focusOrigins = useRef(new Map<number, HTMLElement>())
  const dismiss = useCallback((id: number) => {
    // Only repair focus if it is about to be removed with this notification.
    // Expiring a background notice must not interrupt typing elsewhere.
    const active = document.activeElement
    if (active instanceof HTMLElement && active.closest('[data-toast-id]')?.getAttribute('data-toast-id') === String(id)) {
      const destination = [currentTarget.current, focusOrigins.current.get(id), document.querySelector<HTMLElement>('main[tabindex]')].find(canRestoreFocus)
      destination?.focus({ preventScroll: true })
    }
    focusOrigins.current.delete(id)
    setItems(value => value.filter(item => item.id !== id))
  }, [])
  const notify = useCallback((message: string, tone: Tone = 'info') => {
    const id = Date.now() + Math.random()
    setItems((value) => [...value, { id, message, tone }])
    window.setTimeout(() => dismiss(id), 4200)
  }, [dismiss])
  const value = useMemo(() => ({ notify, registerOverlay }), [notify, registerOverlay])
  // Keep the live region and its controls inside the active focus boundary.
  const notifications = <div className="toast-stack" aria-live="polite">
    {items.map((item) => {
      const Icon = item.tone === 'success' ? CheckCircle2 : item.tone === 'danger' ? CircleAlert : Info
      return (
        <div className={`toast toast-${item.tone}`} key={item.id} data-toast-id={item.id} onFocusCapture={event => {
          const origin = event.relatedTarget
          if (origin instanceof HTMLElement && !event.currentTarget.contains(origin) && !origin.closest('[data-toast-id]')) focusOrigins.current.set(item.id, origin)
        }}>
          <Icon size={18} /><span>{item.message}</span>
          <button type="button" className="icon-button compact" aria-label="关闭提示" onClick={() => dismiss(item.id)}><X /></button>
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
