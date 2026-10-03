import { test, expect } from "@playwright/test";

test("login, real SSE, charts, configuration and logout", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill("fixture-only-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#stream-health")).toHaveText("Dashboard connected");
  await expect(page.locator("#demand")).toHaveText("4");
  await expect(page.locator("#thresholds tr")).toHaveCount(3);
  await expect(page.locator("#chart canvas").first()).toBeVisible();
  await expect(page.locator("#peak")).toHaveText("4 kW");
  await expect(page.locator("#key-status")).toContainText("API key saved");
  expect(await page.content()).not.toContain("fixture-only-key");
  await page.getByLabel("Polling interval (seconds)").fill("60");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.locator("#test-result")).toHaveText("Settings saved.");
  await page.getByRole("button", { name: "7 days", exact: true }).click();
  await expect(page.locator("#analysis-status")).toContainText("Chart buckets: 15 min");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("Dashboard password")).toBeVisible();
  expect(errors).toEqual([]);
});

test("protected endpoints, origin checks and missing data presentation", async ({ page }) => {
  const response = await page.request.get("/api/config");
  expect(response.status()).toBe(401);
  const denied = await page.request.post("/api/auth/login", { data: { password: "fixture-only-password" } });
  expect(denied.status()).toBe(403);
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill("fixture-only-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#thresholds tr")).toHaveCount(3);
  await page.locator("#from").fill("2025-01-01T00:00");
  await page.locator("#to").fill("2025-01-02T00:00");
  await page.getByRole("button", { name: "Apply custom range" }).click();
  await expect(page.locator("#coverage")).toHaveText("0%");
  await expect(page.locator("#energy")).toContainText("Unavailable");
});
