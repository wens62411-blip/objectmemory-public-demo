import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { usePolling } from '../hooks/useApi'
import { normalizeRuntimeConfig, runtimeMode } from '../lib/runtime'
import type { ResolvedRuntimeMode, RuntimeConfig } from '../types'

interface RuntimeContextValue {
  config: RuntimeConfig
  mode: ResolvedRuntimeMode
  loading: boolean
  error: string
  reload: () => Promise<void>
}

const RuntimeContext = createContext<RuntimeContextValue>({
  config: {},
  mode: 'UNKNOWN',
  loading: true,
  error: '',
  reload: async () => undefined,
})

export function RuntimeProvider({ children }: { children: ReactNode }) {
  const state = usePolling<RuntimeConfig>('/api/runtime-config', {}, 10_000)
  const config = useMemo(() => normalizeRuntimeConfig(state.data), [state.data])
  const value = useMemo<RuntimeContextValue>(() => ({
    config,
    mode: state.error ? 'UNKNOWN' : runtimeMode(config),
    loading: state.loading,
    error: state.error,
    reload: state.reload,
  }), [config, state.error, state.loading, state.reload])
  return <RuntimeContext.Provider value={value}>{children}</RuntimeContext.Provider>
}

export const useRuntime = () => useContext(RuntimeContext)
