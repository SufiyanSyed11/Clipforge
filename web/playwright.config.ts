import { defineConfig } from "@playwright/test";

// The stack (API, worker, web) must already be running: scripts/dev.sh or scripts\dev.ps1.
// PW_CHROMIUM_PATH lets a machine reuse an existing Chromium instead of `npx playwright install`.
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  // The tests share one API + job worker; in parallel, one test's clip builds queue ahead of
  // another's export and its timeouts measure queue contention instead of the app.
  workers: 1,
  use: {
    baseURL: process.env.WEB_URL ?? "http://localhost:3000",
    launchOptions: process.env.PW_CHROMIUM_PATH
      ? { executablePath: process.env.PW_CHROMIUM_PATH }
      : {},
  },
  reporter: [["list"]],
});
