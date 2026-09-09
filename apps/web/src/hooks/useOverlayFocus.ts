import { useEffect, useRef, type RefObject } from 'react'
import { useToast } from '../components/Toast'

// React can clean a parent effect before its nested overlays. Restore the page's
// original state only after the last owner releases it, regardless of that order.
const inertLocks = new WeakMap<HTMLElement, { count: number; initial: boolean }>()
const scrollLocks = new WeakMap<HTMLElement, { count: number; value: string; priority: string }>()

function isolateBackground(element: HTMLElement) {
  const lock = inertLocks.get(element) ?? { count: 0, initial: element.inert }
  lock.count += 1
  inertLocks.set(element, lock)
  element.inert = true
  return () => {
    if (--lock.count === 0) {
      element.inert = lock.initial
      inertLocks.delete(element)
    }
  }
}

function lockScroll(body: HTMLElement) {
  const lock = scrollLocks.get(body) ?? {
    count: 0,
    value: body.style.getPropertyValue('overflow'),
    priority: body.style.getPropertyPriority('overflow'),
  }
  lock.count += 1
  scrollLocks.set(body, lock)
  body.style.setProperty('overflow', 'hidden')
  return () => {
    if (--lock.count === 0) {
      body.style.setProperty('overflow', lock.value, lock.priority)
      scrollLocks.delete(body)
    }
  }
}

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
    const releaseScroll = lockScroll(document.body)
    const releaseBackground: Array<() => void> = []
    for (let branch: HTMLElement | null = node; branch?.parentElement; branch = branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling instanceof HTMLElement && sibling !== branch && !sibling.hasAttribute('data-overlay-dismiss')) {
          releaseBackground.push(isolateBackground(sibling))
        }
      }
      if (branch.parentElement === document.body) break
    }
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
      releaseBackground.forEach(release => release())
      releaseScroll()
      if (trigger?.isConnected) trigger.focus({ preventScroll: true })
    }
  }, [active, ref, registerOverlay])
}
