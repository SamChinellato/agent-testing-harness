import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Phase 0 fix #0.15: without this, `npm run dev` on :5173 can't reach
    // any dashboard endpoint on :8000 — every UI change required a full
    // `tsc -b && vite build` to see. See docs/phase-0-hotfixes.md #0.15.
    proxy: {
      '/events': { target: 'http://localhost:8000', changeOrigin: true },
      '/control': { target: 'http://localhost:8000', changeOrigin: true },
      '/register': { target: 'http://localhost:8000', changeOrigin: true },
      '/act': { target: 'http://localhost:8000', changeOrigin: true },
      '/reset': { target: 'http://localhost:8000', changeOrigin: true },
      '/limits': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
