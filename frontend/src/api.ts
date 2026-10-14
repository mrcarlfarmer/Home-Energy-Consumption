export interface Device { device_id: string; label: string }
export interface Config {
  revision: number; account_number: string | null; active_device_id: string | null;
  has_api_key: boolean; polling_enabled: boolean; poll_interval_seconds: number;
  display_timezone: string; devices: Device[];
}
export interface Snapshot {
  device_id: string | null; data_version: number;
  reading: { read_at: string; demand_w: number | null; import_demand_w: number | null } | null;
  health: {
    polling_state: string; telemetry_state: string; storage_state: string;
    error_code: string | null; last_success_at: string | null; next_attempt_at: string | null;
    last_attempt_at: string | null; last_stored_at: string | null;
    recovery_incomplete: boolean;
    low_disk: boolean;
    database_bytes: number | null; disk_free_bytes: number | null;
    storage_budget_bytes: number; storage_paused: boolean;
    maintenance_error: string | null; last_maintenance_at: string | null;
    recovery_pending: boolean; recovery_cursor_at: string | null;
    recovery_target_at: string | null; recovery_gap_count: number;
    recovery_error: string | null;
  };
}
export interface Metrics {
  coverage_pct: number; observed_seconds: number; requested_seconds: number;
  import_energy_kwh: number | null; sampled_peak_w: number | null; sampled_peak_at: string | null;
}
export interface StorageStatus {
  policy: { raw_days: number; five_minute_days: number; daily_days: number; recovery_days: number };
  retention: { five_before_us: number; archive_timezone: string | null } | null;
}
export interface Summary extends Metrics {
  device_id: string;
  warnings: string[];
  from: string; to: string;
  thresholds: {
    threshold_w: number; seconds_above: number; time_above_pct: number | null;
    energy_when_above_kwh: number | null; excess_energy_kwh: number | null;
  }[];
  daily_peaks: (Metrics & { date: string })[];
}
export interface History {
  interval_seconds: number;
  bucket_alignment: string; warnings: string[];
  series: {
    bucket_start_epoch_s: number[]; bucket_end_epoch_s: number[];
    mean_import_w: (number | null)[]; sampled_peak_w: (number | null)[];
    peak_at_epoch_s: (number | null)[]; rolling_15m_w: (number | null)[];
    coverage_pct: number[]; rolling_15m_coverage_pct: number[];
  };
}
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
let csrf = "";
export let authenticationRequired = true;
export async function api<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const retry = response.headers.get("Retry-After");
    throw new ApiError(`${data.detail ?? "Request failed"}${retry ? ` (retry in ${retry}s)` : ""}`, response.status);
  }
  return data as T;
}
export async function session(password?: string, username?: string): Promise<void> {
  const result = await api<{ csrf_token: string; authentication_required: boolean }>(
    password === undefined ? "/api/auth/session" : "/api/auth/login",
    password === undefined ? undefined : { username, password },
  );
  csrf = result.csrf_token;
  authenticationRequired = result.authentication_required;
}
