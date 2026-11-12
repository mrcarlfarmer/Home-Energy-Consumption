import { api, ApiError, authenticationRequired, Config, DailyPeaks, Device, History, RecoveryStatus, session, Snapshot, StorageStatus, Summary, TariffStatus } from "./api";
import { DemandChart } from "./charts";
import { DailyPage, dayLink } from "./daily";
import "./styles.css";

declare const __APP_VERSION__: string;
const [buildNumber, buildRevision, buildDirty] = __APP_VERSION__.split("-");
const versionMarkup = `<p class="app-version" aria-label="Application version">Build ${buildNumber} (${buildRevision}${buildDirty ? ", uncommitted changes" : ""})</p>`;
const root = document.querySelector<HTMLDivElement>("#app")!;
const transportNotice = location.protocol === "http:"
  ? "HTTP connection: data and API-key entry are not encrypted in transit. Use a trusted LAN only."
  : "";
let config: Config;
let stream: EventSource | undefined;
let chart: DemandChart | undefined;
let pending: AbortController | undefined;
let lastRefresh = 0;
let lastVersion = -1;
let selectedMinutes = 1440;
let generation = 0;
let recoveryRange: { from: string; to: string; device_id: string } | undefined;
let recoveryBusy = false;
let recoveryPending = false;
let recoveryHealth: Snapshot["health"] | undefined;
let recoveryConnected = false;
let recoveryRequestError = "";
let viewRange = { from: new Date(), to: new Date() };
let followLatest = true;
let autoRefresh = true;
let pendingAutomatic = false;
let analysisDirty = false;
let dailyPage: DailyPage | undefined;
let dailyVisible = false;
let overviewScroll = 0;
let overviewInterrupted = false;
let overviewLoaded = false;

function fillRecovery(): void {
  element<HTMLButtonElement>("recover-history").disabled = recoveryBusy || !config.polling_enabled
    || !recoveryRange || recoveryRange.device_id !== config.active_device_id;
  text("recovery-help", !config.polling_enabled
    ? "Enable meter polling to recover missing historical readings. This button never starts polling."
    : !recoveryRange || recoveryRange.device_id !== config.active_device_id
      ? "Select the active meter and a range within retained raw history (at most 31 days) to recover readings."
      : "Retries missing Octopus readings in this range, excluding the newest three minutes. Live collection continues. Older data may be unavailable; missing Ohme history is not recovered here.");
}

type RecoveryDisplayState = "idle" | "requesting" | "queued" | "running" | "waiting" | "paused" | "complete" | "incomplete" | "error" | "disconnected";
function showRecovery(state: RecoveryDisplayState, message: string, active = ["requesting", "queued", "running", "waiting"].includes(state)): void {
  const labels: Record<RecoveryDisplayState, string> = {
    idle: "", requesting: "Requesting recovery", queued: "Recovery queued",
    running: "Checking missing readings", waiting: "Waiting to continue recovery",
    paused: "Recovery paused", complete: "Recovery complete",
    incomplete: "Recovery finished with gaps", error: "Recovery request failed",
    disconnected: "Recovery status unavailable",
  };
  const busy = ["requesting", "queued", "running", "waiting"].includes(state);
  const feedback = element("recovery-feedback");
  feedback.hidden = state === "idle";
  feedback.dataset.state = state;
  element("recovery-spinner").hidden = !active;
  element("recover-history").setAttribute("aria-busy", String(busy));
  text("recovery-state", labels[state]);
  text("recovery-status", message);
}
function updateRecovery(health: Snapshot["health"]): void {
  const details = [
    health.recovery_cursor_at ? `Recovery cursor: ${new Date(health.recovery_cursor_at).toLocaleString()}.` : "",
    health.recovery_target_at ? `Target: ${new Date(health.recovery_target_at).toLocaleString()}.` : "",
    health.recovery_last_progress_at ? `Last recovery progress: ${new Date(health.recovery_last_progress_at).toLocaleString()}.` : "",
    recoveryRequestError ? `Recovery request error: ${recoveryRequestError}` : "",
  ];
  if (health.recovery_pending && health.recovery_cursor_at && health.recovery_target_at) {
    const remaining = Math.max(0, (new Date(health.recovery_target_at).getTime() - new Date(health.recovery_cursor_at).getTime()) / 1000);
    details.push(`${number(Math.ceil(remaining / 900), 0)} fifteen-minute windows remain to check. Live and historical requests alternate at the configured polling interval.`);
  }
  const show = (state: RecoveryDisplayState, message: string, active?: boolean) => showRecovery(state, [message, ...details].filter(Boolean).join(" "), active);
  if (health.recovery_error || health.recovery_state === "paused") {
    show("paused", `${health.recovery_error?.replaceAll("_", " ") ?? "Historical recovery is paused"}. Correct settings or request another recovery pass.`);
  } else if (health.recovery_pending) {
    if (health.storage_paused || health.storage_state !== "ok" || health.polling_state === "storage_paused") {
      show("paused", "Recovery cannot continue while storage is paused or unavailable. Check Collection health and restore storage capacity.");
    } else if (health.polling_state === "stopped") {
      show("paused", config.polling_enabled
        ? "Polling is enabled, but the collector is stopped. Recovery cannot advance. Check Collection health; restart the service if the collector stays stopped."
        : "Recovery is waiting for meter polling to be enabled. Start polling to continue; queued history is retained.");
    } else if (["auth_error", "configuration_error"].includes(health.polling_state)) {
      show("paused", `Recovery cannot continue: ${(health.error_code ?? health.polling_state).replaceAll("_", " ")}. Correct the saved connection settings.`);
    } else if (health.polling_state === "backoff") {
      show("waiting", [
        `Recovery will retry after an upstream error: ${(health.error_code ?? "temporary upstream error").replaceAll("_", " ")}.`,
        health.next_attempt_at ? `Next collection attempt: ${new Date(health.next_attempt_at).toLocaleTimeString()}.` : "",
      ].filter(Boolean).join(" "), false);
    } else if (health.recovery_active) {
      show("running", "Active-meter recovery is checking the next historical window with Octopus.");
    } else {
      show(health.recovery_last_progress_at ? "waiting" : "queued",
        "Active-meter recovery is queued between collection requests. It has not finished.");
    }
  } else if (health.recovery_state === "complete") {
    show(health.recovery_gap_count ? "incomplete" : "complete", health.recovery_gap_count
      ? `The recovery pass finished, but ${number(health.recovery_gap_count, 0)} historical windows still contain missing or unavailable samples. No gaps are filled with invented data. Check Collection health; you can retry missing readings later.`
      : "The recovery pass finished with no recorded unresolved windows.");
  } else if (recoveryRequestError) {
    showRecovery("error", recoveryRequestError);
  } else {
    showRecovery("idle", "");
  }
}

