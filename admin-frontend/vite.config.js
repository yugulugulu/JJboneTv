import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const usePolling = process.env.VITE_USE_POLLING === 'true'

export default defineConfig({
  plugins: [react()],
  server: {
    watch: {
      usePolling,
    },
    proxy: {
      '/admin-api': {
        target: process.env.ADMIN_API_PROXY_TARGET || 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
})
