import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiTarget = process.env.API_PROXY_TARGET || 'http://localhost:8000'
const usePolling = process.env.VITE_USE_POLLING === 'true'

export default defineConfig({
  plugins: [react()],
  server: {
    watch: {
      usePolling,
    },
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
