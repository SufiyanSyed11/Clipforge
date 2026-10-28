import { expect, test } from "@playwright/test";

// P0-4: a ping job created from the UI goes API -> queue -> worker and its live log,
// final status and QA checklist arrive back in the browser.
test("ping job runs end to end with live log and QA checklist", async ({ page }) => {
  await page.goto("/system");
  await expect(page.getByTestId("api-health")).toContainText("API online");
  await page.getByRole("button", { name: "Run ping job" }).click();
  const log = page.getByTestId("job-log");
  await expect(log).toContainText("Queued ping job");
  await expect(log).toContainText("ping step 1/3");
  await expect(log).toContainText("ping step 3/3");
  await expect(page.getByTestId("job-status")).toHaveText("done");
  const qa = page.getByTestId("qa-checklist");
  await expect(qa).toContainText("QA passed");
  await expect(qa).toContainText("worker received the job");
  if (process.env.REPORT_DIR) {
    await page.screenshot({ path: `${process.env.REPORT_DIR}/phase0-ping-e2e.png`, fullPage: true });
  }
});
