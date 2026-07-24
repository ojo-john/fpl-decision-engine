import { defineConfig, devices } from "@playwright/test";

const PORT = 3002;
const BASE_URL = `http://localhost:${PORT}`;

/**
 * Drives the real, running app end-to-end -- login, navigation, real forms hitting the real
 * backend (see e2e/README.md for exactly what "real" means here and why this suite is
 * deliberately NOT wired into ci.yml).
 *
 * webServer auto-starts `next dev` on its own port (3000 is docker-compose's own `web`
 * container, kept free so this suite never collides with it) and points at the backend
 * already running on API_BASE_URL (see .env.local) -- this suite never touches Docker itself.
 *
 * No Playwright browser download: `channel: "chrome"` with executablePath pinned to this
 * machine's real installed Chrome, the same approach every ad-hoc verification script used
 * this session (no separate ~300MB browser binary download needed).
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // shared, non-truncated Postgres -- serialize to avoid state races
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  timeout: 30_000,
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    launchOptions: {
      executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: `npm run dev -- --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
