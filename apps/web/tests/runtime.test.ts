import { describe, expect, it } from 'vitest'
import { wsUrl } from '../src/lib/api'
import { browserCameraError, cameraStatusText } from '../src/lib/cameraStatus'
import { browserRuntimeDefaults, normalizeRuntimeConfig, resolveDetectionMode, runtimeMode } from '../src/lib/runtime'

describe('运行时地址与摄像头错误', () => {
  it('从当前页面推导 API 和 WebSocket，不写死后端端口', () => {
    const runtime = browserRuntimeDefaults()
    expect(runtime.api_base_url).toBe(`${window.location.origin}/api`)
    expect(runtime.websocket_base_url).toBe(wsUrl('/ws'))
  })

  it('保留后端返回的实际运行时地址', () => {
    const runtime = normalizeRuntimeConfig({ api_base_url: 'http://127.0.0.1:8019/api', websocket_base_url: 'ws://127.0.0.1:8019/ws', backend_healthy: true })
    expect(runtime.api_base_url).toContain(':8019/api')
    expect(runtime.websocket_base_url).toContain(':8019/ws')
    expect(runtime.backend_healthy).toBe(true)
  })

  it('显式区分 REAL、DEMO、TEST，缺失模式时失败关闭', () => {
    expect(runtimeMode({ runtime_mode: 'REAL' })).toBe('REAL')
    expect(runtimeMode({ runtime_mode: 'DEMO' })).toBe('DEMO')
    expect(runtimeMode({ runtime_mode: 'TEST' })).toBe('TEST')
    expect(runtimeMode({})).toBe('UNKNOWN')
  })

  it('把占用与权限错误翻译成可执行的中文', () => {
    expect(browserCameraError(new DOMException('', 'NotReadableError'))).toContain('其他程序占用')
    expect(browserCameraError(new DOMException('', 'NotAllowedError'))).toContain('摄像头权限')
    expect(cameraStatusText('BUSY')).toContain('占用')
    expect(cameraStatusText('PERMISSION_DENIED')).toContain('隐私设置')
  })

  it('健康状态和设置都没有检测方式时失败关闭', () => {
    expect(resolveDetectionMode('aruco', 'experimental')).toBe('aruco')
    expect(resolveDetectionMode(undefined, 'experimental')).toBe('experimental')
    expect(resolveDetectionMode(undefined, undefined)).toBe('unknown')
    expect(resolveDetectionMode(undefined, 'aruco', false)).toBe('unknown')
  })
})
