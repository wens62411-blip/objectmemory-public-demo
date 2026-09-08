import { useEffect, useRef, type RefObject } from 'react'
import { useToast } from '../components/Toast'

/** Shared keyboard and background isolation for dialogs and the mobile drawer. */
export function useOverlayFocus(ref: RefObject<HTMLElement>, onClose: () => void, active = true) {
  const { registerOverlay } = useToast()
  const initialFocus = useRef(document.activeElement as HTMLElement | null)
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const node = ref.current
    if (!active || !node) return
    const unregisterOverlay = registerOverlay(node)
    const trigger = node.contains(document.activeElement) ? initialFocus.current : document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    const background = new Map<HTMLElement, boolean>()
    for (let branch: HTMLElement | null = node; branch?.parentElement; branch = branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling instanceof HTMLElement && sibling !== branch && !sibling.hasAttribute('data-overlay-dismiss')) {
          background.set(sibling, sibling.inert)
          sibling.inert = true
        }
      }
      if (branch.parentElement === document.body) break
    }
    document.body.style.overflow = 'hidden'
    if (!node.contains(document.activeElement)) node.focus({ preventScroll: true })
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close.current(); return }
      if (event.key !== 'Tab') return
      const controls = Array.from(node.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, summary, [tabindex]')).filter(element => {
        const collapsed = element.closest('details:not([open])')
        return element.tabIndex >= 0 && !element.matches(':disabled, [inert]') && element.getClientRects().length > 0
          && (!collapsed || Boolean(collapsed.querySelector(':scope > summary')?.contains(element)))
      })
      const first = controls[0], last = controls.at(-1)
      if (!first) { event.preventDefault(); node.focus() }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === node)) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    node.addEventListener('keydown', onKeyDown)
    return () => {
      unregisterOverlay()
      node.removeEventListener('keydown', onKeyDown)
      background.forEach((inert, element) => { element.inert = inert })
      document.body.style.overflow = previousOverflow
      if (trigger?.isConnected) trigger.focus({ preventScroll: true })
    }
  }, [active, ref, registerOverlay])
}
