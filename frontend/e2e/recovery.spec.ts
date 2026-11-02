import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8766" });

test("selected-range recovery queues real work without changing polling or credential drafts", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#analysis-status")).toContainText("observed hours");
  if (await page.getByRole("button", { name: "Stop polling", exact: true }).count()) {
    await page.getByRole("button", { name: "Stop polling", exact: true }).click();
  }
  const recover = page.getByRole("button", { name: "Recover missing readings", exact: true });
  await expect(recover).toBeDisabled();
  await expect(page.locator("#recovery-help")).toContainText("Enable meter polling");
  await page.getByRole("button", { name: "Start polling", exact: true }).click();
  await expect(recover).toBeEnabled();
  const before = await (await page.request.get("/api/config")).json();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByLabel("Replace Octopus API key", { exact: false }).fill("unsaved-credential-draft");
  await page.getByRole("link", { name: "Overview", exact: true }).click();
  const responsePromise = page.waitForResponse(response => response.url().endsWith("/api/recovery"));
  await recover.click();
  const response = await responsePromise;
  expect(response.status()).toBe(202);
  const request = response.request().postDataJSON();
  expect(request.device_id).toBe(before.active_device_id);
  expect(request.expected_revision).toBe(before.revision);
  expect(Object.keys(request).sort()).toEqual(["device_id", "expected_revision", "from", "to"]);
  expect(new Date(request.to).getTime() - new Date(request.from).getTime()).toBe(86400000);
  await expect(page.locator("#recovery-status")).toContainText("Recovery queued");
  const queued = await (await page.request.get("/api/storage")).json();
  expect(queued.recovery.recovery_pending).toBe(true);
  const after = await (await page.request.get("/api/config")).json();
  expect(after.revision).toBe(before.revision);
  expect(after.polling_enabled).toBe(true);
  await recover.click();
  await expect(page.locator("#recovery-status")).toContainText("requested recently");
  await expect(recover).toBeEnabled();
  await page.getByRole("button", { name: "30 days", exact: true }).click();
  await expect(page.locator("#analysis-status")).toContainText("observed hours");
  await expect(recover).toBeEnabled();
  await page.locator("#from").fill("2025-01-01T00:00");
  await page.locator("#to").fill("2025-01-02T00:00");
  await page.getByRole("button", { name: "Apply custom range", exact: true }).click();
  await expect(page.locator("#coverage")).toHaveText("0%");
  await expect(recover).toBeDisabled();
  await expect(page.locator("#recovery-help")).toContainText("retained raw history");
  expect(errors).toEqual([]);
});
