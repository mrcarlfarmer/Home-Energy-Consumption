import { expect, test, type Page } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8767" });

async function injectSnapshots(page: Page): Promise<void> {
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
}

test("dragging pins the selected dates across refreshes and EV-basis changes", async ({ page }) => {
  await injectSnapshots(page);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#days tr")).toHaveCount(5);
  await page.getByRole("button", { name: "1 hour", exact: true }).click();
  await expect(page.locator("#chart .u-over")).toBeVisible();
  const initialFrom = new Date(await page.locator("#from").inputValue()).getTime();
  const initialTo = new Date(await page.locator("#to").inputValue()).getTime();
  const drag = async (left: number, right: number) => {
    await page.locator("#chart .u-over").scrollIntoViewIfNeeded();
    const box = (await page.locator("#chart .u-over").boundingBox())!;
    const loaded = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/history");
    await page.mouse.move(box.x + box.width * left, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * right, box.y + box.height / 2, { steps: 10 });
    await page.mouse.up();
    const url = new URL((await loaded).url());
    await expect(page.locator("#chart .u-over")).toBeVisible();
    return [url.searchParams.get("from")!, url.searchParams.get("to")!];
  };
  const selected = await drag(0.25, 0.75);
  const selectedFrom = new Date(selected[0]).getTime(), selectedTo = new Date(selected[1]).getTime();
  expect(selectedFrom).toBeGreaterThan(initialFrom);
  expect(selectedTo).toBeLessThan(initialTo);
  expect(selectedTo - selectedFrom).toBeCloseTo((initialTo - initialFrom) / 2, -4);
  await expect(page.locator("#window-mode")).toHaveText("Fixed window");
  await expect(page.locator('[data-minutes][aria-pressed="true"]')).toHaveCount(0);
  expect(new Date(await page.locator("#from").inputValue()).toISOString()).toBe(selected[0]);
  expect(new Date(await page.locator("#to").inputValue()).toISOString()).toBe(selected[1]);

  const expectWindow = async (action: () => Promise<unknown>, dates = selected) => {
    const loaded = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/history");
    await action();
    const url = new URL((await loaded).url());
    expect([url.searchParams.get("from"), url.searchParams.get("to")]).toEqual(dates);
    await expect(page.locator("#chart .u-over")).toBeVisible();
    return url;
  };
  const evURL = await expectWindow(() => page.getByRole("switch", { name: "Exclude EV charging", exact: true }).check());
  expect(evURL.searchParams.get("exclude_ev")).toBe("true");
  await expectWindow(() => page.getByRole("button", { name: "Refresh graph", exact: true }).click());
  const config = await (await page.request.get("/api/config")).json();
  await page.clock.setFixedTime(new Date(await page.evaluate(() => Date.now()) + 61000));
  await expectWindow(() => page.evaluate(device_id => window.dispatchEvent(new CustomEvent("fixture-snapshot", {
    detail: JSON.stringify({
      device_id, data_version: 10001,
      reading: { read_at: new Date().toISOString(), demand_w: 5000 },
      health: { polling_state: "running", telemetry_state: "fresh", storage_state: "ok", recovery_pending: false },
    }),
  })), config.active_device_id));
  await expect(page.locator("#window-mode")).toHaveText("Fixed window");

  await page.getByRole("switch", { name: "Auto-refresh graph", exact: true }).uncheck();
  const nested = await drag(0.2, 0.8);
  expect(new Date(nested[0]).getTime()).toBeGreaterThan(selectedFrom);
  expect(new Date(nested[1]).getTime()).toBeLessThan(selectedTo);
  await expectWindow(() => page.getByRole("switch", { name: "Auto-refresh graph", exact: true }).check(), nested);
  const shifted = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/history");
  await page.getByRole("button", { name: "Previous time window", exact: true }).click();
  const shiftedURL = new URL((await shifted).url());
  expect(shiftedURL.searchParams.get("to")).toBe(nested[0]);
  expect(new Date(shiftedURL.searchParams.get("from")!).getTime()).toBe(
    2 * new Date(nested[0]).getTime() - new Date(nested[1]).getTime(),
  );
  await page.getByRole("button", { name: "1 hour", exact: true }).click();
  await expect(page.locator("#window-mode")).toHaveText("Latest window");
  expect(errors).toEqual([]);
});

test("EV charging power remains visible in both analysis views", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#chart .u-legend")).toContainText("EV charging (W)");
  const gridResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/history");
  await page.getByRole("button", { name: "1 hour", exact: true }).click();
  await expect(page.locator("#chart .u-legend")).toContainText("EV charging (W)");
  const grid = await (await gridResponse).json();
  expect(grid.series.mean_ev_w).toContain(7000);
  const loaded = page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/history");
  await page.getByRole("switch", { name: "Exclude EV charging", exact: true }).check();
  const nonEV = await (await loaded).json();
  expect(nonEV.series.mean_ev_w).toEqual(grid.series.mean_ev_w);
  expect(nonEV.series.ev_coverage_pct).toEqual(grid.series.ev_coverage_pct);
  expect(nonEV.series.mean_import_w).toContain(1000);
  expect(grid.series.mean_import_w).toContain(8000);
  await expect(page.locator("#chart .u-legend")).toContainText("EV charging (W)");
  await expect(page.locator("#ev-chart-status")).toContainText("both analysis views");
  await page.getByRole("switch", { name: "Exclude EV charging", exact: true }).uncheck();
  await expect(page.locator("#chart .u-legend")).toContainText("EV charging (W)");
  await expect(page.locator("#peak")).toHaveText("8 kW");
});

