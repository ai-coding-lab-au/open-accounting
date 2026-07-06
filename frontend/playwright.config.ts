import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end Playwright config.
 *
 * Tests live under `frontend/e2e/` and run against a separately-managed
 * backend + frontend pair (no `webServer:` block). The convention:
 *
 *   1. Start backend on 127.0.0.1:8765 with DATA_DIR pointing at a
 *      throwaway tree under /tmp/accounting-e2e-data.
 *   2. Start Vite on 127.0.0.1:5174 with VITE_E2E_BACKEND_URL set so
 *      the dev proxy targets that backend instead of the default :8000.
 *   3. Run `npx playwright test`.
 *
 * All of this is wrapped by `e2e/run-e2e.sh`, which `npm run test:e2e`
 * invokes. Use `npm run test:e2e:raw` to drive Playwright directly
 * against a stack you started yourself.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // backend has shared state per company
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:5174",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
