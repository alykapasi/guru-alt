import { defineConfig, devices } from "@playwright/test";

/** Browser journeys (S58) — the half of the product no backend test can reach.
 *
 * Everything here runs against the *built* frontend rather than the dev server, because the
 * bundle is what ships: `vite preview` serves exactly the artifact `npm run build` produced,
 * where the dev server transforms modules on the fly and can differ from it.
 *
 * Port 5173 is not incidental. It is the origin `cors_origins` allows by default and the one
 * the session cookie's SameSite policy treats as same-site as the API on 8000, so the journey
 * exercises the credential path a developer actually runs rather than a relaxed variant of it. */
const API_PORT = process.env.GURU_E2E_API_PORT ?? "8000";
const WEB_PORT = process.env.GURU_E2E_WEB_PORT ?? "5173";

export default defineConfig({
  testDir: "./e2e",
  // One worker: the journeys commit to a shared database and sign in as the same dev learner,
  // so running them in parallel would have them reading each other's conversations.
  workers: 1,
  fullyParallel: false,
  // A retry hides a flake rather than reporting it, and a flaky journey is a finding.
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "bash ../scripts/e2e-backend.sh",
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: { GURU_E2E_API_PORT: API_PORT },
    },
    {
      command: `npm run build && npm run preview -- --port ${WEB_PORT} --strictPort`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      env: { VITE_API_BASE_URL: `http://localhost:${API_PORT}` },
    },
  ],
});