test("12-hour and custom windows move by their exact width and show five expanded daily peaks", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#days tr")).toHaveCount(5);
  await expect(page.locator("#days")).toBeVisible();
  await expect(page.locator("details #days")).toHaveCount(0);
  await expect(page.locator("#days-status")).toContainText("Five calendar days");
  await expect(page.locator("#chart .u-legend")).not.toContainText("Trailing");
  await expect(page.getByRole("switch", { name: "Exclude EV charging", exact: true })).not.toBeChecked();
  await expect(page.getByRole("button", { name: "Next time window", exact: true })).toBeDisabled();

  const historyRequest = () => page.waitForResponse(response => new URL(response.url()).pathname === "/api/analytics/history");
  const preset = historyRequest();
  await page.getByRole("button", { name: "12 hours", exact: true }).click();
  const url = new URL((await preset).url());
  const start = new Date(url.searchParams.get("from")!).getTime();
  const end = new Date(url.searchParams.get("to")!).getTime();
  expect(end - start).toBe(12 * 3600000);
  await expect(page.locator("#days tr")).toHaveCount(5);
  const previous = historyRequest();
  await page.getByRole("button", { name: "Previous time window", exact: true }).click();
  const previousUrl = new URL((await previous).url());
  expect(new Date(previousUrl.searchParams.get("from")!).getTime()).toBe(start - 12 * 3600000);
  expect(new Date(previousUrl.searchParams.get("to")!).getTime()).toBe(start);
  await expect(page.locator("#window-mode")).toHaveText("Fixed window");
  const next = historyRequest();
  await page.getByRole("button", { name: "Next time window", exact: true }).click();
  const nextUrl = new URL((await next).url());
  expect(new Date(nextUrl.searchParams.get("from")!).getTime()).toBe(start);
  expect(new Date(nextUrl.searchParams.get("to")!).getTime()).toBe(end);
  await expect(page.getByRole("button", { name: "Next time window", exact: true })).toBeDisabled();
  await page.locator("#from").fill("2026-10-24T10:15");
  await page.locator("#to").fill("2026-10-24T13:45");
  const custom = historyRequest();
  await page.getByRole("button", { name: "Apply custom range", exact: true }).click();
  const customUrl = new URL((await custom).url());
  const customStart = new Date(customUrl.searchParams.get("from")!).getTime();
  const customEnd = new Date(customUrl.searchParams.get("to")!).getTime();
  expect(customEnd - customStart).toBe(3.5 * 3600000);
  const shifted = historyRequest();
  await page.getByRole("button", { name: "Previous time window", exact: true }).click();
  const shiftedUrl = new URL((await shifted).url());
  expect(new Date(shiftedUrl.searchParams.get("to")!).getTime()).toBe(customStart);
  expect(new Date(shiftedUrl.searchParams.get("from")!).getTime()).toBe(customStart - 3.5 * 3600000);
  await expect(page.locator("#days tr")).toHaveCount(5);
  await page.setViewportSize({ width: 375, height: 812 });
  for (const selector of [".graph-controls", ".window-navigation", "#daily-peaks"]) {
    const box = await page.locator(selector).boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(375);
  }
  expect(errors).toEqual([]);
});

