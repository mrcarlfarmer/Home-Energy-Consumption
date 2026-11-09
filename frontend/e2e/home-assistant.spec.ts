import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8767" });

test("Home Assistant settings and real EV-excluded analytics remain separate from grid import", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#peak")).toHaveText("8 kW");
  await expect(page.locator("#energy")).toHaveText("16 kWh");
  await expect(page.locator("#above-6kw-time")).toHaveText("2 h");
  await expect(page.locator("#home-assistant-health")).toContainText("running");

  const adjustedHistory = page.waitForResponse(response =>
    response.url().includes("/api/analytics/history?") && response.url().includes("exclude_ev=true"),
  );
  await page.getByRole("switch", { name: "Exclude EV charging", exact: true }).check();
  await expect(page.locator("#energy-label")).toHaveText("Estimated non-EV import");
  await expect(page.locator("#energy")).toHaveText("5.35 kWh");
  await expect(page.locator("#above-6kw-time")).toHaveText("29 min");
  await expect(page.locator("#above-6kw-energy")).toHaveText("3.867 kWh");
  await expect(page.locator("#above-6kw-excess")).toHaveText("0.967 kWh");
  await expect(page.locator("#coverage")).toHaveText("8.19%");
  await expect(page.locator("#coverage-hint")).toHaveText("Joint meter / Ohme, minus transition gaps");
  await expect(page.locator("#analysis-basis-note")).toContainText("minute before and after recorded charging starts/stops is unknown");
  const history = await (await adjustedHistory).json();
  expect(history.warnings.some((warning: string) => warning.includes("60 seconds before and after"))).toBe(true);
  expect(history.series.observed_seconds.reduce((total: number, seconds: number) => total + seconds, 0)).toBe(7080);
  const peaks: (number | null)[] = history.series.sampled_peak_w;
  expect(peaks.some((peak, index) => peak === null &&
    peaks.slice(0, index).includes(1000) && peaks.slice(index + 1).includes(8000))).toBe(true);
  await expect(page.locator("#above-6kw-hint")).toContainText("excluding EV");
  await expect(page.locator("#days-label")).toHaveText("Daily derived non-EV peaks");
  await expect(page.locator("#thresholds tr").last()).toContainText("3.867 kWh");
  await expect(page.locator("#thresholds tr")).toHaveCount(1);
  await expect(page.locator("#thresholds tr").first()).toContainText("6 kW");
  await expect(page.locator("#chart canvas").first()).toBeVisible();

  await page.getByRole("switch", { name: "Exclude EV charging", exact: true }).uncheck();
  await expect(page.locator("#energy")).toHaveText("16 kWh");
  await expect(page.locator("#above-6kw-time")).toHaveText("2 h");
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Home Assistant URL", { exact: true })).toHaveValue("http://homeassistant.fixture:8123");
  await expect(page.getByLabel("Ohme power entity", { exact: true })).toHaveValue("sensor.ohme_home_pro_power");
  await expect(page.locator("#ha-token-status")).toContainText("Token saved");
  expect(await page.content()).not.toContain("fixture-home-assistant-token");
  const before = await (await page.request.get("/api/config")).json();
  await page.getByRole("button", { name: "Test Home Assistant", exact: true }).click();
  await expect(page.locator("#ha-test-result")).toContainText("Connected. Power: 0 W");
  await expect(page.locator("#ha-test-result")).toContainText("Recent history: available");
  const after = await (await page.request.get("/api/config")).json();
  expect(after.revision).toBe(before.revision);
  expect(JSON.stringify(after)).not.toContain("fixture-home-assistant-token");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#test-result")).toHaveText("Settings saved.");
  expect((await (await page.request.get("/api/config")).json()).has_home_assistant_token).toBe(true);

  await page.getByRole("link", { name: "Overview", exact: true }).click();
  await page.getByRole("switch", { name: "Exclude EV charging", exact: true }).check();
  await expect(page.locator("#above-6kw-time")).toHaveText("29 min");
  await page.route("**/api/analytics/summary?**", async route => {
    await route.fulfill({ status: 503, json: { detail: "Synthetic analysis failure" } });
  });
  await page.getByRole("button", { name: "7 days", exact: true }).click();
  await expect(page.locator("#message")).toHaveText("Synthetic analysis failure");
  await expect(page.locator("#above-6kw-time")).toHaveText("--");
  await expect(page.locator("#thresholds tr")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("Home Assistant freshness permission failures explain how to recover", async ({ page }) => {
  const snapshot = {
    device_id: null,
    data_version: 1,
    reading: null,
    health: { polling_state: "disabled", telemetry_state: "missing", storage_state: "ok" },
    home_assistant: {
      state: "backoff",
      power_w: null,
      reported_at: null,
      error_code: "home_assistant_freshness_access_denied",
      recovery_cursor_at: null,
    },
  };
  await page.route("**/api/telemetry/live", route => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: `event: snapshot\ndata: ${JSON.stringify(snapshot)}\n\n`,
  }));
  await page.goto("/");
  await expect(page.locator("#home-assistant-health")).toContainText(
    "verify the Home Assistant token and administrator access, then save settings",
  );
});
