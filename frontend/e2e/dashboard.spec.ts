import { test, expect } from "@playwright/test";

test("login, real SSE, charts, configuration and logout", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByText("HTTP connection:", { exact: false })).toBeVisible();
  await page.getByLabel("Dashboard password").fill("fixture-only-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#stream-health")).toHaveText("Dashboard connected");
  await expect(page.locator(".transport-warning")).toContainText("not encrypted in transit");
  await expect(page.locator("#demand")).toHaveText("4");
  await expect(page.locator("#thresholds tr")).toHaveCount(3);
  await expect(page.locator("#chart canvas").first()).toBeVisible();
  await expect(page.locator("#peak")).toHaveText("4 kW");
  await expect(page.locator("#key-status")).toContainText("API key saved");
  expect(await page.content()).not.toContain("fixture-only-key");
  await page.getByLabel("Polling interval (seconds)").fill("60");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.locator("#test-result")).toHaveText("Settings saved.");
  await expect(page.getByLabel("Check every (seconds)")).toHaveValue("60");
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

test("live polling controls persist and synchronize without saving credential drafts", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill("fixture-only-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Start polling", exact: true })).toBeEnabled();
  await expect(page.locator("#polling-status")).toContainText("Polling disabled");
  await page.getByLabel("Replace Octopus API key").fill("unsaved-fixture-key");
  await page.getByLabel("Octopus account number").fill("A-UNSAVED");
  await page.getByLabel("Check every (seconds)").fill("75");
  await page.getByRole("button", { name: "Save interval", exact: true }).click();
  await expect(page.getByLabel("Polling interval (seconds)", { exact: true })).toHaveValue("75");
  await expect(page.getByRole("button", { name: "Start polling", exact: true })).toBeEnabled();
  let saved = await (await page.request.get("/api/config")).json();
  expect(saved.poll_interval_seconds).toBe(75);
  expect(saved.polling_enabled).toBe(false);
  expect(saved.account_number).toBe("A-FIXTURE");

  await page.getByRole("button", { name: "Start polling", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop polling", exact: true })).toBeEnabled();
  await expect(page.getByLabel("Enable polling", { exact: true })).toBeChecked();
  await expect(page.locator("#polling-status")).toContainText("Scheduled interval: 75 seconds");
  await expect(page.getByLabel("Replace Octopus API key")).toHaveValue("unsaved-fixture-key");
  await expect(page.getByLabel("Octopus account number")).toHaveValue("A-UNSAVED");
  saved = await (await page.request.get("/api/config")).json();
  expect(saved.polling_enabled).toBe(true);
  expect(saved.account_number).toBe("A-FIXTURE");

  await page.reload();
  await expect(page.getByRole("button", { name: "Stop polling", exact: true })).toBeEnabled();
  await expect(page.getByLabel("Check every (seconds)")).toHaveValue("75");
  for (const interval of ["30", "3600", "75"]) {
    await page.getByLabel("Check every (seconds)").fill(interval);
    await page.getByRole("button", { name: "Save interval", exact: true }).click();
    await expect(page.getByLabel("Polling interval (seconds)", { exact: true })).toHaveValue(interval);
    saved = await (await page.request.get("/api/config")).json();
    expect(saved.poll_interval_seconds).toBe(Number(interval));
  }
  const writes: string[] = [];
  page.on("request", request => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/config") writes.push(request.url());
  });
  for (const invalid of ["29", "3601", "45.5", ""]) {
    await page.getByLabel("Check every (seconds)").fill(invalid);
    await page.getByRole("button", { name: "Save interval", exact: true }).click();
    await expect(page.locator("#live-interval:invalid")).toHaveCount(1);
  }
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Stop polling", exact: true }).click();
  await expect(page.getByRole("button", { name: "Start polling", exact: true })).toBeEnabled();
  await expect(page.getByLabel("Enable polling", { exact: true })).not.toBeChecked();
  saved = await (await page.request.get("/api/config")).json();
  expect(saved.polling_enabled).toBe(false);
  expect(saved.poll_interval_seconds).toBe(75);
});

test("polling errors leave the saved state unchanged and controls usable", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill("fixture-only-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Start polling", exact: true })).toBeEnabled();
  await page.route("**/api/config", async route => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 503, json: { detail: "Synthetic configuration failure" } });
    } else {
      await route.continue();
    }
  });
  await page.getByRole("button", { name: "Start polling", exact: true }).click();
  await expect(page.locator("#message")).toHaveText("Synthetic configuration failure");
  await expect(page.getByRole("button", { name: "Start polling", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Save interval", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Save settings", exact: true })).toBeEnabled();
  await expect(page.getByLabel("Enable polling", { exact: true })).not.toBeChecked();
});

test("short chart presets request exact windows ending now", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill("fixture-only-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#thresholds tr")).toHaveCount(3);
  for (const [label, minutes] of [["1 hour", 60], ["30 min", 30], ["15 min", 15], ["5 min", 5]] as const) {
    const response = page.waitForResponse(value => {
      const url = new URL(value.url());
      if (url.pathname !== "/api/analytics/history") return false;
      return new Date(url.searchParams.get("to")!).getTime() - new Date(url.searchParams.get("from")!).getTime() === minutes * 60000;
    });
    const before = Date.now();
    await page.getByRole("button", { name: label, exact: true }).click();
    const history = await response;
    expect(history.ok()).toBe(true);
    const url = new URL(history.url());
    const end = new Date(url.searchParams.get("to")!).getTime();
    expect(end).toBeGreaterThanOrEqual(before - 1000);
    expect(end).toBeLessThanOrEqual(Date.now());
    expect((await history.json()).interval_seconds).toBe(60);
    await expect(page.locator("#analysis-status")).toContainText("Chart buckets: 1 min");
    await expect(page.locator("#chart canvas").first()).toBeVisible();
  }
});
