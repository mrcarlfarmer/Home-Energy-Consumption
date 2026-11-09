import { expect, test } from "@playwright/test";
import type { DailyPeaks } from "../src/api";

test.use({ baseURL: "http://127.0.0.1:8769" });

test("tariff settings fetch prices and estimate both grid and EV-excluded threshold costs", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#above-6kw-load-cost")).toHaveText("Pricing disabled");
  await expect(page.locator("#days tr")).toHaveCount(5);
  for (const row of await page.locator("#days tr").all()) {
    await expect(row.locator("td")).toHaveCount(4);
    await expect(row.locator("td").nth(3)).toHaveText("Pricing disabled");
  }
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.locator("#tariff-status")).toContainText("No tariff pricing fetched");
  const before = await (await page.request.get("/api/config")).json();
  await page.getByRole("button", { name: "Fetch Octopus pricing", exact: true }).click();
  await expect(page.locator("#tariff-status")).toContainText("Fixture Octopus tariff");
  await expect(page.locator("#tariff-rates")).toContainText("30 p/kWh");
  expect((await (await page.request.get("/api/config")).json()).revision).toBe(before.revision);
  expect(await page.content()).not.toContain("fixture-only-key");
  await page.getByLabel("Estimate costs using Octopus tariff pricing").check();
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#test-result")).toHaveText("Settings saved.");
  const gridDailyResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/daily-peaks");
  await page.getByRole("link", { name: "Overview", exact: true }).click();
  await expect(page.locator("#above-6kw-load-cost")).toHaveText("£4.80");
  await expect(page.locator("#above-6kw-excess-cost")).toHaveText("£1.20");
  await expect(page.locator("#thresholds")).toContainText("£4.80");
  await expect(page.locator("#pricing-note")).toContainText("including VAT");
  await expect(page.locator("#pricing-note")).toContainText("does not itself incur an extra Octopus charge");
  const gridDaily: DailyPeaks = await (await gridDailyResponse).json();
  const gbp = new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" });
  const checkDaily = async (daily: DailyPeaks, excess: number) => {
    let totalExcess = 0;
    for (const [index, day] of [...daily.daily_peaks].reverse().entries()) {
      const row = page.locator("#days tr").nth(index);
      await expect(row.locator("td").nth(0)).toHaveText(day.date);
      await expect(row.locator("td")).toHaveCount(4);
      const cost = day.pricing.excess_energy_gbp;
      await expect(row.locator("td").nth(3)).toHaveText(cost === null ? "Unavailable" : gbp.format(cost));
      totalExcess += day.pricing.excess_energy_gbp ?? 0;
    }
    expect(totalExcess).toBeCloseTo(excess, 8);
  };
  await checkDaily(gridDaily, 1.2);
  await expect(page.locator("#days-status")).toContainText("Costs cover observed energy only");
  const evDailyResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/daily-peaks");
  await page.getByRole("switch", { name: "Exclude EV charging", exact: true }).check();
  await expect(page.locator("#above-6kw-load-cost")).toHaveText("£1.16");
  await expect(page.locator("#above-6kw-excess-cost")).toHaveText("£0.29");
  await checkDaily(await (await evDailyResponse).json(), 0.29);
  await page.route("**/api/analytics/summary?**", async route => {
    const response = await route.fetch();
    const summary = await response.json();
    summary.pricing.available = false;
    summary.pricing.energy_when_above_gbp = null;
    summary.pricing.excess_energy_gbp = null;
    summary.pricing.price_coverage_pct = 50;
    summary.pricing.warning = "Some applicable tariff prices are missing.";
    await route.fulfill({ response, json: summary });
  });
  await page.getByRole("button", { name: "7 days", exact: true }).click();
  await expect(page.locator("#above-6kw-load-cost")).toHaveText("Unavailable");
  await expect(page.locator("#above-6kw-excess-cost")).toHaveText("Unavailable");
  await expect(page.locator("#pricing-note")).toContainText("50%");
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Fetch Octopus pricing", exact: true }).click();
  await expect(page.locator("#tariff-status")).toContainText("throttled");
  await expect(page.getByRole("button", { name: "Fetch Octopus pricing", exact: true })).toBeEnabled();
  expect(errors).toEqual([]);
});
