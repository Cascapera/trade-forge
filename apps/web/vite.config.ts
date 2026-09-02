/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // The dev server proxies the API so the browser talks to one origin (no CORS) and the
    // client can address the backend with a plain `/api` prefix, whatever host it runs on.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
        // ⚠️ Without this the live-session feed does not connect, and it fails the worst way a
        // proxy can: the upgrade request is served as an ordinary HTTP request, so the browser
        // reports a closed socket and nothing anywhere says "your proxy does not do WebSocket".
        // The same line has a counterpart in `nginx.conf` for the built image.
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.ts'],
    // The Playwright specs live in `e2e/` and run under their own runner, not vitest.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      // main.tsx only mounts the app into a real DOM root; testing it would assert
      // that React works. setupTests and the tests themselves are not subjects.
      exclude: [
        'src/main.tsx',
        'src/setupTests.ts',
        'src/test-utils.tsx',
        'src/**/*.test.{ts,tsx}',
      ],
      thresholds: {
        lines: 90,
        functions: 90,
        branches: 90,
        statements: 90,
      },
    },
  },
})
