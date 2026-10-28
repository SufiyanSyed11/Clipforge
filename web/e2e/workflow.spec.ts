import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

// The complete workflow in the browser with FAKE_MODELS test doubles (plumbing, not quality):
// settings -> import -> transcribe -> host -> clips -> editor -> copy -> export -> download.

const fixtureDir = path.join(__dirname, "..", "test-results", "fixtures");
const video = path.join(fixtureDir, "workflow.mp4");

test.beforeAll(() => {
  mkdirSync(fixtureDir, { recursive: true });
  const python = process.env.PYTHON ?? "python";
  execFileSync(python, ["-m", "clipforge.testing.synth", video, "--seconds", "60"]);
});

test("full workflow: settings, import, edit, copy, export", async ({ page }) => {
  test.setTimeout(240_000);
  // Settings: small renders for speed; the brand tag is the default.
  await page.goto("/settings");
  await expect(page.getByTestId("settings")).toBeVisible();
  await page.getByRole("combobox").first().selectOption("720x1280");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByTestId("settings-msg")).toContainText("Saved");

  await page.goto("/");
  await page.getByRole("button", { name: "Upload file" }).click();
  await page.getByLabel("Episode file").setInputFiles(video);
  await page.getByLabel("Project name").fill("Workflow episode");
  await page.getByLabel("Clip count").fill("1");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Generate clips" }).click();
  await expect(page.getByTestId("project-status")).toContainText("awaiting host", { timeout: 90_000 });
  await page.getByTestId("host-section").getByRole("button", { name: "S1 is the host" }).click();
  await expect(page.getByTestId("clip-video").first()).toBeVisible({ timeout: 150_000 });

  // Editor
  await page.getByRole("link", { name: "Edit" }).first().click();
  await expect(page.getByTestId("waveform")).toBeVisible();
  await expect(page.getByTestId("caption-words")).toBeVisible();
  await expect(page.getByTestId("broll-review").or(page.getByText("No B-roll slots"))).toBeVisible();
  await expect(page.getByTestId("copy-pack")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByLabel("Instagram tags", { exact: true })).toHaveValue(/^#DoubleCoverage/);

  // Export: with test doubles the pack is labelled UNVERIFIED-TEST, never "verified".
  await page.getByRole("button", { name: "Export pack" }).click();
  await page.reload();
  await expect(page.getByTestId("download-export")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("export-section")).toContainText("UNVERIFIED-TEST");
  if (process.env.REPORT_DIR) {
    await page.screenshot({ path: `${process.env.REPORT_DIR}/editor-e2e.png`, fullPage: true });
  }
});
