import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

// The backend currently binds to IPv4 (0.0.0.0:8000).  On Windows, using
// `localhost` here can resolve to ::1 first, which makes the Vite proxy fail
// while direct requests to 127.0.0.1 work.  Keep the target explicit and allow
// an override for Docker/remote development.
const devApiTarget = process.env.ECHOTRACE_DEV_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: devApiTarget,
        changeOrigin: true,
      },
      '/static': {
        target: devApiTarget,
        changeOrigin: true,
      },
    },
  },
})