function element<T extends HTMLElement = HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing UI element: ${id}`);
  return found as T;
}
function text(id: string, value: string): void { element(id).textContent = value; }
function number(value: number | null, digits = 2): string {
  return value === null ? "Unavailable" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
}
function money(value: number | null): string {
  return value === null ? "Unavailable" : new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }).format(value);
}
function showCharging(status?: TariffStatus): void {
  const body = element<HTMLTableSectionElement>("charging-periods");
  body.replaceChildren();
  const events = [
    ...(status?.charging ?? []).map(interval => ({ ...interval, label: "Observed EV charging", price: "--" })),
    ...(status?.rates ?? []).filter(rate => rate.band === "EV_BONUS")
      .map(rate => ({ ...rate, label: "Estimated bonus household slot", price: `${number(rate.pence_per_kwh, 5)} p/kWh` })),
    ...(status?.unresolved ?? []).map(interval => ({ ...interval, label: "Unresolved household rate", price: "Unavailable" })),
  ].sort((a, b) => a.from.localeCompare(b.from));
  element("charging-details").hidden = !status?.adjustment_enabled;
  for (const event of events.slice(0, 200)) {
    const row = body.insertRow();
    for (const value of [
      event.label, new Date(event.from).toLocaleString(), new Date(event.to).toLocaleString(), event.price,
    ]) row.insertCell().textContent = value;
  }
  text("charging-details-note", [
    "Times are clipped to the selected window, in your browser timezone. Bonus rates cover settlement half-hours, not just charging minutes.",
    status?.ev_coverage_pct != null ? `Recorded EV coverage: ${number(status.ev_coverage_pct)}% of the relevant tariff span.` : "",
    events.length > 200 ? `Showing the first 200 of ${events.length} intervals; select a shorter range for detail.` : "",
    "Assumes Prioritise Savings with no Max/Boost charging. These are estimates, not supplier-confirmed billing slots.",
  ].filter(Boolean).join(" "));
}
function showTariff(status: TariffStatus): void {
  text("tariff-status", [
    status.tariff_name ? `${status.tariff_name} (${status.tariff_code})` : "No tariff pricing fetched.",
    status.enabled ? "Cost estimates enabled." : "Cost estimates disabled.",
    status.fetched_at ? `Fetched: ${new Date(status.fetched_at).toLocaleString()}.` : "",
    status.schedule ?? "",
    status.state === "refreshing" ? "Refreshing pricing..." : "",
    status.error_code ? `Refresh error: ${status.error_code.replaceAll("_", " ")}. Previously fetched rates are retained.` : "",
    ...status.warnings,
  ].filter(Boolean).join(" "));
  const body = element<HTMLTableSectionElement>("tariff-rates"); body.replaceChildren();
  for (const rate of status.rates) {
    const row = body.insertRow();
    for (const value of [
      rate.band.replace("ECO7_", "").toLowerCase(),
      `${number(rate.pence_per_kwh, 5)} p/kWh`,
      new Date(rate.from).toLocaleString(), new Date(rate.to).toLocaleString(),
    ]) row.insertCell().textContent = value;
  }
}
async function loadTariff(): Promise<void> {
  try { showTariff(await api<TariffStatus>("/api/tariff")); }
  catch (error) {
    text("tariff-status", error instanceof Error ? error.message : "Tariff pricing could not be loaded.");
  }
}
function duration(seconds: number): string {
  const total = Math.round(seconds * 100) / 100;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return [
    hours ? `${number(hours, 0)} h` : "",
    minutes ? `${minutes} min` : "",
    total % 60 || !total ? `${number(total % 60)} s` : "",
  ].filter(Boolean).join(" ");
}
function showError(error: unknown): void {
  if (error instanceof DOMException && error.name === "AbortError") return;
  if (error instanceof ApiError && error.status === 401 && document.getElementById("dashboard")) {
    renderLogin("Your session expired. Sign in again.");
    return;
  }
  text("message", error instanceof Error ? error.message : "Unexpected application error");
}
function clearMessage(): void { text("message", ""); }
function cleanup(): void {
  generation++;
  recoveryRange = undefined; recoveryBusy = false; recoveryPending = false;
  recoveryHealth = undefined; recoveryConnected = false; recoveryRequestError = "";
  window.onhashchange = null;
  stream?.close(); stream = undefined;
  chart?.destroy(); chart = undefined;
  pending?.abort(); pending = undefined;
  pendingAutomatic = false;
  dailyPage?.destroy(); dailyPage = undefined;
  dailyVisible = false; overviewInterrupted = false; overviewLoaded = false;
}
function renderLogin(message = ""): void {
  cleanup();
  root.innerHTML = `<main class="login"><p class="eyebrow">YOUR HOME, IN FOCUS</p><h1>Home Energy</h1>
    <p>Private electricity monitoring and inverter sizing.</p>
    <form id="login"><label>Username<input id="username" autocomplete="username" value="admin" maxlength="64" required></label>
    <label>Dashboard password<input id="password" type="password" autocomplete="current-password" required></label>
    <button type="submit">Sign in</button></form><p id="message" role="alert"></p>
    <small>${transportNotice || "Credentials stay on your Raspberry Pi. Use your trusted HTTPS address."}</small>${versionMarkup}</main>`;
  text("message", message);
  element<HTMLFormElement>("login").onsubmit = async (event) => {
    event.preventDefault(); clearMessage();
    try {
      await session(element<HTMLInputElement>("password").value, element<HTMLInputElement>("username").value);
      await renderDashboard();
    } catch (error) { showError(error); }
  };
}
function deviceOptions(target: HTMLSelectElement, devices: Device[], selected: string | null): void {
  target.replaceChildren(new Option("Select electricity meter", ""));
  for (const device of devices) target.add(new Option(device.label, device.device_id));
  target.value = selected ?? "";
}
function fillPolling(): void {
  const ready = config.has_api_key && !!config.account_number && !!config.active_device_id;
  const toggle = element<HTMLButtonElement>("toggle-polling");
  toggle.textContent = config.polling_enabled ? "Stop polling" : "Start polling";
  toggle.disabled = !config.polling_enabled && !ready;
  text("polling-status", config.polling_enabled
    ? `Polling enabled. Scheduled interval: ${config.poll_interval_seconds} seconds.`
    : "Polling disabled. No new readings are being collected.");
  text("polling-help", ready
    ? "Interval: 30-3,600 seconds; default: 45. Saving an interval does not start polling."
    : "Save your API key, account number and selected meter in Settings before starting polling.");
  element<HTMLInputElement>("live-interval").value = String(config.poll_interval_seconds);
  element<HTMLInputElement>("enabled").checked = config.polling_enabled;
  element<HTMLInputElement>("interval").value = String(config.poll_interval_seconds);
  fillRecovery();
  if (recoveryHealth && recoveryConnected && !recoveryBusy) updateRecovery(recoveryHealth);
}
async function savePolling(update: { polling_enabled?: boolean; poll_interval_seconds?: number }): Promise<void> {
  clearMessage();
  const current = generation;
  const buttons = document.querySelectorAll<HTMLButtonElement>("#polling button, #settings-save");
  for (const button of buttons) button.disabled = true;
  try {
    const saved = await api<Config>("/api/config", { expected_revision: config.revision, ...update });
    if (current !== generation) return;
    config = saved;
  } catch (error) {
    if (current === generation) showError(error);
  } finally {
    if (current === generation) {
      for (const button of buttons) button.disabled = false;
      fillPolling();
    }
  }
}
function fillSettings(): void {
  element<HTMLInputElement>("account").value = config.account_number ?? "";
  element<HTMLInputElement>("key").value = "";
  element<HTMLInputElement>("clear-key").checked = false;
  fillPolling();
  element<HTMLInputElement>("timezone").value = config.display_timezone;
  deviceOptions(element<HTMLSelectElement>("device"), config.devices, config.active_device_id);
  deviceOptions(element<HTMLSelectElement>("history-device"), config.devices, config.active_device_id);
  text("key-status", config.has_api_key ? "API key saved. Leave blank to keep it." : "No API key saved.");
  element<HTMLInputElement>("ha-url").value = config.home_assistant_url ?? "";
  element<HTMLInputElement>("ha-entity").value = config.home_assistant_entity ?? "";
  element<HTMLInputElement>("ha-token").value = "";
  element<HTMLInputElement>("ha-enabled").checked = config.home_assistant_enabled;
  element<HTMLInputElement>("tariff-enabled").checked = config.tariff_pricing_enabled;
  element<HTMLInputElement>("ev-savings-from").value = config.ev_savings_from ?? "";
  element<HTMLButtonElement>("tariff-fetch").disabled = !config.has_api_key || !config.account_number || !config.active_device_id;
  element<HTMLInputElement>("ha-clear-token").checked = false;
  text("ha-token-status", config.has_home_assistant_token ? "Token saved. Leave blank to keep it." : "No token saved.");
  const evToggle = element<HTMLInputElement>("analysis-basis");
  evToggle.disabled = !config.home_assistant_url || !config.home_assistant_entity;
  if (evToggle.disabled) evToggle.checked = false;
}
function draft(): Record<string, unknown> {
  const data: Record<string, unknown> = {
    account_number: element<HTMLInputElement>("account").value.trim(),
    active_device_id: element<HTMLSelectElement>("device").value || null,
  };
  const key = element<HTMLInputElement>("key").value;
  if (key) data.api_key = key;
  return data;
}
function localDate(date: Date): string {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
}
function setWindow(from: Date, to: Date): void {
  viewRange = { from, to };
  element<HTMLInputElement>("from").value = localDate(from);
  element<HTMLInputElement>("to").value = localDate(to);
  element<HTMLButtonElement>("next-window").disabled =
    to.getTime() + (to.getTime() - from.getTime()) > Date.now();
  text("window-mode", followLatest ? "Latest window" : "Fixed window");
}
function setRange(minutes: number): void {
  selectedMinutes = minutes;
  followLatest = true;
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-minutes]")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.minutes) === minutes));
  }
  const end = new Date();
  setWindow(new Date(end.getTime() - minutes * 60000), end);
}
function setCustomWindow(from: Date, to: Date): void {
  selectedMinutes = 0; followLatest = false;
  setWindow(from, to);
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-minutes]")) button.setAttribute("aria-pressed", "false");
  clearMessage(); void refresh();
}
function shiftWindow(direction: number): void {
  const span = viewRange.to.getTime() - viewRange.from.getTime();
  const to = new Date(viewRange.to.getTime() + direction * span);
  const from = new Date(viewRange.from.getTime() + direction * span);
  if (span <= 0 || from.getTime() < 0 || to.getTime() > Date.now()) return;
  followLatest = false;
  setWindow(from, to);
  clearMessage(); void refresh();
}
function fillAutoRefresh(): void {
  element<HTMLInputElement>("graph-auto-refresh").checked = autoRefresh;
  text("graph-refresh-status", autoRefresh
    ? "Graph auto-refresh is on. Dragging or window arrows keep selected dates fixed; presets return to the latest window."
    : "Graph auto-refresh is off. Live readings and meter polling continue unchanged; use Refresh graph to update this view.");
}
async function refresh(automatic = false, attempt = 0): Promise<void> {
  if (dailyVisible) return;
  if (automatic && pending && attempt === 0) return;
  analysisDirty = false;
  recoveryRange = undefined; fillRecovery();
  if (!automatic) {
    for (const id of ["coverage", "energy", "peak", "above-6kw-time", "above-6kw-energy", "above-6kw-excess", "above-6kw-load-cost", "above-6kw-excess-cost"]) text(id, "--");
    text("pricing-note", "");
    element("thresholds").replaceChildren(); element("days").replaceChildren(); chart?.clear();
    showCharging(); text("off-peak-status", "");
  }
  const selected = element<HTMLSelectElement>("history-device").value;
  if (!selected) { text("analysis-status", "Configure a meter to begin collecting history."); return; }
  pending?.abort();
  const controller = new AbortController();
  pending = controller;
  pendingAutomatic = automatic;
  const current = generation;
  try {
    const { from: start, to: end } = viewRange;
    const query = new URLSearchParams({ from: start.toISOString(), to: end.toISOString(), device_id: selected });
    const excludeEV = element<HTMLInputElement>("analysis-basis").checked;
    if (excludeEV) query.set("exclude_ev", "true");
    text("energy-label", excludeEV ? "Estimated non-EV import" : "Estimated import");
    text("peak-label", excludeEV ? "Derived non-EV peak" : "Sampled peak");
    text("peak-hint", excludeEV ? "Supported readings outside charging transitions" : "Highest observed reading");
    text("coverage-hint", excludeEV ? "Joint meter / Ohme, minus transition gaps" : "Within the selected window");
    text("above-6kw-hint", excludeEV ? "Observed within the selected window, excluding EV" : "Observed within the selected window");
    text("inverter-window", excludeEV ? "EXCLUDING EV / SELECTED WINDOW" : "SELECTED WINDOW");
    text("days-label", excludeEV ? "Daily derived non-EV peaks" : "Daily sampled peaks");
    text("analysis-basis-note", excludeEV
      ? "EV power is subtracted before calculating these estimates. The minute before and after recorded charging starts/stops is unknown, reducing coverage rather than guessing household demand. Missing Ohme data is also unknown, not zero. Live demand remains whole-meter grid import. EV exclusion requires retained raw history."
      : "Analytics include all grid import, including EV charging. Turn on Exclude EV charging after configuring Home Assistant.");
    text("analysis-status", "Calculating observed demand...");
    const storage = await api<StorageStatus>(`/api/storage?device_id=${encodeURIComponent(selected)}`, undefined, controller.signal);
    const archived = storage.retention && start.getTime() * 1000 < storage.retention.five_before_us;
    const timezone = archived ? storage.retention?.archive_timezone ?? config.display_timezone : config.display_timezone;
    const summary = await api<Summary>(`/api/analytics/summary?${query}&timezone=${encodeURIComponent(timezone)}`, undefined, controller.signal);
    const history = await api<History>(`/api/analytics/history?${query}&interval=auto&max_points=${archived ? 4000 : 1500}`, undefined, controller.signal);
    let peaks: DailyPeaks | undefined;
    let peakError = "";
    try {
      peaks = await api<DailyPeaks>(`/api/analytics/daily-peaks?${query}&timezone=${encodeURIComponent(timezone)}`, undefined, controller.signal);
    } catch (error) {
      if (controller.signal.aborted || error instanceof ApiError && error.status === 401) throw error;
      peakError = error instanceof Error ? error.message : "Daily peaks could not be loaded.";
    }
    let rates: TariffStatus["rates"] = [];
    let tariff: TariffStatus | undefined;
    let tariffNote = "";
    try {
      tariff = await api<TariffStatus>(`/api/tariff?${query}`, undefined, controller.signal);
      rates = tariff.rates;
      tariffNote = tariff.adjustment_enabled
        ? [
          "Shading distinguishes scheduled off-peak and estimated EV bonus half-hours. Unresolved slots are not assumed to be peak-priced.",
          ...tariff.warnings,
        ].join(" ")
        : rates.length
        ? "Off-peak shading uses cached household night-rate intervals only; missing tariff periods are not inferred."
        : "No cached household tariff intervals cover this meter/window. Fetch pricing in Settings to identify off-peak slots.";
    } catch (error) {
      if (controller.signal.aborted || error instanceof ApiError && error.status === 401) throw error;
      tariffNote = `Off-peak indicators unavailable: ${error instanceof Error ? error.message : "Tariff request failed."}`;
    }
    if (current !== generation || controller.signal.aborted) return;
    const revision = summary.pricing.pricing_revision;
    if (tariff && (tariff.adjustment_enabled || revision) && (
      summary.data_version !== history.data_version
      || revision && (tariff.pricing_revision !== revision
        || peaks?.daily_peaks.some(day => day.pricing.pricing_revision && day.pricing.pricing_revision !== revision))
    )) {
      if (attempt < 2) { await refresh(automatic, attempt + 1); return; }
      throw new Error("Charging evidence changed during analysis. Refresh to load consistent costs and tariff shading.");
    }
    if (start.getTime() >= Math.max(
      Date.now() - Math.min(storage.policy.raw_days, 31) * 86400000 - 60000,
      (storage.retention?.raw_before_us ?? 0) / 1000,
    ) && start.getTime() < Date.now() - 180000) {
      recoveryRange = { from: start.toISOString(), to: end.toISOString(), device_id: selected };
    }
    fillRecovery();
    const above6kw = summary.thresholds.find(value => value.threshold_w === 6000);
    if (!above6kw) throw new Error("Analysis is missing the 6 kW threshold.");
    text("coverage", `${number(summary.coverage_pct)}%`);
    text("energy", `${number(summary.import_energy_kwh)} kWh`);
    text("peak", summary.sampled_peak_w === null ? "Unavailable" : `${number(summary.sampled_peak_w / 1000)} kW`);
    text("above-6kw-time", summary.observed_seconds > 0 ? duration(above6kw.seconds_above) : "Unavailable");
    text("above-6kw-energy", above6kw.energy_when_above_kwh === null ? "Unavailable" : `${number(above6kw.energy_when_above_kwh, 3)} kWh`);
    text("above-6kw-excess", above6kw.excess_energy_kwh === null ? "Unavailable" : `${number(above6kw.excess_energy_kwh, 3)} kWh`);
    const loadCost = summary.pricing.enabled ? money(summary.pricing.energy_when_above_gbp) : "Pricing disabled";
    const excessCost = summary.pricing.enabled ? money(summary.pricing.excess_energy_gbp) : "Pricing disabled";
    text("above-6kw-load-cost", loadCost);
    text("above-6kw-excess-cost", excessCost);
    text("pricing-note", [
      summary.pricing.warning,
      summary.pricing.price_coverage_pct !== null ? `Applicable prices cover ${number(summary.pricing.price_coverage_pct)}% of observed time above 6 kW.` : "",
      "Costs cover observed energy only, not missing telemetry. Exceeding 6 kW does not itself incur an extra Octopus charge.",
    ].filter(Boolean).join(" "));
    text("analysis-status", `${number(summary.observed_seconds / 3600)} observed hours of ${number(summary.requested_seconds / 3600)}. Chart buckets: ${history.interval_seconds / 60} min. Calendar days: ${timezone}. Range: ${new Date(summary.from).toLocaleString()} - ${new Date(summary.to).toLocaleString()}. Retention: raw ${storage.policy.raw_days} days, five-minute ${storage.policy.five_minute_days} days, daily ${storage.policy.daily_days} days. ${[...new Set([...summary.warnings, ...history.warnings])].join(". ")}`);
    const thresholdBody = element<HTMLTableSectionElement>("thresholds");
    thresholdBody.replaceChildren();
    const row = thresholdBody.insertRow();
    for (const cell of [
      `${above6kw.threshold_w / 1000} kW`, `${number(above6kw.time_above_pct)}%`,
      `${number(above6kw.energy_when_above_kwh, 3)} kWh`, `${number(above6kw.excess_energy_kwh, 3)} kWh`,
      loadCost, excessCost,
    ]) row.insertCell().textContent = cell;
    const days = element<HTMLTableSectionElement>("days"); days.replaceChildren();
    text("days-status", peaks
      ? `Five calendar days ending at the viewed window, newest first. Calendar timezone: ${peaks.timezone}. The final day may be partial. Costs cover observed energy only; excess cost prices only the portion above 6 kW. ${[...new Set([...peaks.warnings, ...peaks.daily_peaks.map(day => day.pricing.warning)])].join(" ")}`
      : `Daily peaks unavailable: ${peakError}`);
    for (const day of [...peaks?.daily_peaks ?? []].reverse()) {
      const row = days.insertRow();
      const link = document.createElement("a");
      link.href = dayLink(day.date, selected, peaks!.timezone, excludeEV);
      link.textContent = day.date;
      link.className = "day-date-link";
      link.setAttribute("aria-label", `View analysis for ${day.date}`);
      link.onclick = () => { overviewScroll = window.scrollY; };
      row.insertCell().append(link);
      for (const value of [
        day.sampled_peak_w === null ? "Unavailable" : `${number(day.sampled_peak_w / 1000)} kW`,
        `${number(day.coverage_pct)}%`,
      ]) row.insertCell().textContent = value;
      const cell = row.insertCell();
      cell.textContent = day.pricing.enabled ? money(day.pricing.excess_energy_gbp) : "Pricing disabled";
      cell.title = [
        day.pricing.warning,
        day.pricing.price_coverage_pct !== null ? `Applicable prices cover ${number(day.pricing.price_coverage_pct)}% of observed time above 6 kW.` : "",
      ].filter(Boolean).join(" ");
    }
    text("off-peak-status", tariffNote);
    text("ev-chart-status", history.series.mean_ev_w.some(value => value !== null)
      ? "EV charging shows recorded Home Assistant mean power in both analysis views. Incomplete EV buckets are gaps, not zero."
      : "EV charging line unavailable for this meter/window. Configure Home Assistant for the active meter and select retained EV history.");
    showCharging(tariff);
    chart?.update(history, rates, tariff?.charging, tariff?.unresolved);
    overviewLoaded = true;
    lastRefresh = Date.now();
  } catch (error) {
    if (!controller.signal.aborted && current === generation) {
      text("analysis-status", "Analysis could not be loaded.");
      showError(error);
    }
  } finally {
    if (pending === controller) { pending = undefined; pendingAutomatic = false; }
  }
}
function connect(): void {
  stream?.close();
  stream = new EventSource("/api/telemetry/live");
  stream.onopen = () => {
    dailyPage?.connection(true);
    recoveryConnected = true;
    text("stream-health", "Dashboard connected");
  };
  stream.onerror = async () => {
    dailyPage?.connection(false);
    recoveryConnected = false;
    text("stream-health", "Reconnecting to dashboard...");
    if (recoveryPending || recoveryBusy) showRecovery("disconnected",
      "Live status updates are disconnected, so recovery progress is unknown. The server may still be working; this does not mean recovery has finished. Reconnecting...");
    try { await session(); } catch (error) { showError(error); }
  };
  stream.addEventListener("snapshot", (event: MessageEvent<string>) => {
    try {
      const snapshot: Snapshot = JSON.parse(event.data);
      dailyPage?.update(snapshot);
      const demand = snapshot.reading?.demand_w ?? null;
      text("demand", demand === null ? "--" : number(Math.abs(demand) >= 1000 ? demand / 1000 : demand));
      text("demand-unit", demand !== null && Math.abs(demand) >= 1000 ? "kW" : "W");
      const measured = snapshot.reading ? new Date(snapshot.reading.read_at).toLocaleString() : "No reading yet";
      text("read-at", `Last measurement: ${measured}`);
      const h = snapshot.health;
      recoveryConnected = true;
      const recoveryFinished = recoveryPending && h.recovery_state === "complete"
        && snapshot.device_id === config.active_device_id;
      recoveryPending = h.recovery_pending;
      recoveryHealth = h;
      if (!recoveryBusy) updateRecovery(h);
      const ev = snapshot.home_assistant;
      text("home-assistant-health", ev
        ? [
          `Ohme via Home Assistant: ${ev.state.replaceAll("_", " ")}`,
          ev.power_w !== null ? `${number(ev.power_w / 1000)} kW` : "",
          ev.reported_at ? `Reported: ${new Date(ev.reported_at).toLocaleTimeString()}` : "",
          ev.error_code === "home_assistant_freshness_access_denied"
            ? "Freshness check denied; verify the Home Assistant token and administrator access, then save settings."
            : ev.error_code?.replaceAll("_", " ") ?? "",
          ev.recovery_cursor_at ? `Recovering history from ${new Date(ev.recovery_cursor_at).toLocaleString()}` : "",
        ].filter(Boolean).join(" | ")
        : "Ohme via Home Assistant: disabled");
      text("collector-health", `${h.polling_state.replaceAll("_", " ")} / ${h.telemetry_state} / storage ${h.storage_state}`);
      element("collector-health").className = h.telemetry_state === "fresh" && h.polling_state === "running" && h.storage_state === "ok" ? "healthy" : "warning";
      text("upstream-health", [
        h.last_success_at ? `API contact: ${new Date(h.last_success_at).toLocaleTimeString()}` : "No successful API contact",
        h.last_attempt_at ? `Last attempt: ${new Date(h.last_attempt_at).toLocaleTimeString()}` : "",
        h.last_stored_at ? `Last write: ${new Date(h.last_stored_at).toLocaleTimeString()}` : "",
        h.error_code ?? "", h.next_attempt_at ? `Next attempt: ${new Date(h.next_attempt_at).toLocaleTimeString()}` : "",
        h.recovery_pending ? `Recovering history: ${h.recovery_cursor_at ?? "pending"} to ${h.recovery_target_at ?? "pending"}; live and recovery requests alternate.` : "",
        h.recovery_error ? `Historical recovery paused: ${h.recovery_error}. Live collection continues; save settings or test saved connectivity to retry.` : "",
        h.recovery_gap_count ? `${h.recovery_gap_count} historical windows have missing or unavailable samples.` : "",
        h.storage_paused ? "Collection paused: storage limit or free-space reserve reached." : "",
        h.database_bytes != null ? `Storage: ${number(h.database_bytes / 1048576)} / ${number(h.storage_budget_bytes / 1048576)} MiB` : "",
        h.disk_free_bytes != null ? `Disk free: ${number(h.disk_free_bytes / 1048576)} MiB` : "",
        h.low_disk ? "Low disk space. Free space or extend storage." : "",
        h.maintenance_error ? `Maintenance error: ${h.maintenance_error}` : "",
        h.last_maintenance_at ? `Maintenance: ${new Date(h.last_maintenance_at).toLocaleString()}` : "",
      ].filter(Boolean).join(" | "));
      if (snapshot.device_id !== config.active_device_id) {
        void api<Config>("/api/config").then(value => {
          config = value;
          if (autoRefresh) { fillSettings(); return refresh(); }
          text("graph-refresh-status", "The active meter changed. Graph remains paused; refresh to load the updated settings.");
        }).catch(showError);
      }
      const versionChanged = snapshot.data_version !== lastVersion;
      lastVersion = snapshot.data_version;
      analysisDirty ||= versionChanged || recoveryFinished;
      if (!dailyVisible && autoRefresh && !pending && (recoveryFinished || (analysisDirty && Date.now() - lastRefresh > 60000)) && !document.hidden) {
        if (followLatest && selectedMinutes) setRange(selectedMinutes);
        void refresh(true);
      }
    } catch (error) { showError(error); }
  });
}
function route(): void {
  const settings = location.hash === "#settings";
  const wasDaily = dailyVisible;
  dailyVisible = location.hash.startsWith("#day/");
  element("overview").hidden = settings || dailyVisible;
  element("settings-page").hidden = !settings;
  element("daily-page").hidden = !dailyVisible;
  if (dailyVisible) {
    if (!wasDaily) { overviewInterrupted = !!pending || !overviewLoaded; pending?.abort(); }
    clearMessage();
    void dailyPage?.open(location.hash, !wasDaily).catch(showError);
    window.scrollTo(0, 0);
  } else {
    dailyPage?.hide();
    if (wasDaily && !settings) {
      requestAnimationFrame(() => { window.scrollTo(0, overviewScroll); });
    }
    if (!settings && overviewInterrupted) { overviewInterrupted = false; void refresh(); }
  }
  for (const link of document.querySelectorAll<HTMLAnchorElement>(".sidebar nav a")) {
    if (link.hash === "#settings") link.setAttribute("aria-current", String(settings));
    else link.removeAttribute("aria-current");
  }
  if (settings) window.scrollTo(0, 0);
  else if (!dailyVisible && !wasDaily && location.hash.length > 1) document.getElementById(location.hash.slice(1))?.scrollIntoView();
}
async function renderDashboard(): Promise<void> {
  cleanup();
  config = await api<Config>("/api/config");
  lastVersion = -1;
  root.innerHTML = `<div id="dashboard"><a class="skip-link" href="#overview">Skip to dashboard</a>
    <aside class="sidebar"><a class="brand-mark" href="#overview" aria-label="Home Energy overview"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m13 2-9 12h7l-1 8 10-13h-8l1-7Z"/></svg></a>
    <nav aria-label="Dashboard navigation">
    <a href="#overview" aria-label="Overview" title="Overview"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/></svg><span>Overview</span></a>
    <a href="#history" aria-label="Demand history" title="Demand history"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3v18h18M6 15l4-5 4 3 6-8"/></svg><span>History</span></a>
    <a href="#inverters" aria-label="Inverter sizing" title="Inverter sizing"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5"/></svg><span>Inverters</span></a>
    <a href="#settings" aria-label="Settings" title="Settings"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 17h16M9 3v8M16 13v8"/></svg><span>Settings</span></a>
    ${authenticationRequired ? '<button id="logout" class="nav-button" aria-label="Sign out" title="Sign out"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg></button>' : ""}
    </nav></aside><div class="workspace"><p id="message" role="alert"></p><main id="overview">
    <div class="page-heading"><div><p class="eyebrow">HOME ENERGY / CONTROL ROOM</p><h1>Home Energy<span class="accent">_</span></h1></div><span class="local-badge">LOCAL / RASPBERRY PI</span></div>
    ${authenticationRequired ? "" : '<p class="access-warning" role="note">Login disabled: anyone who can reach this dashboard can view data and change settings. Trusted LAN only.</p>'}
    <section class="metrics" aria-label="Energy overview"><article class="live-card"><span class="channel">CH.01</span><p>Live grid demand</p><div class="reading"><strong id="demand">--</strong> <span id="demand-unit">W</span></div><p id="read-at">No reading yet</p></article>
    <article><span class="channel">CH.02</span><p id="energy-label">Estimated import</p><strong id="energy">--</strong><small>Within the selected window</small></article>
    <article><span class="channel">CH.03</span><p id="peak-label">Sampled peak</p><strong id="peak">--</strong><small id="peak-hint">Highest observed reading</small></article>
    <article><span class="channel">CH.04</span><p>Observed coverage</p><strong id="coverage">--</strong><small id="coverage-hint">Within the selected window</small></article>
    <article class="threshold-card"><span class="channel">CH.05</span><p>Time above 6 kW</p><strong id="above-6kw-time">--</strong>
    <small>Energy while above: <span id="above-6kw-energy">--</span><br>Excess beyond 6 kW: <span id="above-6kw-excess">--</span><br>Cost while above: <span id="above-6kw-load-cost">--</span><br>Cost of excess: <span id="above-6kw-excess-cost">--</span><br><span id="above-6kw-hint">Observed within the selected window</span></small></article></section>
    <div class="dashboard-grid"><section class="collection-panel" aria-labelledby="collection-title"><h2 id="collection-title">Collection health</h2><p id="collector-health">Connecting...</p><p id="stream-health"></p><small id="upstream-health"></small>
    <p id="home-assistant-health">Ohme via Home Assistant: disabled</p>
    <form id="polling" class="polling-controls" aria-label="Live polling controls">
    <div class="section-heading"><h3>Polling</h3><button id="toggle-polling" type="button">Start polling</button></div>
    <p id="polling-status" role="status"></p>
    <div class="range"><label>Check every (seconds)<input id="live-interval" type="number" min="30" max="3600" step="1" required aria-describedby="polling-help"></label><button type="submit">Save interval</button></div>
    <p id="polling-help" class="note"></p></form></section>
    <section id="history"><div class="section-heading"><div><p class="eyebrow">OBSERVED DEMAND</p><h2>Demand, in detail</h2></div><select id="history-device" aria-label="Historical meter"></select></div>
    <div class="graph-controls">
    <label class="check switch"><input id="analysis-basis" type="checkbox" role="switch" aria-describedby="analysis-basis-note">Exclude EV charging</label>
    <label class="check switch"><input id="graph-auto-refresh" type="checkbox" role="switch" aria-describedby="graph-refresh-status">Auto-refresh graph</label>
    <button id="refresh-graph" class="secondary" type="button">Refresh graph</button></div>
    <p id="analysis-basis-note" class="note"></p>
    <p id="graph-refresh-status" class="note" role="status"></p>
    <div class="presets"><button data-minutes="5">5 min</button><button data-minutes="15">15 min</button><button data-minutes="30">30 min</button><button data-minutes="60">1 hour</button><button data-minutes="720">12 hours</button><button data-minutes="1440">24 hours</button><button data-minutes="10080">7 days</button><button data-minutes="43200">30 days</button></div>
    <div class="window-navigation" role="group" aria-label="Time window navigation">
    <button id="previous-window" class="secondary" type="button" aria-label="Previous time window">&lt;</button>
    <span id="window-mode" class="note"></span>
    <button id="next-window" class="secondary" type="button" aria-label="Next time window">&gt;</button></div>
    <form id="range" class="range"><label>From (browser local time)<input id="from" type="datetime-local" step="1" required></label>
    <label>To (browser local time)<input id="to" type="datetime-local" step="1" required></label><button>Apply custom range</button></form>
    <p id="analysis-status" role="status"></p>
    <button id="recover-history" class="secondary" type="button" aria-describedby="recovery-help recovery-status" disabled>Recover missing readings</button>
    <p id="recovery-help" class="note"></p>
    <div id="recovery-feedback" class="recovery-feedback" role="status" aria-live="polite" aria-atomic="true" hidden>
    <div class="recovery-heading"><span id="recovery-spinner" class="recovery-spinner" aria-hidden="true" hidden></span><strong id="recovery-state"></strong></div>
    <p id="recovery-status" class="note"></p></div>
    <div id="chart" aria-label="Demand history chart"></div>
    <div class="chart-indicators" aria-label="Visible chart indicators"><span id="off-peak-key" class="off-peak-key" hidden>Household off-peak slot</span><span id="bonus-key" class="bonus-key" hidden>Estimated EV bonus slot</span><span id="charging-key" class="charging-key" hidden>Observed EV charging</span><span id="unresolved-key" class="unresolved-key" hidden>Unresolved rate</span><span id="threshold-key" class="threshold-key" hidden>6 kW threshold</span></div>
    <p id="off-peak-status" class="note"></p>
    <p id="ev-chart-status" class="note"></p>
    <details id="charging-details" hidden><summary>EV charging and bonus-rate periods</summary><p id="charging-details-note" class="note"></p><div class="table-scroll"><table><thead><tr><th scope="col">Evidence / rate</th><th scope="col">From</th><th scope="col">To</th><th scope="col">Household rate including VAT</th></tr></thead><tbody id="charging-periods"></tbody></table></div></details>
    <p class="note">Gaps are unknown, not zero. Incomplete bucket means are not drawn. Chart timestamps use your browser timezone. Peaks are sampled; short surges may be missed.</p>
    </section></div><section id="inverters"><div class="section-heading"><div><p class="eyebrow">REFERENCE COMPARISON</p><h2>Explore inverter sizing</h2></div><span id="inverter-window" class="local-badge">SELECTED WINDOW</span></div>
    <div class="table-scroll"><table><thead><tr><th scope="col">Inverter rating</th><th scope="col">Observed time above</th><th scope="col">Energy while above</th><th scope="col">Excess energy</th><th scope="col">Estimated cost while above</th><th scope="col">Estimated excess cost</th></tr></thead><tbody id="thresholds"></tbody></table></div>
    <p id="pricing-note" class="note"></p>
    <p class="note">Energy while above includes the whole load during those periods. Excess energy includes only demand beyond the rating. Grid import can understate household demand when solar or a battery supplies power. The 6 kW comparison is not a G98/G99 compliance assessment.</p>
    <div id="daily-peaks" aria-labelledby="days-label"><h3 id="days-label">Daily sampled peaks</h3><p id="days-status" class="note"></p><div class="table-scroll"><table aria-describedby="days-status"><thead><tr><th scope="col">Local date</th><th scope="col">Peak demand</th><th scope="col">Coverage</th><th scope="col">Estimated excess cost above 6 kW</th></tr></thead><tbody id="days"></tbody></table></div></div></section>
    </main><main id="settings-page" hidden><section id="settings-section"><div class="page-heading"><div><p class="eyebrow">CONNECTION &amp; COLLECTION</p><h1>Settings</h1></div></div><form id="settings"><div class="settings-grid">
    <label>Octopus account number<input id="account" autocomplete="off" maxlength="64" placeholder="A-..."></label>
    <label>Electricity meter<select id="device"></select><small>Test account connectivity to discover meters.</small></label>
    <label>Replace Octopus API key<input id="key" type="password" autocomplete="new-password" maxlength="512"><small id="key-status"></small></label>
    <label>Polling interval (seconds)<input id="interval" type="number" min="30" max="3600" required></label>
    <label>Display / calendar timezone<input id="timezone" required></label>
    <div><label class="check"><input id="enabled" type="checkbox">Enable polling</label><label class="check"><input id="clear-key" type="checkbox">Clear saved API key (disable polling and tariff pricing first)</label></div></div>
    <h2 class="settings-subheading">Tariff pricing</h2>
    <label class="check"><input id="tariff-enabled" type="checkbox">Estimate costs using Octopus tariff pricing</label>
    <p class="note">Save your Octopus account and meter first, then fetch their household import rates. Enable and save to refresh prices automatically every six hours, independently of meter polling. Prices include VAT and exclude standing charges. Both the whole load while above 6 kW and only the excess energy are priced at their applicable time-based rates.</p>
    <label>Prioritise Savings start date (UK local date)<input id="ev-savings-from" type="date" min="1970-01-01" aria-describedby="ev-savings-help"></label>
    <p id="ev-savings-help" class="note">Optional: entering a date confirms all Ohme charging from that date uses Intelligent Octopus Go Prioritise Savings, with no Max/Boost charging. Recorded positive charger power then earns an estimated whole-half-hour household off-peak rate, in both analysis modes. Leave blank to use published schedules only. Clear this assumption if charging policy or charger changes. Missing EV history leaves daytime costs unavailable where eligibility is unknown. Fetch pricing again after upgrading to cache dated daytime and night prices. Estimates are not supplier-confirmed billing or battery savings.</p>
    <button id="tariff-fetch" class="secondary" type="button">Fetch Octopus pricing</button>
    <p id="tariff-status" role="status">Loading tariff pricing...</p>
    <div class="table-scroll"><table><caption>Known household rates in the next 24 hours (browser local times; future bonus slots are not guaranteed)</caption><thead><tr><th scope="col">Band</th><th scope="col">Unit rate including VAT</th><th scope="col">From</th><th scope="col">To</th></tr></thead><tbody id="tariff-rates"></tbody></table></div>
    <h2 class="settings-subheading">Ohme via Home Assistant</h2><div class="settings-grid">
    <label><span id="ha-url-label">Home Assistant URL</span><input id="ha-url" aria-labelledby="ha-url-label" aria-describedby="ha-url-help" type="url" maxlength="512" placeholder="http://homeassistant.local:8123"><small id="ha-url-help">Use an address reachable from this app's container. HTTPS is recommended; HTTP sends the token unencrypted. Redirects are not followed.</small></label>
    <label><span id="ha-entity-label">Ohme power entity</span><input id="ha-entity" aria-labelledby="ha-entity-label" aria-describedby="ha-entity-help" maxlength="255" placeholder="sensor.ohme_home_pro_power"><small id="ha-entity-help">Power sensor in W or kW, recorded in Home Assistant history.</small></label>
    <label><span id="ha-token-label">Replace Home Assistant access token</span><input id="ha-token" aria-labelledby="ha-token-label" aria-describedby="ha-token-status" type="password" autocomplete="new-password" maxlength="4096"><small id="ha-token-status"></small></label>
    <div><label class="check"><input id="ha-enabled" type="checkbox">Collect Ohme power history</label><label class="check"><input id="ha-clear-token" type="checkbox">Clear saved Home Assistant token (disable EV collection first)</label></div></div>
    <p class="note">Collection runs independently every 60 seconds and gradually recovers the configured lookback (default: one day). EV exclusion uses joint meter / Ohme coverage within raw retention, omitting the minute either side of recorded charging starts/stops; it is an estimate, not total household load. Tokens stay in the backend database and private backups. Home Assistant must record this entity.</p>
    <div class="actions"><button id="settings-save" type="submit">Save settings</button><button id="test" class="secondary" type="button">Test connectivity</button><button id="ha-test" class="secondary" type="button">Test Home Assistant</button></div><p class="note">Testing connectivity does not save settings or start polling. Save your meter settings, then use Start polling above or save with Enable polling checked.</p><p id="test-result" role="status"></p><p id="ha-test-result" role="status"></p></form></section>
    </main><main id="daily-page" hidden aria-label="Daily energy analysis"></main><footer>${versionMarkup}Local storage. No third-party dashboard services. Keep backups private: they contain your API key and any saved Home Assistant token.</footer></div></div>`;
  fillSettings(); setRange(1440); fillAutoRefresh();
  void loadTariff();
  dailyPage = new DailyPage(element("daily-page"), () => config, showError);
  window.onhashchange = route; route();
  chart = new DemandChart(element("chart"), visible => {
    element("off-peak-key").hidden = !visible.offPeak;
    element("bonus-key").hidden = !visible.bonus;
    element("charging-key").hidden = !visible.charging;
    element("unresolved-key").hidden = !visible.unresolved;
    element("threshold-key").hidden = !visible.threshold;
  }, setCustomWindow);
  if (authenticationRequired) {
    element("logout").onclick = async () => {
      try { await api("/api/auth/logout", {}); renderLogin(); } catch (error) { showError(error); }
    };
  }
  element<HTMLFormElement>("range").onsubmit = event => {
    event.preventDefault();
    setCustomWindow(new Date(element<HTMLInputElement>("from").value), new Date(element<HTMLInputElement>("to").value));
  };
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-minutes]")) {
    button.onclick = () => { setRange(Number(button.dataset.minutes)); clearMessage(); void refresh(); };
  }
  element("history-device").onchange = () => { clearMessage(); void refresh(); };
  element("analysis-basis").onchange = () => { clearMessage(); void refresh(); };
  element("previous-window").onclick = () => shiftWindow(-1);
  element("next-window").onclick = () => shiftWindow(1);
  element("refresh-graph").onclick = () => {
    if (followLatest && selectedMinutes) setRange(selectedMinutes);
    clearMessage(); void refresh();
  };
  element<HTMLInputElement>("graph-auto-refresh").onchange = () => {
    autoRefresh = element<HTMLInputElement>("graph-auto-refresh").checked;
    if (!autoRefresh && pendingAutomatic) pending?.abort();
    fillAutoRefresh();
    if (autoRefresh) {
      if (followLatest && selectedMinutes) setRange(selectedMinutes);
      clearMessage(); void refresh();
    }
  };
  element<HTMLButtonElement>("recover-history").onclick = async () => {
    if (!recoveryRange) return;
    const current = generation;
    recoveryBusy = true; recoveryRequestError = ""; fillRecovery(); clearMessage();
    showRecovery("requesting", "Requesting historical recovery for the selected range...");
    try {
      const queued = await api<RecoveryStatus>("/api/recovery", {
        ...recoveryRange, expected_revision: config.revision,
      });
      if (current !== generation) return;
      recoveryPending = true;
      if (recoveryHealth) recoveryHealth = {
        ...recoveryHealth, recovery_state: queued.state, recovery_pending: true,
        recovery_active: false, recovery_cursor_at: queued.from,
        recovery_target_at: queued.to, recovery_error: null,
      };
      showRecovery(recoveryConnected ? "queued" : "disconnected",
        `Recovery queued: ${new Date(queued.from).toLocaleString()} to ${new Date(queued.to).toLocaleString()}. Existing pending work is retained; missing readings will be retried in fifteen-minute chunks.${recoveryConnected ? "" : " Live status updates are disconnected; progress is unknown until reconnection."}`);
    } catch (error) {
      if (current === generation) {
        recoveryRequestError = error instanceof Error ? error.message : "Historical recovery could not be requested.";
        if (recoveryHealth && recoveryConnected) updateRecovery(recoveryHealth);
        else if (recoveryPending) showRecovery("disconnected",
          `Recovery request error: ${recoveryRequestError} Existing queued recovery may still be running, but progress is unknown until live status updates reconnect.`);
        else showRecovery("error", recoveryRequestError);
        showError(error);
      }
    } finally {
      if (current === generation) { recoveryBusy = false; fillRecovery(); }
    }
  };
  element<HTMLButtonElement>("toggle-polling").onclick = () => {
    void savePolling({ polling_enabled: !config.polling_enabled });
  };
  element<HTMLFormElement>("polling").onsubmit = event => {
    event.preventDefault();
    void savePolling({ poll_interval_seconds: Number(element<HTMLInputElement>("live-interval").value) });
  };
  element<HTMLFormElement>("settings").onsubmit = async event => {
    event.preventDefault(); clearMessage();
    const data: Record<string, unknown> = { ...draft(), expected_revision: config.revision,
      polling_enabled: element<HTMLInputElement>("enabled").checked,
      poll_interval_seconds: Number(element<HTMLInputElement>("interval").value),
      display_timezone: element<HTMLInputElement>("timezone").value.trim(),
      home_assistant_url: element<HTMLInputElement>("ha-url").value.trim() || null,
      home_assistant_entity: element<HTMLInputElement>("ha-entity").value.trim() || null,
      home_assistant_enabled: element<HTMLInputElement>("ha-enabled").checked,
      tariff_pricing_enabled: element<HTMLInputElement>("tariff-enabled").checked,
      ev_savings_from: element<HTMLInputElement>("ev-savings-from").value || null,
    };
    if (element<HTMLInputElement>("clear-key").checked) data.api_key = null;
    const homeToken = element<HTMLInputElement>("ha-token").value;
    if (homeToken) data.home_assistant_token = homeToken;
    if (element<HTMLInputElement>("ha-clear-token").checked) data.home_assistant_token = null;
    try {
      config = await api<Config>("/api/config", data);
      fillSettings(); text("test-result", "Settings saved.");
      await loadTariff();
      await refresh();
    } catch (error) { showError(error); }
  };
  element<HTMLButtonElement>("tariff-fetch").onclick = async () => {
    const button = element<HTMLButtonElement>("tariff-fetch"); button.disabled = true;
    clearMessage(); text("tariff-status", "Fetching pricing for the saved Octopus account and meter...");
    try {
      showTariff(await api<TariffStatus>("/api/tariff/refresh", { expected_revision: config.revision }));
      await refresh();
    } catch (error) {
      showError(error);
      text("tariff-status", error instanceof Error ? error.message : "Tariff pricing refresh failed.");
    } finally { button.disabled = false; }
  };
  element<HTMLButtonElement>("ha-test").onclick = async () => {
    const button = element<HTMLButtonElement>("ha-test"); button.disabled = true;
    clearMessage(); text("ha-test-result", "Testing Home Assistant without saving...");
    const data: Record<string, unknown> = {
      home_assistant_url: element<HTMLInputElement>("ha-url").value.trim() || null,
      home_assistant_entity: element<HTMLInputElement>("ha-entity").value.trim() || null,
    };
    const token = element<HTMLInputElement>("ha-token").value;
    if (token) data.home_assistant_token = token;
    try {
      const result = await api<{ power_w: number | null; fresh: boolean; history_available: boolean; warnings: string[] }>("/api/home-assistant/test", data);
      text("ha-test-result", [
        `Connected. Power: ${number(result.power_w)} W. Recent history: ${result.history_available ? "available" : "unavailable"}.`,
        ...result.warnings,
      ].join(" "));
    } catch (error) { showError(error); text("ha-test-result", "Home Assistant test did not succeed."); }
    finally { button.disabled = false; }
  };
  element<HTMLButtonElement>("test").onclick = async () => {
    const button = element<HTMLButtonElement>("test"); button.disabled = true;
    clearMessage(); text("test-result", "Testing saved or draft settings (without saving)...");
    try {
      const result = await api<{ devices: Device[]; telemetry_available: boolean; selection_required: boolean; warnings: string[] }>("/api/config/test", draft());
      const selected = element<HTMLSelectElement>("device").value;
      deviceOptions(element<HTMLSelectElement>("device"), result.devices, selected);
      const message = result.telemetry_available ? "Connected; usable telemetry returned." : result.selection_required ? "Account connected. Select an electricity meter and test again." : "Meter accessible, but no usable readings.";
      text("test-result", [message, ...result.warnings].join(" "));
    } catch (error) { showError(error); text("test-result", "Connectivity test did not succeed."); }
    finally { button.disabled = false; }
  };
  connect();
  await refresh();
}

async function start(): Promise<void> {
  try {
    await session();
    await renderDashboard();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      renderLogin();
      return;
    }
    cleanup();
    root.innerHTML = '<main class="login"><h1>Home Energy</h1><p id="message" role="alert"></p><button id="retry">Retry</button></main>';
    showError(error);
    element("retry").onclick = () => { void start(); };
  }
}
void start();
