import type {
  AcceptanceMarkerBinding,
  AcceptanceMarkerStatus,
  AcceptanceRun,
  AcceptanceSuite,
  CreateAcceptanceRunRequest,
  CreateAcceptanceSuiteRequest,
} from '../types'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

type Options = RequestInit & { json?: unknown }

export const SESSION_REQUIRED_EVENT = 'objectmemory:session-required'
let sessionGeneration = 0
let sessionRecovery: Promise<boolean> | null = null
let recoveryBlocked = false
let recoveryRetryAfter = 0

function requireSession(reason: 'pin' | 'error') {
  if (recoveryBlocked) return
  recoveryBlocked = true
  window.dispatchEvent(new CustomEvent(SESSION_REQUIRED_EVENT, { detail: reason }))
}

function recoverSession(): Promise<boolean> {
  if (sessionRecovery) return sessionRecovery
  if (recoveryBlocked || Date.now() < recoveryRetryAfter) return Promise.resolve(false)
  // Only this existing server endpoint decides whether a local session may be
  // issued. A LAN client never submits or guesses a PIN during recovery.
  sessionRecovery = (async () => {
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 5000)
    try {
      const response = await fetch('/api/session', { credentials: 'same-origin', signal: controller.signal, cache: 'no-store' })
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) requireSession('pin')
        else recoveryRetryAfter = Date.now() + 5000
        return false
      }
      const session = await response.json() as { authenticated?: boolean }
      if (session.authenticated !== true) { requireSession('pin'); return false }
      sessionGeneration += 1
      return true
    } catch {
      // Keep network failures retryable on a later normal poll, not a tight loop.
      recoveryRetryAfter = Date.now() + 5000
      return false
    } finally { window.clearTimeout(timer) }
  })().finally(() => { sessionRecovery = null })
  return sessionRecovery
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const { json, headers, ...rest } = options
  const generation = sessionGeneration
  const sessionEndpoint = path.split('?')[0] === '/api/session'
  const method = (rest.method || 'GET').toUpperCase()
  let response: Response
  try {
    const send = () => fetch(path, {
      credentials: 'same-origin',
      ...rest,
      headers: {
        ...(json === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...headers,
      },
      body: json === undefined ? rest.body : JSON.stringify(json),
    })
    response = await send()
    // Never replay POST/PATCH/DELETE (including registration and flashing),
    // external URLs, or the session endpoint itself. One safe read retry only.
    if (response.status === 401 && path.startsWith('/api/') && !sessionEndpoint
      && ['GET', 'HEAD'].includes(method) && !rest.signal?.aborted && !recoveryBlocked) {
      const recovered = generation !== sessionGeneration || await recoverSession()
      if (recovered && !rest.signal?.aborted) {
        response = await send()
        if (response.status === 401) requireSession('error')
      }
    }
  } catch (error) {
    const reason = error instanceof Error && error.message ? `（${error.message}）` : ''
    throw new ApiError(`后端未连接：无法访问物忆本地服务${reason}`, 0)
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = await response.json() as { detail?: string; message?: string }
      message = body.detail || body.message || message
    } catch {
      // Keep the plain-language fallback for non-JSON failures.
    }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204 || method === 'HEAD') return undefined as T
  const result = await response.json()
  if (sessionEndpoint && result?.authenticated === true) {
    // Explicit bootstrap/reconnect or user-entered pairing reopens a blocked
    // recovery. No PIN or session token is retained in this client module.
    recoveryBlocked = false; recoveryRetryAfter = 0; sessionGeneration += 1
  }
  return result as T
}

export function wsUrl(path: string) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${path}`
}

export function mediaUrl(path?: string | null) {
  if (!path) return undefined
  if (/^https?:\/\//.test(path) || path.startsWith('/')) return path
  return `/media/${path.replace(/^media[\\/]/, '').replaceAll('\\', '/')}`
}

export function queryString(values: Record<string, string | number | undefined | null>) {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  const text = params.toString()
  return text ? `?${text}` : ''
}

export const acceptanceApi = {
  createSuite(payload: CreateAcceptanceSuiteRequest) {
    return api<AcceptanceSuite>('/api/acceptance/suites', { method: 'POST', json: payload })
  },
  suite(suiteId: string) {
    return api<AcceptanceSuite>(`/api/acceptance/suites/${encodeURIComponent(suiteId)}`)
  },
  createRun(payload: CreateAcceptanceRunRequest) {
    return api<AcceptanceRun>('/api/acceptance/runs', { method: 'POST', json: payload })
  },
  startRun(runId: string) {
    return api<AcceptanceRun>(`/api/acceptance/runs/${encodeURIComponent(runId)}/start`, { method: 'POST' })
  },
  cancelRun(runId: string, reason = 'user_cancelled_from_acceptance_ui') {
    return api<AcceptanceRun>(`/api/acceptance/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', json: { reason } })
  },
  disconnectReconnect(runId: string) {
    return api<AcceptanceRun>(`/api/acceptance/runs/${encodeURIComponent(runId)}/disconnect-reconnect`, { method: 'POST' })
  },
  bindMarker(itemId: string, arucoId?: number | null) {
    return api<AcceptanceMarkerBinding>(`/api/acceptance/items/${encodeURIComponent(itemId)}/marker`, { method: 'POST', json: arucoId === undefined || arucoId === null ? {} : { aruco_id: arucoId } })
  },
  markerStatus(filters: { validationRunId?: string | null; itemId?: string | null; cameraId?: string | null } = {}) {
    return api<AcceptanceMarkerStatus>(`/api/acceptance/marker-status${queryString({
      validation_run_id: filters.validationRunId,
      item_id: filters.itemId,
      camera_id: filters.cameraId,
    })}`)
  },
  markerImageUrl(markerId: number, size = 1000, nonce?: number) {
    return `/api/acceptance/markers/${encodeURIComponent(String(markerId))}.png${queryString({ size, v: nonce })}`
  },
}
