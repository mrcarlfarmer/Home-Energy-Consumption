import { api, ApiError, authenticationRequired, Config, Device, History, session, Snapshot, Summary } from "./api";
import { DemandChart } from "./charts";
import "./styles.css";

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

function element<T extends HTMLElement = HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing UI element: ${id}`);
  return found as T;
}
function text(id: string, value: string): void { element(id).textContent = value; }
function number(value: number | null, digits = 2): string {
  return value === null ? "Unavailable" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
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
  window.onhashchange = null;
  stream?.close(); stream = undefined;
  chart?.destroy(); chart = undefined;
  pending?.abort(); pending = undefined;
}
function renderLogin(message = ""): void {
  cleanup();
  root.innerHTML = `<main class="login"><p class="eyebrow">YOUR HOME, IN FOCUS</p><h1>Home Energy</h1>
    <p>Private electricity monitoring and inverter sizing.</p>
    <form id="login"><label>Username<input id="username" autocomplete="username" value="admin" maxlength="64" required></label>
    <label>Dashboard password<input id="password" type="password" autocomplete="current-password" required></label>
    <button type="submit">Sign in</button></form><p id="message" role="alert"></p>
    <small>${transportNotice || "Credentials stay on your Raspberry Pi. Use your trusted HTTPS address."}</small></main>`;
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
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function setRange(minutes: number): void {
  selectedMinutes = minutes;
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-minutes]")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.minutes) === minutes));
  }
  const end = new Date();
  element<HTMLInputElement>("from").value = localDate(new Date(end.getTime() - minutes * 60000));
  element<HTMLInputElement>("to").value = localDate(end);
}
async function refresh(): Promise<void> {
  const selected = element<HTMLSelectElement>("history-device").value;
  if (!selected) { text("analysis-status", "Configure a meter to begin collecting history."); return; }
  pending?.abort();
  const controller = new AbortController();
  pending = controller;
  const current = generation;
  try {
    const end = selectedMinutes ? new Date() : new Date(element<HTMLInputElement>("to").value);
    const start = selectedMinutes
      ? new Date(end.getTime() - selectedMinutes * 60000)
      : new Date(element<HTMLInputElement>("from").value);
    const query = new URLSearchParams({ from: start.toISOString(), to: end.toISOString(), device_id: selected });
    text("analysis-status", "Calculating observed demand...");
    const summary = await api<Summary>(`/api/analytics/summary?${query}&timezone=${encodeURIComponent(config.display_timezone)}`, undefined, controller.signal);
    const history = await api<History>(`/api/analytics/history?${query}&interval=auto&max_points=1500`, undefined, controller.signal);
    if (current !== generation || controller.signal.aborted) return;
    text("coverage", `${number(summary.coverage_pct)}%`);
    text("energy", `${number(summary.import_energy_kwh)} kWh`);
    text("peak", summary.sampled_peak_w === null ? "Unavailable" : `${number(summary.sampled_peak_w / 1000)} kW`);
    text("analysis-status", `${number(summary.observed_seconds / 3600)} observed hours of ${number(summary.requested_seconds / 3600)}. Chart buckets: ${history.interval_seconds / 60} min. Calendar days: ${config.display_timezone}.`);
    const thresholdBody = element<HTMLTableSectionElement>("thresholds");
    thresholdBody.replaceChildren();
    for (const value of summary.thresholds) {
      const row = thresholdBody.insertRow();
      for (const cell of [
        `${value.threshold_w / 1000} kW`, `${number(value.time_above_pct)}%`,
        `${number(value.energy_when_above_kwh, 3)} kWh`, `${number(value.excess_energy_kwh, 3)} kWh`,
      ]) row.insertCell().textContent = cell;
    }
    const days = element<HTMLTableSectionElement>("days"); days.replaceChildren();
    for (const day of summary.daily_peaks) {
      const row = days.insertRow();
      for (const value of [
        day.date, day.sampled_peak_w === null ? "Unavailable" : `${number(day.sampled_peak_w / 1000)} kW`,
        `${number(day.coverage_pct)}%`,
      ]) row.insertCell().textContent = value;
    }
    chart?.update(history);
    lastRefresh = Date.now();
  } catch (error) {
    if (!controller.signal.aborted && current === generation) {
      text("analysis-status", "Analysis could not be loaded.");
      showError(error);
    }
  }
}
function connect(): void {
  stream?.close();
  stream = new EventSource("/api/telemetry/live");
  stream.onopen = () => text("stream-health", "Dashboard connected");
  stream.onerror = async () => {
    text("stream-health", "Reconnecting to dashboard...");
    try { await session(); } catch (error) { showError(error); }
  };
  stream.addEventListener("snapshot", (event: MessageEvent<string>) => {
    try {
      const snapshot: Snapshot = JSON.parse(event.data);
      const demand = snapshot.reading?.demand_w ?? null;
      text("demand", demand === null ? "--" : number(Math.abs(demand) >= 1000 ? demand / 1000 : demand));
      text("demand-unit", demand !== null && Math.abs(demand) >= 1000 ? "kW" : "W");
      const measured = snapshot.reading ? new Date(snapshot.reading.read_at).toLocaleString() : "No reading yet";
      text("read-at", `Last measurement: ${measured}`);
      const h = snapshot.health;
      text("collector-health", `${h.polling_state.replaceAll("_", " ")} / ${h.telemetry_state} / storage ${h.storage_state}`);
      element("collector-health").className = h.telemetry_state === "fresh" && h.polling_state === "running" && h.storage_state === "ok" ? "healthy" : "warning";
      text("upstream-health", [
        h.last_success_at ? `API contact: ${new Date(h.last_success_at).toLocaleTimeString()}` : "No successful API contact",
        h.error_code ?? "", h.next_attempt_at ? `Next attempt: ${new Date(h.next_attempt_at).toLocaleTimeString()}` : "",
        h.recovery_incomplete ? "Older outage gaps could not be recovered." : "",
        h.low_disk ? "Low disk space. Raw history is retained; free space or extend storage." : "",
      ].filter(Boolean).join(" | "));
      if (snapshot.device_id !== config.active_device_id) {
        void api<Config>("/api/config").then(value => { config = value; fillSettings(); return refresh(); }).catch(showError);
      }
      if (snapshot.data_version !== lastVersion) {
        lastVersion = snapshot.data_version;
        if (Date.now() - lastRefresh > 60000 && !document.hidden) {
          if (selectedMinutes) setRange(selectedMinutes);
          void refresh();
        }
      }
    } catch (error) { showError(error); }
  });
}
function route(): void {
  const settings = location.hash === "#settings";
  element("overview").hidden = settings;
  element("settings-page").hidden = !settings;
  for (const link of document.querySelectorAll<HTMLAnchorElement>("nav a")) {
    if (link.hash === "#settings") link.setAttribute("aria-current", String(settings));
    else link.removeAttribute("aria-current");
  }
  if (settings) window.scrollTo(0, 0);
  else if (location.hash.length > 1) document.getElementById(location.hash.slice(1))?.scrollIntoView();
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
    ${transportNotice ? `<p class="transport-warning" role="note">${transportNotice}</p>` : ""}
    <section class="metrics" aria-label="Energy overview"><article class="live-card"><span class="channel">CH.01</span><p>Live grid demand</p><div class="reading"><strong id="demand">--</strong> <span id="demand-unit">W</span></div><p id="read-at">No reading yet</p></article>
    <article><span class="channel">CH.02</span><p>Estimated import</p><strong id="energy">--</strong><small>Within the selected window</small></article>
    <article><span class="channel">CH.03</span><p>Sampled peak</p><strong id="peak">--</strong><small>Highest observed reading</small></article>
    <article><span class="channel">CH.04</span><p>Observed coverage</p><strong id="coverage">--</strong><small>Within the selected window</small></article></section>
    <div class="dashboard-grid"><section class="collection-panel" aria-labelledby="collection-title"><h2 id="collection-title">Collection health</h2><p id="collector-health">Connecting...</p><p id="stream-health"></p><small id="upstream-health"></small>
    <form id="polling" class="polling-controls" aria-label="Live polling controls">
    <div class="section-heading"><h3>Polling</h3><button id="toggle-polling" type="button">Start polling</button></div>
    <p id="polling-status" role="status"></p>
    <div class="range"><label>Check every (seconds)<input id="live-interval" type="number" min="30" max="3600" step="1" required aria-describedby="polling-help"></label><button type="submit">Save interval</button></div>
    <p id="polling-help" class="note"></p></form></section>
    <section id="history"><div class="section-heading"><div><p class="eyebrow">OBSERVED DEMAND</p><h2>Demand, in detail</h2></div><select id="history-device" aria-label="Historical meter"></select></div>
    <div class="presets"><button data-minutes="5">5 min</button><button data-minutes="15">15 min</button><button data-minutes="30">30 min</button><button data-minutes="60">1 hour</button><button data-minutes="1440">24 hours</button><button data-minutes="10080">7 days</button><button data-minutes="43200">30 days</button></div>
    <form id="range" class="range"><label>From (browser local time)<input id="from" type="datetime-local" required></label>
    <label>To (browser local time)<input id="to" type="datetime-local" required></label><button>Apply custom range</button></form>
    <p id="analysis-status" role="status"></p>
    <div id="chart" aria-label="Demand history chart"></div>
    <p class="note">Gaps are unknown, not zero. Incomplete bucket means and incomplete 15-minute windows are not drawn. Chart timestamps use your browser timezone. Peaks are sampled; short surges may be missed.</p>
    </section></div><section id="inverters"><div class="section-heading"><div><p class="eyebrow">REFERENCE COMPARISON</p><h2>Explore inverter sizing</h2></div><span class="local-badge">SELECTED WINDOW</span></div>
    <div class="table-scroll"><table><thead><tr><th scope="col">Inverter rating</th><th scope="col">Observed time above</th><th scope="col">Energy while above</th><th scope="col">Excess energy</th></tr></thead><tbody id="thresholds"></tbody></table></div>
    <p class="note">Energy while above includes the whole load during those periods. Excess energy includes only demand beyond the rating. Grid import can understate household demand when solar or a battery supplies power. The 3.68 kW preset is not a G98/G99 compliance assessment.</p>
    <details><summary>Daily sampled peaks</summary><div class="table-scroll"><table><thead><tr><th>Local date</th><th>Peak demand</th><th>Coverage</th></tr></thead><tbody id="days"></tbody></table></div></details></section>
    </main><main id="settings-page" hidden><section id="settings-section"><div class="page-heading"><div><p class="eyebrow">CONNECTION &amp; COLLECTION</p><h1>Settings</h1></div></div><form id="settings"><div class="settings-grid">
    <label>Octopus account number<input id="account" autocomplete="off" maxlength="64" placeholder="A-..."></label>
    <label>Electricity meter<select id="device"></select><small>Test account connectivity to discover meters.</small></label>
    <label>Replace Octopus API key<input id="key" type="password" autocomplete="new-password" maxlength="512"><small id="key-status"></small></label>
    <label>Polling interval (seconds)<input id="interval" type="number" min="30" max="3600" required></label>
    <label>Display / calendar timezone<input id="timezone" required></label>
    <div><label class="check"><input id="enabled" type="checkbox">Enable polling</label><label class="check"><input id="clear-key" type="checkbox">Clear saved API key (disable polling first)</label></div></div>
    <div class="actions"><button id="settings-save" type="submit">Save settings</button><button id="test" class="secondary" type="button">Test connectivity</button></div><p class="note">Testing connectivity does not save settings or start polling. Save your meter settings, then use Start polling above or save with Enable polling checked.</p><p id="test-result" role="status"></p></form></section>
    </main><footer>Local storage. No third-party dashboard services. Keep backups private: they contain your API key.</footer></div></div>`;
  fillSettings(); setRange(1440);
  window.onhashchange = route; route();
  chart = new DemandChart(element("chart"));
  if (authenticationRequired) {
    element("logout").onclick = async () => {
      try { await api("/api/auth/logout", {}); renderLogin(); } catch (error) { showError(error); }
    };
  }
  element<HTMLFormElement>("range").onsubmit = event => {
    event.preventDefault(); selectedMinutes = 0;
    for (const button of document.querySelectorAll<HTMLButtonElement>("[data-minutes]")) button.setAttribute("aria-pressed", "false");
    clearMessage(); void refresh();
  };
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-minutes]")) {
    button.onclick = () => { setRange(Number(button.dataset.minutes)); clearMessage(); void refresh(); };
  }
  element("history-device").onchange = () => { clearMessage(); void refresh(); };
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
    };
    if (element<HTMLInputElement>("clear-key").checked) data.api_key = null;
    try {
      config = await api<Config>("/api/config", data);
      fillSettings(); text("test-result", "Settings saved.");
      await refresh();
    } catch (error) { showError(error); }
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
