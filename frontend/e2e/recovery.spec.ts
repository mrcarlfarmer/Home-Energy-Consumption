import { expect, test, type Page } from "@playwright/test";
import type { Snapshot } from "../src/api";

test.use({ baseURL: "http://127.0.0.1:8766" });

async function isolateRecoveryUpdates(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        this.close();
        window.addEventListener("fixture-recovery-snapshot", event => {
          const data = (event as CustomEvent<string>).detail;
          sessionStorage.setItem("fixture-recovery-snapshot", data);
          this.dispatchEvent(new MessageEvent("snapshot", { data }));
        });
        window.addEventListener("fixture-recovery-disconnect", () => this.dispatchEvent(new Event("error")));
        queueMicrotask(() => {
          this.dispatchEvent(new Event("open"));
          const data = sessionStorage.getItem("fixture-recovery-snapshot");
          if (data) this.dispatchEvent(new MessageEvent("snapshot", { data }));
        });
      }
    };
  });
}

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
  await expect(page.locator("#recovery-feedback")).toHaveAttribute("data-state", "queued");
  await expect(page.locator("#recovery-spinner")).toBeVisible();
  await expect(recover).toHaveAttribute("aria-busy", "true");
  const queued = await (await page.request.get("/api/storage")).json();
  expect(queued.recovery.recovery_pending).toBe(true);
  const after = await (await page.request.get("/api/config")).json();
  expect(after.revision).toBe(before.revision);
  expect(after.polling_enabled).toBe(true);
  await recover.click();
  await expect(page.locator("#recovery-status")).toContainText("requested recently");
  // This fixture has no collector worker; queued work cannot advance.
  await expect(page.locator("#recovery-feedback")).toHaveAttribute("data-state", "paused");
  await expect(page.locator("#recovery-spinner")).toBeHidden();
  await expect(page.locator("#recovery-status")).toContainText("collector is stopped");
  await expect(page.locator("#recovery-state")).not.toHaveText("Recovery complete");
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

