import { defineConfig } from "@playwright/test";

// The stack (API, worker, web) must already be running: scripts/dev.sh or scripts\dev.ps1.
// PW_CHROMIUM_PATH lets a machine reuse an existing Chromium instead of `npx playwright install`.
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  use: {
    baseURL: process.env.WEB_URL ?? "http://localhost:3000",
    launchOptions: process.env.PW_CHROMIUM_PATH
      ? { executablePath: process.env.PW_CHROMIUM_PATH }
      : {},
  },
  reporter: [["list"]],
});
