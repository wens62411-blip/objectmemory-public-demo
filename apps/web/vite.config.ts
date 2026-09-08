import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(() => {
  const apiPort = process.env.OM_API_PORT
  const proxy = apiPort ? {
    '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
    '/ws': { target: `ws://127.0.0.1:${apiPort}`, ws: true, changeOrigin: false },
    '/media': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
  } : undefined
  return {
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy,
  },
  }
})
