import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(() => {
  const apiTarget = process.env.PIVOT_API_URL ?? 'http://127.0.0.1:8000'
  const wsTarget = apiTarget.replace(/^http/, 'ws')

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': apiTarget,
        '/ws': { target: wsTarget, ws: true },
      },
    },
  }
})
