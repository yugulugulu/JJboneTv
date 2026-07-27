import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/admin-api': {
        target: process.env.ADMIN_API_PROXY_TARGET || 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
})
