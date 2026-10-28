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
  // Synthetic-speech fixture the FAKE_MODELS test doubles understand (no real podcast).
  const python = process.env.PYTHON ?? "python";
  execFileSync(python, ["-m", "clipforge.testing.synth", video, "--seconds", "75"]);
});

test("upload → rights gate → host confirm → ranked clips", async ({ page, request }) => {
  // Small renders keep the test fast; the pipeline is the same at 1080x1920.
  const api = process.env.API_URL ?? "http://127.0.0.1:8000";
  const cfg = await (await request.get(`${api}/api/settings`)).json();
  cfg.style.width = 360;
  cfg.style.height = 640;
  expect((await request.put(`${api}/api/settings`, { data: cfg })).ok()).toBeTruthy();

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
  // Every clip is rendered; with test doubles the gate honestly says "needs review".
  await expect(page.getByTestId("clip-video").first()).toBeVisible({ timeout: 120_000 });
  await expect(page.getByTestId("clip-status").first()).toContainText("needs review");
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
