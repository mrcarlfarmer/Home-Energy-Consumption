import { test, expect } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8766" });

test("opens directly without cookies, keeps settings and streams without login", async ({ page, context }) => {
  const errors: string[] = [];
  const loginRequests: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    if (request.url().endsWith("/api/auth/login")) loginRequests.push(request.url());
  });
  await page.goto("/");
  await expect(page.locator("#stream-health")).toHaveText("Dashboard connected");
  await expect(page.locator("#demand")).toHaveText("4");
  await expect(page.locator("#thresholds tr")).toHaveCount(3);
  await expect(page.locator(".access-warning")).toContainText("anyone who can reach");
  await expect(page.getByLabel("Dashboard password")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
  expect(await page.content()).not.toContain("fixture-only-key");
  await page.getByLabel("Check every (seconds)").fill("80");
  await page.getByRole("button", { name: "Save interval", exact: true }).click();
  await expect(page.getByLabel("Polling interval (seconds)", { exact: true })).toHaveValue("80");
  await page.getByRole("button", { name: "Start polling", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop polling", exact: true })).toBeEnabled();
  await page.reload();
  await expect(page.locator("#stream-health")).toHaveText("Dashboard connected");
  await expect(page.getByLabel("Check every (seconds)")).toHaveValue("80");
  await expect(page.getByRole("button", { name: "Stop polling", exact: true })).toBeEnabled();
  expect(await context.cookies()).toHaveLength(0);
  const state = await (await page.request.get("/api/auth/session")).json();
  expect(state.authentication_required).toBe(false);
  expect(state.authenticated).toBe(false);
  const config = await (await page.request.get("/api/config")).json();
  const missingCsrf = await page.request.post("/api/config", {
    headers: { Origin: "http://127.0.0.1:8766" },
    data: { expected_revision: config.revision, poll_interval_seconds: 90 },
  });
  expect(missingCsrf.status()).toBe(403);
  const wrongOrigin = await page.request.post("/api/config", {
    headers: { Origin: "http://untrusted.test", "X-CSRF-Token": state.csrf_token },
    data: { expected_revision: config.revision, poll_interval_seconds: 90 },
  });
  expect(wrongOrigin.status()).toBe(403);
  expect(loginRequests).toEqual([]);
  expect(errors).toEqual([]);
});

test("a startup failure offers retry rather than an unnecessary login", async ({ page }) => {
  await page.route("**/api/config", route => route.fulfill({
    status: 503, contentType: "application/problem+json",
    body: JSON.stringify({ detail: "Storage temporarily unavailable" }),
  }));
  await page.goto("/");
  await expect(page.getByRole("alert")).toHaveText("Storage temporarily unavailable");
  await expect(page.getByLabel("Dashboard password")).toHaveCount(0);
  await page.unroute("**/api/config");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.locator("#stream-health")).toHaveText("Dashboard connected");
});
