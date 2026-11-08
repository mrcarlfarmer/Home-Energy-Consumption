import { expect, test } from "@playwright/test";
import type { Snapshot } from "../src/api";

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
  await expect(page.locator("#recovery-status")).not.toHaveClass("recovering");
  const queued = await (await page.request.get("/api/storage")).json();
  expect(queued.recovery.recovery_pending).toBe(true);
  const after = await (await page.request.get("/api/config")).json();
  expect(after.revision).toBe(before.revision);
  expect(after.polling_enabled).toBe(true);
  await recover.click();
  await expect(page.locator("#recovery-status")).toContainText("requested recently");
  await expect(page.locator("#recovery-status")).not.toHaveClass("recovering");
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

test("recovery activity is subtle, respects reduced motion and stops when collection cannot proceed", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        window.addEventListener("fixture-snapshot", event => {
          this.dispatchEvent(new MessageEvent("snapshot", { data: (event as CustomEvent<string>).detail }));
        });
        window.addEventListener("fixture-disconnect", () => this.dispatchEvent(new Event("error")));
      }
    };
  });
  await page.goto("/");
  await expect(page.locator("#analysis-status")).toContainText("observed hours");
  const config = await (await page.request.get("/api/config")).json();
  const snapshot: Snapshot = {
    device_id: config.active_device_id, data_version: 1, reading: null,
    health: {
      polling_state: "running", telemetry_state: "fresh", storage_state: "ok",
      error_code: null, last_success_at: null, next_attempt_at: null,
      last_attempt_at: null, last_stored_at: null, recovery_incomplete: true,
      low_disk: false, database_bytes: 0, disk_free_bytes: 104857600,
      storage_budget_bytes: 2147483648, storage_paused: false,
      maintenance_error: null, last_maintenance_at: null,
      recovery_pending: true, recovery_cursor_at: "2026-11-07T11:01:15Z",
      recovery_target_at: "2026-11-08T10:27:30Z", recovery_gap_count: 0, recovery_error: null,
    },
  };
  const publish = (health: Partial<Snapshot["health"]> = {}) => page.evaluate(value =>
    window.dispatchEvent(new CustomEvent("fixture-snapshot", { detail: JSON.stringify(value) })),
    { ...snapshot, health: { ...snapshot.health, ...health } });
  const status = page.locator("#recovery-status");
  await expect(status).not.toHaveClass("recovering");
  await publish();
  await expect(status).toHaveClass("recovering");
  await expect(status).toContainText("Active-meter recovery in progress.");
  await expect(status).toContainText("Checking from");
  await expect(status).toContainText("Target:");
  await expect(status).toHaveAttribute("role", "status");
  const spinner = () => status.evaluate(node => {
    const style = getComputedStyle(node, "::before");
    return { animation: style.animationName, width: style.width, transform: style.transform };
  });
  expect(await spinner()).toMatchObject({ animation: "recovery-spin", width: "12px" });
  const initialTransform = (await spinner()).transform;
  await expect.poll(async () => (await spinner()).transform).not.toBe(initialTransform);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(status).toHaveClass("recovering");
  expect(await spinner()).toMatchObject({ animation: "none", width: "12px" });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.setViewportSize({ width: 375, height: 812 });
  const box = await status.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(375);

  for (const health of [
    { polling_state: "stopped" }, { polling_state: "backoff" },
    { polling_state: "auth_error" }, { polling_state: "configuration_error" },
    { polling_state: "storage_paused" }, { storage_state: "error" },
    { storage_paused: true },
  ]) {
    await publish(health);
    await expect(status).not.toHaveClass("recovering");
    await expect(status).toContainText("waiting for collection to resume");
    await publish();
    await expect(status).toHaveClass("recovering");
  }
  await publish({ recovery_pending: false, recovery_error: "range_too_old" });
  await expect(status).not.toHaveClass("recovering");
  await expect(status).toContainText("Recovery paused");
  await publish();
  await expect(status).toHaveClass("recovering");
  await page.evaluate(() => window.dispatchEvent(new Event("fixture-disconnect")));
  await expect(status).not.toHaveClass("recovering");
  await expect(status).toContainText("reconnecting");
  await publish();
  await expect(status).toHaveClass("recovering");
  await publish({ recovery_pending: false, recovery_incomplete: false });
  await expect(status).not.toHaveClass("recovering");
  await expect(status).toContainText("Recovery pass finished.");
  expect(errors).toEqual([]);
});