test("recovery feedback distinguishes active work, waiting, pauses, completion and disconnected updates", async ({ page }) => {
  await isolateRecoveryUpdates(page);
  await page.goto("/");
  await expect(page.locator("#analysis-status")).toContainText("observed hours");
  await page.getByRole("switch", { name: "Auto-refresh graph", exact: true }).uncheck();
  const config = await (await page.request.get("/api/config")).json();
  const at = Date.now();
  const snapshot: Snapshot = {
    device_id: config.active_device_id, data_version: 1, reading: null,
    health: {
      polling_state: "running", telemetry_state: "fresh", storage_state: "ok",
      error_code: null, last_success_at: null, next_attempt_at: null,
      last_attempt_at: null, last_stored_at: null, recovery_incomplete: true,
      low_disk: false, database_bytes: null, disk_free_bytes: null,
      storage_budget_bytes: 104857600, storage_paused: false,
      maintenance_error: null, last_maintenance_at: null,
      recovery_state: "pending", recovery_active: false, recovery_pending: true,
      recovery_cursor_at: new Date(at - 3600000).toISOString(),
      recovery_target_at: new Date(at - 180000).toISOString(),
      recovery_last_progress_at: null, recovery_gap_count: 0, recovery_error: null,
    },
  };
  const emit = async (health: Partial<Snapshot["health"]> = {}) => {
    snapshot.health = { ...snapshot.health, ...health };
    await page.evaluate(value => window.dispatchEvent(new CustomEvent("fixture-recovery-snapshot", { detail: JSON.stringify(value) })), snapshot);
  };
  const feedback = page.locator("#recovery-feedback");
  const spinner = page.locator("#recovery-spinner");
  await emit();
  await expect(feedback).toHaveAttribute("data-state", "queued");
  await expect(spinner).toBeVisible();
  expect(await spinner.evaluate(element => getComputedStyle(element).animationName)).toBe("recovery-spin");
  await expect(page.locator("#recovery-status")).toContainText("4 fifteen-minute windows");
  await emit({ recovery_active: true });
  await expect(feedback).toHaveAttribute("data-state", "running");
  await expect(page.locator("#recovery-state")).toHaveText("Checking missing readings");
  if (config.polling_enabled) {
    await page.route("**/api/recovery", route => route.fulfill({
      status: 429, json: { detail: "Recovery was requested recently" },
    }));
    await page.getByRole("button", { name: "Recover missing readings", exact: true }).click();
    await expect(feedback).toHaveAttribute("data-state", "running");
    await expect(spinner).toBeVisible();
    await expect(page.locator("#recovery-status")).toContainText("requested recently");
  }
  await emit({
    recovery_active: false,
    recovery_cursor_at: new Date(at - 2700000).toISOString(),
    recovery_last_progress_at: new Date(at).toISOString(),
  });
  await expect(feedback).toHaveAttribute("data-state", "waiting");
  await expect(page.locator("#recovery-status")).toContainText("Last recovery progress");
  await expect(page.locator("#recovery-status")).toContainText("3 fifteen-minute windows");
  await emit({ polling_state: "backoff", error_code: "network_unavailable", next_attempt_at: new Date(at + 45000).toISOString() });
  await expect(feedback).toHaveAttribute("data-state", "waiting");
  await expect(spinner).toBeHidden();
  await expect(page.locator("#recovery-status")).toContainText("network unavailable");
  await expect(page.locator("#recovery-status")).toContainText("Next collection attempt");
  await emit({ polling_state: "auth_error", error_code: "authentication_failed" });
  await expect(feedback).toHaveAttribute("data-state", "paused");
  await expect(spinner).toBeHidden();
  await expect(page.locator("#recovery-status")).toContainText("authentication failed");
  await emit({ polling_state: "running", error_code: null, storage_paused: true });
  await expect(feedback).toHaveAttribute("data-state", "paused");
  await expect(page.locator("#recovery-status")).toContainText("storage is paused");
  await emit({ storage_paused: false, polling_state: "stopped" });
  await expect(feedback).toHaveAttribute("data-state", "paused");
  await expect(page.locator("#recovery-status")).toContainText(config.polling_enabled ? "collector is stopped" : "Start polling to continue");
  await emit({ polling_state: "running", recovery_state: "paused", recovery_pending: false, recovery_error: "invalid_telemetry_shape" });
  await expect(feedback).toHaveAttribute("data-state", "paused");
  await expect(page.locator("#recovery-status")).toContainText("invalid telemetry shape");
  await emit({ recovery_state: "complete", recovery_error: null, recovery_gap_count: 3 });
  await expect(feedback).toHaveAttribute("data-state", "incomplete");
  await expect(page.locator("#recovery-state")).toHaveText("Recovery finished with gaps");
  await expect(page.locator("#recovery-status")).toContainText("3 historical windows");
  await expect(spinner).toBeHidden();
  await page.reload();
  await expect(feedback).toHaveAttribute("data-state", "incomplete");
  await expect(page.locator("#recovery-state")).toHaveText("Recovery finished with gaps");
  await emit({ recovery_gap_count: 0 });
  await expect(feedback).toHaveAttribute("data-state", "complete");
  await expect(spinner).toBeHidden();
  await expect(page.locator("#recover-history")).toHaveAttribute("aria-busy", "false");
  await emit({ recovery_state: "pending", recovery_pending: true, recovery_active: true });
  await expect(spinner).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("fixture-recovery-disconnect")));
  await expect(feedback).toHaveAttribute("data-state", "disconnected");
  await expect(spinner).toBeHidden();
  await expect(page.locator("#recovery-status")).toContainText("progress is unknown");
  await emit();
  await expect(feedback).toHaveAttribute("data-state", "running");
  await expect(spinner).toBeVisible();
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await spinner.evaluate(element => getComputedStyle(element).animationName)).toBe("none");
  await page.setViewportSize({ width: 375, height: 812 });
  const box = await feedback.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(375);
  await expect(feedback).toHaveAttribute("role", "status");
  await expect(feedback).toHaveAttribute("aria-live", "polite");
});