test("pausing graph refresh preserves its canvas and dates while live snapshots continue", async ({ page }) => {
  await injectSnapshots(page);
  let historyRequests = 0;
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/analytics/history") historyRequests++;
  });
  await page.goto("/");
  await expect(page.locator("#days tr")).toHaveCount(5);
  const before = await (await page.request.get("/api/config")).json();
  const toggle = page.getByRole("switch", { name: "Auto-refresh graph", exact: true });
  await toggle.uncheck();
  await expect(page.locator("#graph-refresh-status")).toContainText("meter polling continue unchanged");
  const dates = [await page.locator("#from").inputValue(), await page.locator("#to").inputValue()];
  await page.locator("#chart canvas").first().evaluate(canvas => canvas.setAttribute("data-preserved", "yes"));
  const count = historyRequests;
  await page.clock.setFixedTime(new Date(await page.evaluate(() => Date.now()) + 61000));
  const snapshot = {
    device_id: before.active_device_id, data_version: 10001,
    reading: { read_at: new Date().toISOString(), demand_w: 5000 },
    health: { polling_state: "running", telemetry_state: "fresh", storage_state: "ok", recovery_pending: false },
  };
  await page.evaluate(value => window.dispatchEvent(new CustomEvent("fixture-snapshot", { detail: JSON.stringify(value) })), snapshot);
  await expect(page.locator("#demand")).toHaveText("5");
  expect(historyRequests).toBe(count);
  expect(await page.locator("#from").inputValue()).toBe(dates[0]);
  expect(await page.locator("#to").inputValue()).toBe(dates[1]);
  await expect(page.locator('#chart canvas[data-preserved="yes"]')).toHaveCount(1);
  expect((await (await page.request.get("/api/config")).json()).revision).toBe(before.revision);
  await page.getByRole("button", { name: "Refresh graph", exact: true }).click();
  await expect(page.locator('#chart canvas[data-preserved="yes"]')).toHaveCount(0);
  await expect(page.locator("#days tr")).toHaveCount(5);
  await expect(toggle).not.toBeChecked();

  await toggle.check();
  await expect(page.locator("#days tr")).toHaveCount(5);
  await page.getByRole("button", { name: "Previous time window", exact: true }).click();
  await expect(page.locator("#days tr")).toHaveCount(5);
  const fixedDates = [await page.locator("#from").inputValue(), await page.locator("#to").inputValue()];
  const afterResume = historyRequests;
  await page.clock.setFixedTime(new Date(await page.evaluate(() => Date.now()) + 61000));
  await page.evaluate(value => window.dispatchEvent(new CustomEvent("fixture-snapshot", { detail: JSON.stringify(value) })), {
    ...snapshot, data_version: 10002, reading: { ...snapshot.reading, demand_w: 5500 },
  });
  await expect(page.locator("#demand")).toHaveText("5.5");
  await expect.poll(() => historyRequests).toBeGreaterThan(afterResume);
  expect(await page.locator("#from").inputValue()).toBe(fixedDates[0]);
  expect(await page.locator("#to").inputValue()).toBe(fixedDates[1]);
  expect((await (await page.request.get("/api/config")).json()).polling_enabled).toBe(before.polling_enabled);
});

test("off-peak shading uses the selected cached tariff window and the 6 kW line only appears in range", async ({ page }) => {
  let highPeak = true;
  let hasOffPeak = true;
  await page.route("**/api/analytics/history?**", async route => {
    const response = await route.fetch();
    const history = await response.json();
    history.series.sampled_peak_w = history.series.sampled_peak_w.map((value: number | null) =>
      value === null ? null : highPeak ? 8000 : 2000);
    history.series.mean_import_w = history.series.mean_import_w.map((value: number | null) =>
      value === null ? null : highPeak ? 8000 : 2000);
    history.series.mean_ev_w = history.series.mean_ev_w.map((value: number | null) =>
      value === null ? null : highPeak ? 2000 : 1000);
    await route.fulfill({ response, json: history });
  });
  const queriedWindows: string[][] = [];
  await page.route("**/api/tariff?**", async route => {
    const response = await route.fetch();
    const status = await response.json();
    const url = new URL(route.request().url());
    const from = url.searchParams.get("from")!, to = url.searchParams.get("to")!;
    queriedWindows.push([from, to]);
    status.rates = hasOffPeak ? [{
      from, to: new Date((new Date(from).getTime() + new Date(to).getTime()) / 2).toISOString(),
      band: "ECO7_NIGHT", pence_per_kwh: 6.6,
    }] : [];
    await route.fulfill({ response, json: status });
  });
  await page.goto("/");
  await expect(page.locator("#off-peak-key")).toBeVisible();
  await expect(page.locator("#threshold-key")).toBeVisible();
  await expect(page.locator("#chart .u-legend")).not.toContainText("Trailing");
  await page.getByRole("button", { name: "Previous time window", exact: true }).click();
  await expect(page.locator("#off-peak-key")).toBeVisible();
  expect(queriedWindows.length).toBeGreaterThanOrEqual(2);
  const latest = queriedWindows.at(-1)!;
  const previous = queriedWindows.at(-2)!;
  expect(new Date(latest[1]).getTime()).toBe(new Date(previous[0]).getTime());
  highPeak = false;
  hasOffPeak = false;
  await page.getByRole("button", { name: "24 hours", exact: true }).click();
  await expect(page.locator("#days tr")).toHaveCount(5);
  await expect(page.locator("#threshold-key")).toBeHidden();
  await expect(page.locator("#off-peak-key")).toBeHidden();
  await expect(page.locator("#off-peak-status")).toContainText("No cached household tariff intervals");
});

test("daily peak and tariff failures are explicit without losing the demand graph", async ({ page }) => {
  await page.route("**/api/analytics/daily-peaks?**", route => route.fulfill({
    status: 503, json: { detail: "Daily archives are rebuilding" },
  }));
  await page.route("**/api/tariff?**", route => route.fulfill({
    status: 503, json: { detail: "Tariff cache temporarily unavailable" },
  }));
  await page.goto("/");
  await expect(page.locator("#chart canvas").first()).toBeVisible();
  await expect(page.locator("#days-status")).toContainText("Daily archives are rebuilding");
  await expect(page.locator("#days tr")).toHaveCount(0);
  await expect(page.locator("#off-peak-status")).toContainText("Tariff cache temporarily unavailable");
  await expect(page.locator("#off-peak-key")).toBeHidden();
  await expect(page.locator("#peak")).toHaveText("8 kW");
});
