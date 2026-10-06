import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend serves the built SPA from frontend/dist and exposes /api/*.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