test("requesting recovery animates until a rejected request reports an error rather than completion", async ({ page }) => {
  await isolateRecoveryUpdates(page);
  await page.goto("/");
  await expect(page.locator("#analysis-status")).toContainText("observed hours");
  if (await page.getByRole("button", { name: "Start polling", exact: true }).count()) {
    await page.getByRole("button", { name: "Start polling", exact: true }).click();
  }
  let finish!: () => void;
  const gate = new Promise<void>(resolve => { finish = resolve; });
  await page.route("**/api/recovery", async route => {
    await gate;
    await route.fulfill({ status: 503, json: { detail: "Recovery service temporarily unavailable" } });
  });
  const recover = page.getByRole("button", { name: "Recover missing readings", exact: true });
  await recover.click();
  await expect(page.locator("#recovery-feedback")).toHaveAttribute("data-state", "requesting");
  await expect(page.locator("#recovery-spinner")).toBeVisible();
  await expect(recover).toBeDisabled();
  finish();
  await expect(page.locator("#recovery-feedback")).toHaveAttribute("data-state", "error");
  await expect(page.locator("#recovery-spinner")).toBeHidden();
  await expect(page.locator("#recovery-state")).toHaveText("Recovery request failed");
  await expect(page.locator("#recovery-status")).toContainText("Recovery service temporarily unavailable");
  await expect(recover).toBeEnabled();
});

test("recovery activity is subtle, respects reduced motion and stops when collection cannot proceed", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await isolateRecoveryUpdates(page);
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
      recovery_state: "pending", recovery_active: true, recovery_pending: true,
      recovery_cursor_at: "2026-11-07T11:01:15Z",
      recovery_target_at: "2026-11-08T10:27:30Z",
      recovery_last_progress_at: "2026-11-08T10:20:00Z", recovery_gap_count: 0, recovery_error: null,
    },
  };
  const publish = (health: Partial<Snapshot["health"]> = {}) => page.evaluate(value =>
    window.dispatchEvent(new CustomEvent("fixture-recovery-snapshot", { detail: JSON.stringify(value) })),
    { ...snapshot, health: { ...snapshot.health, ...health } });
  const feedback = page.locator("#recovery-feedback");
  const status = page.locator("#recovery-status");
  const indicator = page.locator("#recovery-spinner");
  await expect(indicator).toBeHidden();
  await publish();
  await expect(indicator).toBeVisible();
  await expect(page.locator("#recovery-state")).toHaveText("Checking missing readings");
  await expect(status).toContainText("Recovery cursor:");
  await expect(status).toContainText("Target:");
  await expect(feedback).toHaveAttribute("role", "status");
  const spinner = () => indicator.evaluate(node => {
    const style = getComputedStyle(node);
    return { animation: style.animationName, width: style.width, transform: style.transform };
  });
  expect(await spinner()).toMatchObject({ animation: "recovery-spin", width: "12px" });
  const initialTransform = (await spinner()).transform;
  await expect.poll(async () => (await spinner()).transform).not.toBe(initialTransform);
  expect(await status.evaluate(node => getComputedStyle(node, "::before").content)).toBe("none");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(indicator).toBeVisible();
  expect(await spinner()).toMatchObject({ animation: "none", width: "12px" });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.setViewportSize({ width: 375, height: 812 });
  const box = await feedback.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(375);

  for (const [health, state] of [
    [{ polling_state: "stopped" }, "paused"], [{ polling_state: "backoff" }, "waiting"],
    [{ polling_state: "auth_error" }, "paused"], [{ polling_state: "configuration_error" }, "paused"],
    [{ polling_state: "storage_paused" }, "paused"], [{ storage_state: "error" }, "paused"],
    [{ storage_paused: true }, "paused"],
  ] as const) {
    await publish(health);
    await expect(indicator).toBeHidden();
    await expect(feedback).toHaveAttribute("data-state", state);
    await publish();
    await expect(indicator).toBeVisible();
  }
  await publish({ recovery_state: "paused", recovery_pending: false, recovery_error: "range_too_old" });
  await expect(indicator).toBeHidden();
  await expect(page.locator("#recovery-state")).toHaveText("Recovery paused");
  await publish();
  await expect(indicator).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("fixture-recovery-disconnect")));
  await expect(indicator).toBeHidden();
  await expect(status).toContainText("Reconnecting");
  await publish();
  await expect(indicator).toBeVisible();
  await publish({ recovery_state: "complete", recovery_pending: false, recovery_incomplete: false });
  await expect(indicator).toBeHidden();
  await expect(page.locator("#recovery-state")).toHaveText("Recovery complete");
  await expect(status).toContainText("recovery pass finished");
  expect(errors).toEqual([]);
});
