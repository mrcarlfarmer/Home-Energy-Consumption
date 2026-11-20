import { test, expect } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8766" });

test("architectural light and dark themes and navigation remain usable across screen sizes", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#thresholds tr")).toHaveCount(1);
  expect(await page.evaluate(() => document.body.innerText)).not.toMatch(/swiss\s+grid/i);
  const readTheme = () => page.locator("#dashboard").evaluate(element => {
    const style = getComputedStyle(element);
    return {
      scheme: style.colorScheme,
      background: style.getPropertyValue("--bg").trim().toLowerCase(),
      accent: style.getPropertyValue("--accent").trim().toLowerCase(),
      chart: style.getPropertyValue("--chart-mean").trim().toLowerCase(),
      chartPeak: style.getPropertyValue("--chart-peak").trim().toLowerCase(),
      font: style.fontFamily,
      themeColor: document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.content.toLowerCase(),
    };
  });
  const lightTheme = await readTheme();
  expect(lightTheme).toMatchObject({
    scheme: "light", background: "#f5f6f8", accent: "#1d4ed8", chart: "#0f766e", chartPeak: "#111315", themeColor: "#f5f6f8",
  });
  expect(lightTheme.font).toContain("sans-serif");

  const themeSwitch = page.getByRole("switch", { name: "Dark mode", exact: true });
  await expect(themeSwitch).toHaveAttribute("aria-checked", "false");
  await themeSwitch.click();
  await expect(themeSwitch).toHaveAttribute("aria-checked", "true");
  const darkTheme = await readTheme();
  expect(darkTheme).toMatchObject({
    scheme: "dark", background: "#0d0f12", accent: "#60a5fa", chart: "#2dd4bf", chartPeak: "#f3f5f8", themeColor: "#0d0f12",
  });
  await expect(page.locator("#chart canvas").first()).toBeVisible();

  await page.reload();
  await expect(page.getByRole("switch", { name: "Dark mode", exact: true })).toHaveAttribute("aria-checked", "true");
  expect((await readTheme()).scheme).toBe("dark");

  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const version = page.getByLabel("Application version", { exact: true });
    await expect(version).toBeVisible();
    await expect(version).toHaveText(/^Build \d+ \([a-f0-9]{12}(?:, uncommitted changes)?\)$/);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const nav = page.getByRole("navigation", { name: "Dashboard navigation" });
    await nav.getByRole("link", { name: "Settings", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeInViewport();
    await nav.getByRole("link", { name: "Inverter sizing", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Explore inverter sizing" })).toBeInViewport();
    await nav.getByRole("link", { name: "Demand history", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Demand, in detail" })).toBeInViewport();
    await expect(page.locator("#chart canvas").first()).toBeVisible();
  }

  await page.getByRole("switch", { name: "Dark mode", exact: true }).click();
  await expect(page.getByRole("switch", { name: "Dark mode", exact: true })).toHaveAttribute("aria-checked", "false");
  expect((await readTheme()).scheme).toBe("light");
});

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
  await expect(page.locator("#thresholds tr")).toHaveCount(1);
  await expect(page.locator(".access-warning")).toContainText("anyone who can reach");
  await expect(page.getByLabel("Application version", { exact: true })).toBeVisible();
  await expect(page.locator(".transport-warning")).toHaveCount(0);
  await expect(page.getByText("HTTP connection:", { exact: false })).toHaveCount(0);
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
