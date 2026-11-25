import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8766" });

test("collection history displays recorded events and refreshes", async ({ page }) => {
  await page.route("**/api/diagnostics*", route => route.fulfill({
    json: {
      events: [{ id: 2, at: "2026-11-25T21:00:00Z", level: "WARNING", source: "app.poller", message: "Collection failed: code=rate_limited" }],
      dropped: 1,
    },
  }));
  await page.goto("/");
  await page.getByRole("link", { name: "Collection history" }).click();
  await expect(page.getByRole("heading", { name: "Collection history" })).toBeVisible();
  await expect(page.locator("#diagnostics-events")).toContainText("rate_limited");
  await expect(page.locator("#diagnostics-status")).toContainText("1 events could not be recorded");
  await expect(page.getByRole("button", { name: "Load older" })).toBeDisabled();
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.locator("#diagnostics-events tr")).toHaveCount(1);
  await page.getByRole("link", { name: "Overview", exact: true }).click();
  await expect(page.locator("#overview")).toBeVisible();
});
