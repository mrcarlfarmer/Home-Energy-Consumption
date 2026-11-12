import { expect, test } from "@playwright/test";
import type { DayAnalysis } from "../src/api";

test.use({ baseURL: "http://127.0.0.1:8767" });

test("daily date links open real calendar analysis, all three views and preserve overview state", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#days a")).toHaveCount(5);
  await page.getByRole("switch", { name: "Auto-refresh graph", exact: true }).uncheck();
  const from = await page.locator("#from").inputValue(), to = await page.locator("#to").inputValue();
  const link = page.locator("#days a").first();
  const date = await link.textContent();
  expect(await link.getAttribute("href")).toContain("timezone=Europe%2FLondon");
  const response = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/day");
  await link.click();
  const day: DayAnalysis = await (await response).json();
  await expect(page.locator("#daily-page")).toBeVisible();
  await expect(page.locator("#overview")).toBeHidden();
  await expect(page.locator("#day-date")).toHaveValue(date!);
  await expect(page.locator("#day-chart canvas").first()).toBeVisible();
  await expect(page.locator("#day-chart .u-legend")).toContainText("Sampled peak (W)");
  await expect(page.locator("#day-chart .u-legend")).not.toContainText("Observed mean");
  await expect(page.locator(".day-metrics")).toContainText(`${day.grid.sampled_peak_w! / 1000} kW`);
  await expect(page.locator(".day-notice").first()).toContainText("observed");

  await page.getByRole("button", { name: "Peak window", exact: true }).click();
  await page.getByRole("link", { name: "EV & tariff", exact: true }).click();
  await expect(page.locator("#daily-page")).toContainText("Charging evidence & applicable household rates");
  await expect(page.locator("#daily-page .table-scroll table")).toContainText("Recorded EV charging");
  await expect(page.locator("#daily-page")).toContainText("Bonus-rate assumption not active for this meter");
  const adjustedResponse = page.waitForResponse(response => response.url().includes("/api/analytics/day?") &&
    response.url().includes("exclude_ev=true"));
  await page.getByRole("button", { name: "Exclude EV", exact: true }).click();
  const adjusted: DayAnalysis = await (await adjustedResponse).json();
  expect(adjusted.adjusted!.observed_seconds).toBeLessThan(adjusted.grid.observed_seconds);
  await expect(page.getByRole("button", { name: "Exclude EV", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("link", { name: "Coverage & events", exact: true }).click();
  await expect(page.locator(".day-health")).toHaveCount(4);
  await expect(page.locator("#daily-page table")).toContainText("EV transition uncertainty");
  await expect(page.locator("#day-recover")).toBeDisabled();
  await expect(page.locator("#daily-page")).toContainText("does not recover Home Assistant history");
  await page.getByRole("button", { name: "Inspect", exact: true }).last().click();
  await expect(page.getByRole("link", { name: "Daily demand", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.locator("#day-chart canvas").first()).toBeVisible();
  await expect(page.locator("#day-chart .u-legend")).toContainText("EV charging (W)");
  await expect(page.locator("#day-chart .u-legend")).toContainText("Grid peak reference (W)");
  await expect(page.locator("#day-chart .u-legend")).not.toContainText("Observed mean");
  await page.getByRole("button", { name: "Peak window", exact: true }).click();
  await expect(page.locator("#from")).toHaveValue(from);
  await expect(page.locator("#to")).toHaveValue(to);
  await expect(page.locator("#daily-page")).toContainText("Derived non-EV peak");
  await page.reload();
  await expect(page.locator("#day-date")).toHaveValue(date!);
  await expect(page.getByRole("button", { name: "Exclude EV", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("link", { name: "Back to overview", exact: false }).click();
  await expect(page.locator("#overview")).toBeVisible();
  expect(errors).toEqual([]);
  // Re-enter without a document reload to check the original range is retained.
  await page.getByRole("switch", { name: "Auto-refresh graph", exact: true }).uncheck();
  const keptFrom = await page.locator("#from").inputValue(), keptTo = await page.locator("#to").inputValue();
  await expect(page.locator("#days a")).toHaveCount(5);
  await page.locator("#days a").first().click();
  await expect(page.locator("#day-chart canvas").first()).toBeVisible();
  await page.getByRole("link", { name: "Back to overview", exact: false }).click();
  await expect(page.locator("#from")).toHaveValue(keptFrom);
  await expect(page.locator("#to")).toHaveValue(keptTo);
  expect(from).not.toBe("");
  expect(to).not.toBe("");
});

test("daily pages navigate dates, show no-data and request failures explicitly, and fit mobile screens", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#days a")).toHaveCount(5);
  await page.locator("#days a").last().click();
  await expect(page.locator("#day-chart canvas").first()).toBeVisible();
  await expect(page.locator(".day-metrics")).toContainText("Unavailable");
  await expect(page.locator(".day-notice").first()).toContainText("0%");
  const before = await page.locator("#day-date").inputValue();
  await page.getByRole("link", { name: "Next calendar day", exact: true }).click();
  await expect(page.locator("#day-date")).not.toHaveValue(before);
  await expect(page.locator("#day-chart canvas").first()).toBeVisible();
  await page.goBack();
  await expect(page.locator("#day-date")).toHaveValue(before);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("link", { name: "EV & tariff", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.route("**/api/analytics/day?**", route => route.fulfill({
    status: 503, json: { detail: "Synthetic daily analysis failure" },
  }));
  await page.getByRole("button", { name: "Refresh this day", exact: true }).click();
  await expect(page.locator("#day-error")).toHaveText("Synthetic daily analysis failure");
  await expect(page.locator("#day-date")).toBeVisible();
  await expect(page.locator("#day-retry")).toBeEnabled();
  await page.unroute("**/api/analytics/day?**");
  await page.locator("#day-retry").click();
  await expect(page.locator("#day-chart canvas").first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("archived daily pages keep grid metrics without inventing charging evidence", async ({ page }) => {
  await page.route("**/api/analytics/day?**", async route => {
    const response = await route.fetch();
    const data: DayAnalysis = await response.json();
    await route.fulfill({ response, json: {
      ...data, raw_available: false, recovery_available: false, exclude_ev: false, adjusted: null,
      ev_unavailable_reason: "EV exclusion is unavailable: retained raw meter/Ohme history has expired",
      coverage: { ...data.coverage, ev_seconds: null, joint_seconds: null, adjusted_seconds: null },
      charging: [], meter_gaps: [], transition_gaps: [], events: [], event_count: 0,
    } });
  });
  await page.goto("/");
  await expect(page.locator("#days a")).toHaveCount(5);
  await page.locator("#days a").first().click();
  await expect(page.locator("#day-chart canvas").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Exclude EV", exact: true })).toBeDisabled();
  await expect(page.locator(".day-metrics")).toContainText("8 kW");
  await page.getByRole("link", { name: "EV & tariff", exact: true }).click();
  await expect(page.locator(".day-metrics article").nth(0)).toContainText("Unavailable");
  await expect(page.locator(".day-metrics article").nth(1)).toContainText("Unavailable");
  await expect(page.locator(".day-metrics article").nth(2)).toContainText("Pricing disabled");
  await page.getByRole("link", { name: "Coverage & events", exact: true }).click();
  await expect(page.locator("#daily-page")).toContainText("Archive gaps and EV activity are not reconstructed");
  await expect(page.locator("#day-recover")).toBeDisabled();
});

test("daily recovery sends snapshot revision and exact day bounds, with explicit failure", async ({ page }) => {
  let day: DayAnalysis;
  await page.route("**/api/analytics/day?**", async route => {
    const response = await route.fetch();
    day = await response.json();
    await route.fulfill({ response, json: { ...day, recovery_available: true } });
  });
  let attempts = 0;
  let hold = false;
  let finishRecovery = (): void => {};
  await page.route("**/api/recovery", async route => {
    const requestedDay = day;
    const body = route.request().postDataJSON();
    expect(body).toEqual({
      expected_revision: day.config_revision, device_id: day.device_id, from: day.from, to: day.to,
    });
    attempts++;
    if (hold) await new Promise<void>(resolve => { finishRecovery = resolve; });
    await route.fulfill(attempts === 1
      ? { status: 409, json: { detail: "Configuration changed; refresh this day before recovering" } }
      : { json: { device_id: requestedDay.device_id, from: requestedDay.from, to: requestedDay.to, state: "pending" } });
  });
  await page.goto("/");
  await expect(page.locator("#days a")).toHaveCount(5);
  await page.locator("#days a").first().click();
  await page.getByRole("link", { name: "Coverage & events", exact: true }).click();
  await page.locator("#day-recover").click();
  await expect(page.locator("#day-recovery-status")).toContainText("Recovery request failed: Configuration changed");
  await expect(page.locator("#day-recover")).toBeEnabled();
  await page.locator("#day-recover").click();
  await expect(page.locator("#day-recovery-status")).toContainText("Recovery queued:");
  expect(attempts).toBe(2);
  hold = true;
  await page.locator("#day-recover").click();
  await expect.poll(() => attempts).toBe(3);
  await page.getByRole("link", { name: "Previous calendar day", exact: true }).click();
  await expect(page.locator("#day-recover")).toBeDisabled();
  finishRecovery();
  await expect(page.locator("#day-recover")).toBeEnabled();
  await expect(page.locator("#day-recovery-status")).toBeEmpty();
});
