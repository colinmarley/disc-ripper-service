import { defineConfig, devices } from '@playwright/test';

/**
 * E2E config for the plain-JS static UI (backend/static/index.html), served
 * directly by the FastAPI app at GET /. Points at BASE_URL (default:
 * localhost:8083) rather than starting its own server — this service needs
 * makemkvcon/the optical drive to be meaningful, so it's normally run via
 * `docker compose up` on ai-workstation, not spun up ad hoc for tests.
 *
 * Realistic boundary: GET /disc/info wraps a real makemkvcon scan of
 * physical hardware — no spec here should assume a disc is in the drive.
 * Job-list/start/retry/stop flows are backend-mockable and get real pytest
 * coverage (tests/test_api.py); this suite covers the static page actually
 * rendering and wiring up correctly, which pytest can't see.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:8083',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
