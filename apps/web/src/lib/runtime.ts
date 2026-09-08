import type { ResolvedRuntimeMode, RuntimeConfig } from '../types'

export function browserRuntimeDefaults(): Required<Pick<RuntimeConfig, 'frontend_url' | 'api_base_url' | 'websocket_base_url' | 'backend_healthy' | 'mode'>> & Pick<RuntimeConfig, 'runtime_mode'> {
  const origin = window.location.origin
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return {
    frontend_url: window.location.href,
    api_base_url: `${origin}/api`,
    websocket_base_url: `${wsProtocol}//${window.location.host}/ws`,
    backend_healthy: false,
    mode: 'unknown',
    runtime_mode: 'UNKNOWN',
  }
}

export function normalizeRuntimeConfig(value?: RuntimeConfig | null): RuntimeConfig {
  const fallback = browserRuntimeDefaults()
  return {
    ...fallback,
    ...value,
    frontend_url: value?.frontend_url || fallback.frontend_url,
    api_base_url: value?.api_base_url || fallback.api_base_url,
    websocket_base_url: value?.websocket_base_url || fallback.websocket_base_url,
    mode: value?.mode || (value?.no_hardware_demo ? 'no_hardware_demo' : fallback.mode),
    runtime_mode: value?.runtime_mode || value?.mode || (value?.no_hardware_demo ? 'DEMO' : fallback.runtime_mode),
  }
}

export function runtimeMode(value?: RuntimeConfig | string | null): ResolvedRuntimeMode {
  const config = typeof value === 'string' ? { runtime_mode: value } : value
  const raw = String(config?.runtime_mode || config?.mode || '').trim().toUpperCase().replaceAll('-', '_')
  if (raw === 'REAL' || raw === 'NORMAL') return 'REAL'
  if (raw === 'DEMO' || raw === 'NO_HARDWARE_DEMO' || config?.no_hardware_demo === true) return 'DEMO'
  if (raw === 'TEST' || raw === 'TESTING') return 'TEST'
  return 'UNKNOWN'
}

export function resolveDetectionMode(healthMode?: string | null, settingsMode?: string | null, settingsAvailable = true) {
  return healthMode || (settingsAvailable ? settingsMode : null) || 'unknown'
}
