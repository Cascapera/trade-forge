import { defineConfig, devices } from '@playwright/test'

// The happy-path E2E drives the real built UI in a real browser, with the API mocked at the
// network boundary (see the spec) — so it needs the dev server up but no backend, Postgres or
// Redis. Playwright starts the server itself and reuses one already running in dev.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
