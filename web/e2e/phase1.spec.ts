import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

// P1-1, P1-5, P1-8 in the browser. Needs the stack running with FAKE_MODELS=true: whisper,
// diarization and the clip picker are stubs, so this proves the UI and pipeline plumbing only.
// Real transcription and picking are checked by `clipforge-golden run` on the owner's PC.

const fixtureDir = path.join(__dirname, "..", "test-results", "fixtures");
const video = path.join(fixtureDir, "episode.mp4");

test.beforeAll(() => {
  mkdirSync(fixtureDir, { recursive: true });
  execFileSync("ffmpeg", [
    "-y", "-v", "error",
    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=75",
    "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=75",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", video,
  ]);
});

test("upload → rights gate → host confirm → ranked clips", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Upload file" }).click();
  await page.getByLabel("Episode file").setInputFiles(video);
  await page.getByLabel("Project name").fill("E2E episode");
  await page.getByLabel("Clip count").fill("3");

  const generate = page.getByRole("button", { name: "Generate clips" });
  await expect(generate).toBeDisabled(); // rights not confirmed yet
  await page.getByRole("checkbox").check();
  await expect(generate).toBeEnabled();
  await generate.click();

  await expect(page).toHaveURL(/\/projects\/\d+$/);
  await expect(page.getByTestId("project-status")).toContainText("awaiting host", { timeout: 60_000 });
  await expect(page.getByTestId("job-log")).toContainText("Suggested host: S1");
  const hostSection = page.getByTestId("host-section");
  await expect(hostSection).toContainText("S1 (suggested)");

  await hostSection.getByRole("button", { name: "S1 is the host" }).click();
  await expect(page.getByTestId("project-status")).toContainText("clips ready", { timeout: 60_000 });
  const cards = page.getByTestId("clip-card");
  await expect(cards.first()).toBeVisible();
  expect(await cards.count()).toBeLessThanOrEqual(3);
  await expect(cards.first()).toContainText("Virality");
  await expect(cards.first()).toContainText("Hook:");
  await expect(cards.first().getByTestId("clip-qa")).toContainText("30 s hard max");
  // The stub run must say it isn't a real result.
  await expect(page.getByTestId("qa-checklist").first()).toContainText("real clip picking");
  if (process.env.REPORT_DIR) {
    await page.screenshot({ path: `${process.env.REPORT_DIR}/phase1-clips-e2e.png`, fullPage: true });
  }

  // Changing the host is one click.
  await hostSection.getByRole("button", { name: "Make S2 the host" }).click();
  await expect(hostSection).toContainText("S2 · HOST", { timeout: 60_000 });
  await expect(page.getByTestId("project-status")).toContainText("clips ready", { timeout: 60_000 });
});

test("URL project without rights is refused by the API", async ({ request }) => {
  const api = process.env.API_URL ?? "http://127.0.0.1:8000";
  const r = await request.post(`${api}/api/projects`, {
    multipart: { url: "https://www.youtube.com/watch?v=x", rights_confirmed: "false" },
  });
  expect(r.status()).toBe(422);
});
