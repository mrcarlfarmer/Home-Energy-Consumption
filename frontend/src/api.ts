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
    recovery_incomplete: boolean;
    low_disk: boolean;
  };
}
export interface Metrics {
  coverage_pct: number; observed_seconds: number; requested_seconds: number;
  import_energy_kwh: number | null; sampled_peak_w: number | null; sampled_peak_at: string | null;
}
export interface Summary extends Metrics {
  device_id: string;
  thresholds: {
    threshold_w: number; seconds_above: number; time_above_pct: number | null;
    energy_when_above_kwh: number | null; excess_energy_kwh: number | null;
  }[];
  daily_peaks: (Metrics & { date: string })[];
}
export interface History {
  interval_seconds: number;
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
export async function session(password?: string): Promise<void> {
  const result = await api<{ csrf_token: string }>(
    password === undefined ? "/api/auth/session" : "/api/auth/login",
    password === undefined ? undefined : { password },
  );
  csrf = result.csrf_token;
}
