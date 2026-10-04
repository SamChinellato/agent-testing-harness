/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dashboard API runs on :8000. In development the Vite server proxies
// every backend path so the UI can hot-reload against a live backend.
const backend = process.env.HARNESS_BACKEND ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
  },
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/events': { target: backend, changeOrigin: true },
      '/metrics': { target: backend, changeOrigin: true },
    },
  },
})
