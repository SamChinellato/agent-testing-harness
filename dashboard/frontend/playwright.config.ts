import { defineConfig, devices } from '@playwright/test';

// The suite runs against the real dashboard backend, started here on a
// throwaway database. It serves the built UI from dashboard/frontend/dist,
// so `npm run test:e2e` builds first.
const PORT = 8766;

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `uv run python -c "import uvicorn; uvicorn.run('maasagentharness.dashboard.app:app', host='127.0.0.1', port=${PORT}, log_level='warning')"`,
    cwd: '../..',
    url: `http://127.0.0.1:${PORT}/api/stream?once=1`,
    reuseExistingServer: false,
    timeout: 90_000,
    env: {
      HARNESS_DB_PATH: `/tmp/harness-e2e-${process.pid}.db`,
      SCENARIOS_DIR: 'scenarios',
    },
  },
});
