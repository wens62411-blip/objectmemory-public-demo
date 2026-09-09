import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { StrictMode, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider, useToast } from '../src/components/Toast'
import { Modal } from '../src/components/UI'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  document.body.style.removeProperty('overflow')
})

describe('overlay isolation lifecycle', () => {
  function Nested({ visible = true }: { visible?: boolean }) {
    const [inner, setInner] = useState(false)
    return <>
      <button>Page action</button>
      {visible && <Modal title="Outer" onClose={() => undefined}>
        <button onClick={() => setInner(true)}>Open inner</button>
        {inner && <Modal title="Inner" onClose={() => setInner(false)}><button>Inner action</button></Modal>}
      </Modal>}
    </>
  }

  it.each([false, true])('unlocks after parent and child are removed together (StrictMode=%s)', (strict) => {
    const renderTree = (visible: boolean) => {
      const tree = <ToastProvider><Nested visible={visible} /></ToastProvider>
      return strict ? <StrictMode>{tree}</StrictMode> : tree
    }
    const view = render(renderTree(true))
    const pageAction = screen.getByText('Page action')
    fireEvent.click(screen.getByRole('button', { name: 'Open inner' }))
    expect(screen.getAllByRole('dialog')).toHaveLength(2)
    expect(pageAction.inert).toBe(true)
    expect(document.body.style.overflow).toBe('hidden')

    // Route/session changes can remove the complete modal subtree in one commit.
    view.rerender(renderTree(false))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(pageAction.inert).not.toBe(true)
    expect(document.body.style.overflow).toBe('')
  })

  it('keeps the outer lock until the last overlay closes', () => {
    const view = render(<ToastProvider><Nested /></ToastProvider>)
    const pageAction = screen.getByText('Page action')
    fireEvent.click(screen.getByRole('button', { name: 'Open inner' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Inner' })).getByRole('button', { name: '关闭' }))
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(pageAction.inert).toBe(true)
    expect(document.body.style.overflow).toBe('hidden')
    view.rerender(<ToastProvider><Nested visible={false} /></ToastProvider>)
    expect(pageAction.inert).not.toBe(true)
    expect(document.body.style.overflow).toBe('')
  })

  it('preserves prior inert and scroll styles after the whole route unmounts', () => {
    const external = document.createElement('section')
    external.inert = true
    document.body.append(external)
    document.body.style.setProperty('overflow', 'scroll', 'important')
    try {
      const view = render(<ToastProvider><Nested /></ToastProvider>)
      fireEvent.click(screen.getByRole('button', { name: 'Open inner' }))
      act(() => view.unmount())
      expect(external.inert).toBe(true)
      expect(document.body.style.overflow).toBe('scroll')
      expect(document.body.style.getPropertyPriority('overflow')).toBe('important')
    } finally { external.remove() }
  })

  function TimedNotice({ modal = true }: { modal?: boolean }) {
    const [open, setOpen] = useState(modal)
    const { notify } = useToast()
    const controls = <><button onClick={() => notify('Saved notice')}>Notify</button><input aria-label="Unrelated input" /></>
    return open ? <Modal title="Notice owner" onClose={() => setOpen(false)}>{controls}</Modal> : controls
  }

  it('returns focus from an expired notice to the live modal so Escape still works', () => {
    vi.useFakeTimers()
    render(<ToastProvider><TimedNotice /></ToastProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Notify' }))
    screen.getByRole('button', { name: '关闭提示' }).focus()
    act(() => { vi.advanceTimersByTime(4200) })
    expect(screen.queryByText('Saved notice')).not.toBeInTheDocument()
    expect(screen.getByRole('dialog')).toHaveFocus()
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('does not steal focus from another input when a notice expires', () => {
    vi.useFakeTimers()
    render(<ToastProvider><TimedNotice /></ToastProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Notify' }))
    const input = screen.getByRole('textbox', { name: 'Unrelated input' })
    input.focus()
    act(() => { vi.advanceTimersByTime(4200) })
    expect(input).toHaveFocus()
  })

  it('uses the current nested overlay, not the overlay present when the timer started', () => {
    vi.useFakeTimers()
    function LaterOverlay() {
      const { notify } = useToast()
      const [inner, setInner] = useState(false)
      return <Modal title="First" onClose={() => undefined}>
        <button onClick={() => notify('Delayed notice')}>Notify</button>
        <button onClick={() => setInner(true)}>Open later</button>
        {inner && <Modal title="Later" onClose={() => setInner(false)}>{null}</Modal>}
      </Modal>
    }
    render(<ToastProvider><LaterOverlay /></ToastProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Notify' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open later' }))
    const later = screen.getByRole('dialog', { name: 'Later' })
    within(later).getByRole('button', { name: '关闭提示' }).focus()
    act(() => { vi.advanceTimersByTime(4200) })
    expect(later).toHaveFocus()
    fireEvent.keyDown(later, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Later' })).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'First' })).toBeInTheDocument()
  })

  it('returns to the prior page control when a focused notice expires outside a modal', () => {
    vi.useFakeTimers()
    render(<ToastProvider><TimedNotice modal={false} /></ToastProvider>)
    const trigger = screen.getByRole('button', { name: 'Notify' })
    trigger.focus()
    fireEvent.click(trigger)
    screen.getByRole('button', { name: '关闭提示' }).focus()
    act(() => { vi.advanceTimersByTime(4200) })
    expect(trigger).toHaveFocus()
  })
})
