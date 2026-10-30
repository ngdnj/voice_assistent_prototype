import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Локально REST работает в `python -m app.mcp_server` на 8001; в MAX сайт и /api на одном домене.
    proxy: { '/api': 'http://127.0.0.1:8001' },
  },
})
