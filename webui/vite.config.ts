import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发代理：把 /api 与 /ws 转发到本机后端（T-6.5）
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
