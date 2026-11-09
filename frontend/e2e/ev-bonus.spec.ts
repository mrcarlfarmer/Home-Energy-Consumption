import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8769" });

test("bonus rates, charging evidence and costs stay visible in both EV bases", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const revision = "fixture:bonus:1";
  await page.route("**/api/analytics/summary?**", async route => {
    const response = await route.fetch();
    const summary = await response.json();
    summary.pricing = {
      ...summary.pricing, enabled: true, available: true, energy_when_above_gbp: 0.32,
      excess_energy_gbp: 0.08, price_coverage_pct: 100, pricing_revision: revision,
      warning: "Estimated bonus household slots assume Prioritise Savings.",
    };
    await route.fulfill({ response, json: summary });
  });
  await page.route("**/api/analytics/daily-peaks?**", async route => {
    const response = await route.fetch();
    const peaks = await response.json();
    for (const day of peaks.daily_peaks) {
      day.pricing = {
        ...day.pricing, enabled: true, available: true, excess_energy_gbp: 0.08,
        pricing_revision: revision,
      };
    }
    await route.fulfill({ response, json: peaks });
  });
  await page.route("**/api/tariff?**", async route => {
    const response = await route.fetch();
    const status = await response.json();
    const query = new URL(route.request().url()).searchParams;
    const from = query.get("from")!;
    const start = new Date(from).getTime() + 3600000;
    const at = (offset: number) => new Date(start + offset * 60000).toISOString();
    Object.assign(status, {
      rates: [{ from: at(0), to: at(30), pence_per_kwh: 8, band: "EV_BONUS" }],
      charging: [{ from: at(7), to: at(17) }],
      unresolved: [{ from: at(30), to: at(60) }],
      adjustment_enabled: true, ev_coverage_pct: 50, pricing_revision: revision,
      warnings: ["Estimated from Ohme power; not supplier-confirmed billing slots."],
    });
    await route.fulfill({ response, json: status });
  });
  await page.goto("/");
  await expect(page.locator("#bonus-key")).toBeVisible();
  await expect(page.locator("#charging-key")).toBeVisible();
  await expect(page.locator("#unresolved-key")).toBeVisible();
  await expect(page.locator("#off-peak-key")).toBeHidden();
  await expect(page.locator("#above-6kw-excess-cost")).toHaveText("£0.08");
  await expect(page.locator("#thresholds")).toContainText("£0.08");
  await expect(page.locator("#days tr").first()).toContainText("£0.08");
  await page.locator("#charging-details summary").click();
  await expect(page.locator("#charging-periods")).toContainText("Observed EV charging");
  await expect(page.locator("#charging-periods")).toContainText("Estimated bonus household slot");
  await expect(page.locator("#charging-periods")).toContainText("8 p/kWh");
  await expect(page.locator("#charging-periods")).toContainText("Unresolved household rate");
  await expect(page.locator("#charging-details-note")).toContainText("50%");
  const adjustedTariff = page.waitForResponse(response => response.url().includes("/api/tariff?"));
  await page.getByRole("switch", { name: "Exclude EV charging", exact: true }).check();
  await adjustedTariff;
  await expect(page.locator("#bonus-key")).toBeVisible();
  await expect(page.locator("#above-6kw-excess-cost")).toHaveText("£0.08");
  await page.unrouteAll({ behavior: "wait" });
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByLabel("Prioritise Savings start date").fill("2026-10-01");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#test-result")).toHaveText("Settings saved.");
  await expect(page.locator("#analysis-status")).not.toHaveText("Calculating observed demand...");
  expect((await (await page.request.get("/api/config")).json()).ev_savings_from).toBe("2026-10-01");
  await page.reload();
  await expect(page.getByLabel("Prioritise Savings start date")).toHaveValue("2026-10-01");
  await page.getByLabel("Prioritise Savings start date").fill("");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#test-result")).toHaveText("Settings saved.");
  expect(errors).toEqual([]);
});

test("automatic refresh retries mismatched evidence without mixing costs and shading", async ({ page }) => {
  await page.addInitScript(() => {
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        window.addEventListener("fixture-snapshot", event => {
          this.dispatchEvent(new MessageEvent("snapshot", { data: (event as CustomEvent<string>).detail }));
        });
      }
    };
  });
  let tariffRequests = 0;
  let staleNext = false;
  await page.route("**/api/analytics/summary?**", async route => {
    const response = await route.fetch();
    const summary = await response.json();
    summary.pricing = {
      ...summary.pricing, enabled: true, available: true, excess_energy_gbp: 0.08,
      pricing_revision: "stable",
    };
    await route.fulfill({ response, json: summary });
  });
  await page.route("**/api/tariff?**", async route => {
    const response = await route.fetch();
    const status = await response.json();
    tariffRequests++;
    status.adjustment_enabled = true;
    status.pricing_revision = staleNext ? "outdated" : "stable";
    staleNext = false;
    await route.fulfill({ response, json: status });
  });
  await page.goto("/");
  await expect(page.locator("#above-6kw-excess-cost")).toHaveText("£0.08");
  const before = tariffRequests;
  const config = await (await page.request.get("/api/config")).json();
  await page.clock.setFixedTime(new Date(await page.evaluate(() => Date.now()) + 61000));
  staleNext = true;
  await page.evaluate(value => window.dispatchEvent(new CustomEvent("fixture-snapshot", {
    detail: JSON.stringify(value),
  })), {
    device_id: config.active_device_id, data_version: 10001,
    reading: { read_at: new Date().toISOString(), demand_w: 5000 },
    health: { polling_state: "running", telemetry_state: "fresh", storage_state: "ok", recovery_pending: false },
  });
  await expect.poll(() => tariffRequests).toBe(before + 2);
  await expect(page.locator("#analysis-status")).toContainText("observed hours");
  await expect(page.locator("#above-6kw-excess-cost")).toHaveText("£0.08");
  await page.unrouteAll({ behavior: "wait" });
});
