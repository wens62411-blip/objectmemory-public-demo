import { render, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RuntimeModeBanner } from '../src/components/RuntimeModeBanner'

describe('持续运行模式标识', () => {
  it('在 DEMO 和 TEST 模式明确说明数据不是真实家庭摄像头事件', () => {
    const view = render(<RuntimeModeBanner mode="DEMO" />)
    expect(within(view.container).getByRole('status')).toHaveTextContent('演示模式，当前事件不来自真实家庭摄像头。')
    view.rerender(<RuntimeModeBanner mode="TEST" />)
    expect(within(view.container).getByRole('status')).toHaveTextContent('测试模式，当前数据只供自动化验收')
  })

  it('手机伴侣可持续显示 REAL 标识，普通壳层可隐藏 REAL 横幅', () => {
    const view = render(<RuntimeModeBanner mode="REAL" showReal />)
    expect(within(view.container).getByRole('status')).toHaveTextContent('REAL · 真实模式')
    view.rerender(<RuntimeModeBanner mode="REAL" />)
    expect(within(view.container).queryByRole('status')).not.toBeInTheDocument()
  })
})
