import { expect, test } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const shots = path.resolve(__dirname, "../../docs/reports/phase-6");

test("learning: CSV import shows insights; storage panel on the System page", async ({ page }) => {
  mkdirSync(shots, { recursive: true });
  const today = new Date();
  const d = (h: number) => new Date(today.getTime() - h * 3600_000).toISOString().slice(0, 10);
  const csv = [
    "Video title,Video publish time,Duration,Views,Average percentage viewed (%)",
    `"Jalen Brunt: ""nobody guards me like that"" 🏀",${d(10)},0:22,90000,81`,
    `"Mike Torres: ""he cried in the tunnel"" 😳",${d(20)},0:23,70000,77`,
    `thoughts on the trade deadline and everything else we talked about,${d(15)},0:41,900,30`,
    `our full reaction to the draft lottery results this year,${d(30)},0:44,700,28`,
  ].join("\n");
  const file = path.join(__dirname, "../test-results/studio.csv");
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, csv);

  await page.goto("/learning");
  await expect(page.getByRole("heading", { name: "Learning" })).toBeVisible();
  await page.getByTestId("csv-input").setInputFiles(file);
  const insights = page.getByTestId("insights");
  await expect(insights).toContainText("4 uploads");
  await expect(insights).toContainText("Best length: 20-25 s");
  await expect(insights).toContainText("In use by the picker and copy writer");
  await page.screenshot({ path: path.join(shots, "learning.png"), fullPage: true });

  await page.goto("/system");
  const storage = page.getByTestId("storage");
  await expect(storage).toContainText("Free:");
  await storage.getByRole("button", { name: /Clear re-creatable files/ }).click();
  await expect(storage).toContainText("Freed");
  await page.screenshot({ path: path.join(shots, "system-storage.png"), fullPage: true });
});
