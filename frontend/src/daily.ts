import { api, Config, DayAnalysis, DayEvent, PricingEstimate, RecoveryStatus, Snapshot, Summary } from "./api";
import { DemandChart } from "./charts";

type View = "demand" | "ev" | "coverage";
const views: { id: View; label: string }[] = [
  { id: "demand", label: "Daily demand" }, { id: "ev", label: "EV & tariff" }, { id: "coverage", label: "Coverage & events" },
];
const escape = (value: string): string => value.replace(/[&<>"']/g, char =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
const number = (value: number | null, digits = 2): string =>
  value === null ? "Unavailable" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
const cost = (pricing: PricingEstimate): string => !pricing.enabled ? "Pricing disabled"
  : pricing.excess_energy_gbp === null ? "Unavailable"
  : new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }).format(pricing.excess_energy_gbp);
const hours = (seconds: number): string => `${number(seconds / 3600)} h`;
const sumSeconds = (periods: { from: string; to: string }[]): number =>
  periods.reduce((total, row) => total + (Date.parse(row.to) - Date.parse(row.from)) / 1000, 0);

export function dayLink(date: string, device: string, timezone: string, excludeEV: boolean, view: View = "demand"): string {
  const query = new URLSearchParams({ device_id: device, timezone, exclude_ev: String(excludeEV) });
  return `#day/${encodeURIComponent(date)}/${view}?${query}`;
}

export class DailyPage {
  private chart?: DemandChart;
  private pending?: AbortController;
  private data?: DayAnalysis;
  private key = "";
  private date = "";
  private view: View = "demand";
  private device = "";
  private timezone = "";
  private excludeEV = false;
  private focusWindow?: { from: number; to: number };
  private recoveryText = "";
  private recovering = false;
  private recoveryQueued = false;
  private connected = false;
  private active = false;

  constructor(
    private target: HTMLElement,
    private configuration: () => Config,
    private onError: (error: unknown) => void,
  ) {}

  hide(): void {
    this.active = false;
    this.pending?.abort();
    this.pending = undefined;
    this.chart?.destroy(); this.chart = undefined;
  }
  destroy(): void { this.hide(); this.target.replaceChildren(); }

  async open(hash: string, reload = false): Promise<void> {
    this.active = true;
    this.pending?.abort(); this.pending = undefined;
    const [path, search = ""] = hash.slice(1).split("?");
    const [, encodedDate = "", requestedView = "demand"] = path.split("/");
    this.date = encodedDate;
    this.view = views.find(view => view.id === requestedView)?.id ?? "demand";
    const params = new URLSearchParams(search);
    const config = this.configuration();
    this.device = params.get("device_id") ?? config.active_device_id ?? "";
    this.timezone = params.get("timezone") ?? config.display_timezone;
    this.excludeEV = params.get("exclude_ev") === "true";
    const query = new URLSearchParams({
      date: this.date, device_id: this.device, timezone: this.timezone, exclude_ev: String(this.excludeEV),
    });
    const key = query.toString();
    if (!reload && key === this.key && this.data) { this.render(); return; }
    this.data = undefined; this.key = ""; this.focusWindow = undefined;
    this.recoveryText = ""; this.recoveryQueued = false;
    const message = document.getElementById("message");
    if (message) message.textContent = "";
    this.chart?.destroy(); this.chart = undefined;
    this.target.innerHTML = `${this.header()}<section class="day-panel"><p id="day-loading" role="status">Loading recorded data for this calendar day...</p></section>`;
    this.bind();
    const controller = new AbortController();
    this.pending = controller;
    try {
      const data = await api<DayAnalysis>(`/api/analytics/day?${query}`, undefined, controller.signal);
      if (controller.signal.aborted || !this.active) return;
      this.data = data; this.key = key; this.timezone = data.timezone;
      this.render();
    } catch (error) {
      if (controller.signal.aborted || !this.active) return;
      this.target.innerHTML = `${this.header()}<section class="day-panel"><h2>Daily analysis unavailable</h2><p id="day-error" role="alert"></p><button id="day-retry">Retry this day</button></section>`;
      const message = this.target.querySelector("#day-error")!;
      message.textContent = error instanceof Error ? error.message : "The daily analysis request failed.";
      this.bind();
      this.onError(error);
    } finally { if (this.pending === controller) this.pending = undefined; }
  }

  private link(date = this.date, view = this.view, basis = this.excludeEV): string {
    return dayLink(date, this.device, this.timezone, basis, view);
  }
  private clock(at: string): string {
    return new Date(at).toLocaleString(undefined, {
      timeZone: this.timezone, timeZoneName: "short", day: "2-digit", month: "short",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  }
  private shift(days: number): string {
    const date = new Date(`${this.date}T12:00:00Z`);
    if (!Number.isFinite(date.getTime())) return this.date;
    date.setUTCDate(date.getUTCDate() + days);
    return date.toISOString().slice(0, 10);
  }
  private header(): string {
    const config = this.configuration(), data = this.data;
    const date = new Date(`${this.date}T12:00:00Z`);
    const title = Number.isFinite(date.getTime()) ? date.toLocaleDateString(undefined, {
      timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric",
    }) : this.date;
    const label = config.devices.find(device => device.device_id === this.device)?.label ?? this.device;
    return `<div class="day-breadcrumb"><a id="day-back" href="#inverters">&larr; Back to overview</a><span>/ Daily analysis / ${escape(this.date)}</span></div>
      <div class="page-heading day-heading"><div><p class="eyebrow">HOME ENERGY / DAILY ANALYSIS</p><h1>${escape(title)}<span class="accent">_</span></h1>
      <p class="note">${escape(label)} &middot; ${escape(this.timezone)}${data ? ` &middot; ${hours((Date.parse(data.calendar_to) - Date.parse(data.from)) / 1000)} calendar day${data.partial ? " &middot; today, clipped at request time" : ""}` : ""}</p></div>
      <div class="day-date-controls"><a class="day-button" aria-label="Previous calendar day" href="${escape(this.link(this.shift(-1)))}">&larr;</a>
      <label class="day-date-label">Calendar date<input id="day-date" type="date" min="1970-01-01" ${data ? `max="${data.today}"` : ""} value="${escape(this.date)}"></label>
      ${data && this.date >= data.today ? '<span class="day-button disabled" aria-label="Next calendar day unavailable">&rarr;</span>' : `<a class="day-button" aria-label="Next calendar day" href="${escape(this.link(this.shift(1)))}">&rarr;</a>`}</div></div>
      <div class="day-toolbar"><nav class="day-tabs" aria-label="Daily analysis views">${views.map(view =>
        `<a href="${escape(this.link(this.date, view.id))}" ${this.view === view.id ? 'aria-current="page"' : ""}>${view.label}</a>`).join("")}</nav>
      <div class="day-basis" role="group" aria-label="Daily analysis basis"><span>Analysis basis</span><button data-day-basis="false" aria-pressed="${!data?.exclude_ev}">Grid import</button><button data-day-basis="true" aria-pressed="${!!data?.exclude_ev}" ${data?.ev_unavailable_reason ? `disabled title="${escape(data.ev_unavailable_reason)}"` : ""}>Exclude EV</button></div></div>`;
  }
  private summary(): Summary { return this.data!.exclude_ev ? this.data!.adjusted! : this.data!.grid; }
  private metric(label: string, value: string, note: string, accent = false): string {
    return `<article><p>${label}</p><strong${accent ? ' class="accent"' : ""}>${escape(value)}</strong><small>${escape(note)}</small></article>`;
  }
  private render(): void {
    const data = this.data!;
    const summary = this.summary();
    this.chart?.destroy(); this.chart = undefined;
    const unsupported = Math.max(0, summary.requested_seconds - summary.observed_seconds);
    const warning = summary.coverage_pct < 100
      ? `<div class="day-notice" role="note"><strong>${number(summary.coverage_pct)}% of this day is supported.</strong> ${hours(summary.observed_seconds)} of ${hours(summary.requested_seconds)} observed; ${hours(unsupported)} unknown. Energy and costs cover observed periods only, not a projected daily total. <a href="${escape(this.link(this.date, "coverage"))}">Inspect coverage &rarr;</a></div>`
      : `<p class="note">All requested time is supported. Peaks remain sampled values; brief surges may be missed.</p>`;
    this.target.innerHTML = `${this.header()}${warning}
      ${data.ev_unavailable_reason ? `<p class="day-notice">${escape(data.ev_unavailable_reason)}. Grid analysis remains available.</p>` : ""}
      ${this.view === "demand" ? this.demand() : this.view === "ev" ? this.tariff() : this.coverage()}
      <p class="day-status note">${escape([...new Set([...data.warnings, ...summary.warnings])].join(". "))}</p>
      <div class="day-footer"><span>Calendar times: ${escape(data.timezone)} &middot; Snapshot: ${escape(this.clock(data.to))}<br>Historical analysis stays fixed. Collection continues independently.</span><button id="day-retry" class="secondary">Refresh this day</button></div>`;
    this.bind();
    const chartTarget = this.target.querySelector<HTMLElement>("#day-chart");
    if (chartTarget) {
      this.chart = new DemandChart(chartTarget, indicators => {
        const key = this.target.querySelector<HTMLElement>("#day-threshold-key");
        if (key) key.hidden = !indicators.threshold;
      });
      this.chart.update(data.history, data.tariff.rates, data.charging, data.tariff.unresolved, {
        timezone: data.timezone, grid: data.grid_history,
        gaps: data.meter_gaps,
        transitions: data.exclude_ev ? data.transition_gaps : [],
      });
      this.applyFocus();
    }
  }
  private graph(): string {
    return `<section class="day-panel"><div class="section-heading"><div><h2>Demand, charging &amp; household rates</h2><p class="note">${escape(this.timezone)} &middot; chart buckets: ${this.data!.history.interval_seconds / 60} min</p></div><div class="day-chart-controls"><button class="secondary" id="day-full">Full day</button><button class="secondary" id="day-peak" ${this.summary().sampled_peak_at ? "" : "disabled"}>Peak window</button></div></div>
      <div id="day-chart" aria-label="Calendar day demand chart"></div>
      <div class="chart-indicators"><span class="off-peak-key">Scheduled off-peak</span><span class="bonus-key">Estimated EV bonus slot</span><span class="charging-key">Recorded EV charging</span><span class="unresolved-key">Unresolved rate</span><span class="day-gap-key">Missing meter data</span>${this.data!.exclude_ev ? '<span class="day-transition-key">Transition uncertainty</span>' : ""}<span id="day-threshold-key" class="threshold-key" hidden>6 kW threshold</span></div>
      <p class="note">Gaps are unknown, not zero. Incomplete bucket means are not drawn. ${this.data!.exclude_ev ? "The dashed grid peak reference uses the same buckets but can have different supported periods." : "Peaks are sampled grid import, not continuous household load."}</p></section>`;
  }
  private demand(): string {
    const data = this.data!, summary = this.summary(), threshold = summary.thresholds[0];
    const overlap = data.peak_overlaps_ev;
    const peakNote = summary.sampled_peak_at
      ? `At ${this.clock(summary.sampled_peak_at)}; gaps may hide a higher peak`
      : "No supported peak is available";
    return `<section class="metrics day-metrics" aria-label="Daily metrics">
      ${this.metric(data.exclude_ev ? "Derived non-EV peak" : "Sampled grid peak", summary.sampled_peak_w === null ? "Unavailable" : `${number(summary.sampled_peak_w / 1000)} kW`, peakNote, true)}
      ${this.metric("Observed import", summary.import_energy_kwh === null ? "Unavailable" : `${number(summary.import_energy_kwh)} kWh`, "Observed energy, not an extrapolated daily total")}
      ${this.metric("Estimated excess cost >6 kW", cost(summary.pricing), "Only energy beyond 6 kW at applicable rates")}
      ${this.metric("Usable coverage", `${number(summary.coverage_pct)}%`, data.exclude_ev ? "Joint meter / Ohme, minus transition gaps" : "Within the requested calendar day")}</section>
      <div class="day-grid">${this.graph()}<aside class="day-panel day-findings"><p class="eyebrow">WHAT THE DATA SUPPORTS</p><h2>Day findings</h2>
      <article><h3>${summary.sampled_peak_w === null ? "No supported peak" : overlap === true ? "The selected peak overlaps recorded EV charging" : overlap === false ? "The peak does not overlap recorded positive EV power" : "EV attribution is unavailable"}</h3>
      <p>${overlap === true ? "Charger power was positive at the peak timestamp. This is evidence of overlap, not an appliance diagnosis or a claim that EV charging caused the entire peak." : "A grid curve alone cannot identify EV charging or individual appliances. Missing evidence is not proof that the charger was off."}</p><a href="${escape(this.link(this.date, "ev"))}">See charging evidence &rarr;</a></article>
      <article><h3>Separate peak height from excess energy</h3><p>${summary.observed_seconds ? `${hours(threshold.seconds_above)} above 6 kW in supported time. The estimated excess cost prices only demand beyond the reference, not the entire load.` : "No supported duration is available; energy and cost are unavailable, not zero."}</p><button class="secondary" id="day-inspect" ${summary.sampled_peak_at ? "" : "disabled"}>Inspect peak window &rarr;</button></article>
      <article><h3>${summary.coverage_pct < 100 ? "Do not infer a complete-day total" : "Supported is not the same as continuous"}</h3><p>Missing periods may hide additional demand. Sampled peaks can miss short surges. The 6 kW reference is not a battery-sizing recommendation.</p><a href="${escape(this.link(this.date, "coverage"))}">Inspect evidence &rarr;</a></article></aside></div>
      <div class="day-two"><section class="day-panel"><p class="eyebrow">REFERENCE COMPARISON</p><h2>What a 6 kW rating would not cover</h2><dl class="day-facts">
      ${this.fact("Observed time above 6 kW", summary.observed_seconds ? hours(threshold.seconds_above) : "Unavailable")}
      ${this.fact("Whole-load energy while above", threshold.energy_when_above_kwh === null ? "Unavailable" : `${number(threshold.energy_when_above_kwh, 3)} kWh`)}
      ${this.fact("Only the energy beyond 6 kW", threshold.excess_energy_kwh === null ? "Unavailable" : `${number(threshold.excess_energy_kwh, 3)} kWh`)}
      ${this.fact("Estimated cost of that excess", cost(summary.pricing))}</dl><p class="note">${escape(summary.pricing.warning)}. Costs cover observed energy only. Exceeding 6 kW is not itself an Octopus penalty.</p></section>
      <section class="day-panel"><p class="eyebrow">SAME DAY / SAME EVIDENCE</p><h2>Grid and non-EV side by side</h2>${this.comparison()}
      <p class="note">The two bases can have different supported periods. Subtracting these energy totals does not measure EV energy. Solar or a battery can make non-EV grid import lower than household load.</p></section></div>`;
  }
  private fact(label: string, value: string): string { return `<div><dt>${label}</dt><dd>${escape(value)}</dd></div>`; }
  private comparison(): string {
    const data = this.data!;
    const rows: [string, (summary: Summary) => string][] = [
      ["Peak", row => row.sampled_peak_w === null ? "Unavailable" : `${number(row.sampled_peak_w / 1000)} kW`],
      ["Energy", row => row.import_energy_kwh === null ? "Unavailable" : `${number(row.import_energy_kwh)} kWh`],
      ["Supported coverage", row => `${number(row.coverage_pct)}%`],
      ["Excess cost", row => cost(row.pricing)],
    ];
    return `<div class="table-scroll"><table><thead><tr><th>Observed metric</th><th>Grid import</th><th>Exclude EV</th></tr></thead><tbody>${rows.map(([label, value]) => `<tr><td>${label}</td><td>${escape(value(data.grid))}</td><td>${escape(data.adjusted ? value(data.adjusted) : "Unavailable")}</td></tr>`).join("")}</tbody></table></div>`;
  }
  private tariff(): string {
    const data = this.data!, summary = this.summary(), tariff = data.tariff;
    const requested = (Date.parse(data.to) - Date.parse(data.from)) / 1000;
    const hasEvidence = data.coverage.ev_seconds !== null && data.coverage.ev_seconds > 0;
    const bonusSeconds = sumSeconds(tariff.rates.filter(row => row.band === "EV_BONUS"));
    const bonusDuration = !tariff.enabled ? "Pricing disabled" : !tariff.adjustment_enabled ? "Not enabled"
      : tariff.unresolved.length ? bonusSeconds ? `At least ${hours(bonusSeconds)}` : "Unresolved"
      : tariff.rates.length ? hours(bonusSeconds) : "Unavailable";
    const periods = [
      ...data.charging.map(row => ({ ...row, label: "Recorded EV charging", price: "See matching household slot" })),
      ...tariff.rates.map(row => ({ ...row, label: row.band === "EV_BONUS" ? "Estimated EV bonus half-hour" : row.band === "ECO7_NIGHT" ? "Scheduled off-peak" : "Published household rate", price: `${number(row.pence_per_kwh, 5)} p/kWh` })),
      ...tariff.unresolved.map(row => ({ ...row, label: "Unresolved household rate", price: "Unavailable" })),
    ].filter(row => Date.parse(row.from) < Date.parse(data.to) && Date.parse(row.to) > Date.parse(data.from))
      .sort((a, b) => a.from.localeCompare(b.from));
    const clip = (value: string, left: boolean): string => new Date(
      left ? Math.max(Date.parse(value), Date.parse(data.from)) : Math.min(Date.parse(value), Date.parse(data.to)),
    ).toISOString();
    return `<section class="metrics day-metrics" aria-label="EV and tariff metrics">
      ${this.metric("Recorded charging periods", hasEvidence ? number(data.charging.length) : "Unavailable", "Within known history; not supplier dispatches")}
      ${this.metric("Observed charging duration", hasEvidence ? hours(sumSeconds(data.charging)) : "Unavailable", "Missing evidence is not zero charging")}
      ${this.metric("Bonus-priced duration", bonusDuration, "Estimated household slots; unresolved slots omitted", true)}
      ${this.metric("EV evidence coverage", data.coverage.ev_seconds === null ? "Unavailable" : `${number(100 * data.coverage.ev_seconds / requested)}%`, "Independent of grid or exclusion coverage")}</section>
      <div class="day-grid">${this.graph()}<aside class="day-panel"><p class="eyebrow">WHY THE RATE APPLIES</p><h2>Charging minutes are not tariff minutes</h2>
      <p>Under the configured Savings-only policy, recorded positive EV power can earn the household night rate for the matching settlement half-hour.</p>
      <dl class="day-facts">${this.fact("Policy", tariff.adjustment_enabled ? `Prioritise Savings from ${data.ev_savings_from ?? "configured date"}` : "Bonus-rate assumption not active for this meter")}
      ${this.fact("Observed excess cost", cost(summary.pricing))}
      ${this.fact("Price coverage of supported exceedances", summary.pricing.price_coverage_pct === null ? "Unavailable" : `${number(summary.pricing.price_coverage_pct)}%`)}</dl>
      <p class="note">Transition guards do not remove original charging evidence or revoke bonus entitlement. Prices include VAT; these are estimates, not supplier-confirmed billing slots.</p></aside></div>
      <section class="day-panel"><div class="section-heading"><div><p class="eyebrow">THE AUDIT TRAIL</p><h2>Charging evidence &amp; applicable household rates</h2></div></div>
      <p class="note">Local times in ${escape(this.timezone)}. Actual charging and tariff slots are separate rows; values clip to this day's requested bounds.</p>
      <div class="table-scroll"><table><thead><tr><th>Period / evidence</th><th>From</th><th>To</th><th>Household rate including VAT</th></tr></thead><tbody>${periods.slice(0, 200).map(row => `<tr><td>${row.label}</td><td>${escape(this.clock(clip(row.from, true)))}</td><td>${escape(this.clock(clip(row.to, false)))}</td><td>${escape(row.price)}</td></tr>`).join("") || '<tr><td colspan="4">No retained charging or applicable tariff evidence for this day. This is not proof of no charging.</td></tr>'}</tbody></table></div>
      <p class="note">${periods.length > 200 ? `Showing the first 200 of ${periods.length} evidence/rate rows. ` : ""}${escape([summary.pricing.warning, ...tariff.warnings].join(" "))} Price coverage concerns observed above-threshold time, not the full day's telemetry. Missing prices yield unavailable costs, not zero.</p></section>`;
  }
  private coverage(): string {
    const data = this.data!, summary = this.summary();
    const requested = (Date.parse(data.to) - Date.parse(data.from)) / 1000;
    const health = (label: string, seconds: number | null, note: string): string =>
      `<div class="day-health"><div><strong>${label}</strong><span>${seconds === null ? "Unavailable" : `${hours(seconds)} / ${hours(requested)} (${number(seconds * 100 / requested)}%)`}</span></div><meter min="0" max="${requested}" value="${seconds ?? 0}" aria-label="${label} coverage">${number(seconds === null ? null : seconds * 100 / requested)}%</meter><small>${note}</small></div>`;
    const details: Record<DayEvent["kind"], { title: string; note: string }> = {
      meter_gap: { title: "Meter gap", note: "Demand and energy are unknown; this is not a zero-load period." },
      ev_gap: { title: "Ohme evidence gap", note: "No supported recorded EV state; grid values are not substituted." },
      ev_transition: { title: "EV transition uncertainty", note: "Non-EV analysis is omitted around a recorded charging start/stop; grid data remains." },
      exceedance: { title: "Observed exceedance", note: `Above 6 kW in the ${data.exclude_ev ? "EV-excluded" : "grid"} analysis; missing data breaks episodes.` },
    };
    return `<div class="day-two"><section class="day-panel"><p class="eyebrow">WHERE THE DAY IS KNOWN</p><h2>Supported time by source</h2>
      ${health("Grid meter", data.coverage.meter_seconds, "Valid bounded meter segments; invalid values and long gaps are unsupported.")}
      ${health("Ohme recorded evidence", data.coverage.ev_seconds, "Known recorded states; unchanged states cannot prove there were no unreported outages.")}
      ${health("Joint meter / Ohme", data.coverage.joint_seconds, "Intersection of supported sources, before charging transition guards.")}
      ${health("EV-excluded analysis", data.coverage.adjusted_seconds, "Joint support after transition uncertainty; overlapping gaps count once.")}</section>
      <section class="day-panel"><p class="eyebrow">HOW TO READ THIS DAY</p><h2>What we can and cannot conclude</h2><ul class="day-reading">
      <li><strong>Supported:</strong> observed energy and peaks within usable intervals.</li><li><strong>Not supported:</strong> a full-day total or a claim that unseen periods had no higher peak.</li><li><strong>Attribution:</strong> recorded EV power, not appliance identity inferred from a grid curve.</li><li><strong>Excess cost:</strong> ${escape(cost(summary.pricing))}. ${escape(summary.pricing.warning)}</li><li><strong>Comparison:</strong> differences in coverage can explain differences in reported metrics.</li></ul></section></div>
      <section class="day-panel"><p class="eyebrow">DAY EVENT LOG</p><h2>Events and uncertainty</h2><p class="note">${data.raw_available ? "Observed periods only. Missing evidence closes an episode; it never bridges it." : "Detailed events require retained raw readings. Archive gaps and EV activity are not reconstructed."}</p>
      <div class="table-scroll"><table><thead><tr><th>From</th><th>To</th><th>Event</th><th>Meaning</th><th>Action</th></tr></thead><tbody>${data.events.map((row, index) => `<tr><td>${escape(this.clock(row.from))}</td><td>${escape(this.clock(row.to))}</td><td>${details[row.kind].title}${row.peak_w === null ? "" : ` (${number(row.peak_w / 1000)} kW)`}</td><td class="day-event-note">${details[row.kind].note}</td><td><button class="secondary" data-day-event="${index}">Inspect</button></td></tr>`).join("") || `<tr><td colspan="5">${data.raw_available ? "No detailed events in the supported evidence." : "Detailed event data is unavailable at the retained resolution."}</td></tr>`}</tbody></table></div>
      <p class="note">${data.event_count > data.events.length ? `Showing ${data.events.length} of ${data.event_count} events, ordered by start time. ` : ""}Known transition-guard duration: ${data.coverage.ev_seconds === null ? "Unavailable" : hours(sumSeconds(data.transition_gaps))}; only overlaps with valid meter time reduce joint coverage.</p></section>
      <div class="day-two"><section class="day-panel"><p class="eyebrow">RECOVERY WITHOUT FALSE PROMISES</p><h2>Retry missing meter readings</h2><p>Retries missing Octopus readings for this day within raw retention. It does not recover Home Assistant history, start polling, or guarantee that older readings exist.</p>
      <button id="day-recover" ${!data.recovery_available || this.recovering ? "disabled" : ""}>Recover missing readings for this day</button>
      <p class="note">${data.recovery_available ? "Existing pending recovery is retained. The newest three minutes are excluded." : "Requires the active meter, enabled polling, a started recovery window, and a day within retained raw history."}</p><p id="day-recovery-status" class="note" role="status">${escape(this.recoveryText)}</p></section>
      <section class="day-panel"><p class="eyebrow">ACTUAL RETAINED RESOLUTION</p><h2>${data.raw_available ? "Recent raw history" : "Archived day"}</h2><p>Chart resolution: ${number(data.history.interval_seconds / 60)} minutes. ${data.raw_available ? "EV exclusion is available only with configured, retained evidence." : "Grid aggregates remain usable. EV exclusion and detailed events are unavailable without raw meter/Ohme evidence."}</p><p class="note">A no-data day shows unavailable energy and cost, not zero. Today is clipped at request time; calendar days can have 23 or 25 hours across clock changes.</p></section></div>`;
  }
  private bind(): void {
    this.target.querySelector<HTMLInputElement>("#day-date")!.onchange = event => {
      const input = event.currentTarget as HTMLInputElement;
      if (input.value && input.checkValidity()) location.hash = this.link(input.value);
    };
    for (const button of this.target.querySelectorAll<HTMLButtonElement>("[data-day-basis]")) {
      button.onclick = () => { location.hash = this.link(this.date, this.view, button.dataset.dayBasis === "true"); };
    }
    const retry = this.target.querySelector<HTMLButtonElement>("#day-retry");
    if (retry) retry.onclick = () => { void this.open(location.hash, true); };
    const focus = (): void => {
      const at = this.summary().sampled_peak_at;
      if (at) this.focusWindow = { from: Date.parse(at) / 1000 - 900, to: Date.parse(at) / 1000 + 900 };
      this.applyFocus();
    };
    for (const id of ["day-peak", "day-inspect"]) {
      const button = this.target.querySelector<HTMLButtonElement>(`#${id}`);
      if (button) button.onclick = focus;
    }
    const full = this.target.querySelector<HTMLButtonElement>("#day-full");
    if (full) full.onclick = () => { this.focusWindow = undefined; this.applyFocus(); };
    for (const button of this.target.querySelectorAll<HTMLButtonElement>("[data-day-event]")) {
      button.onclick = () => {
        const row = this.data!.events[Number(button.dataset.dayEvent)];
        this.focusWindow = { from: Date.parse(row.from) / 1000 - 300, to: Date.parse(row.to) / 1000 + 300 };
        location.hash = this.link(this.date, "demand");
      };
    }
    const recover = this.target.querySelector<HTMLButtonElement>("#day-recover");
    if (recover) recover.onclick = () => { void this.recover(); };
  }
  private applyFocus(): void {
    const data = this.data!;
    const left = Date.parse(data.history.from) / 1000, right = Date.parse(data.history.to) / 1000;
    this.chart?.focus(Math.max(left, this.focusWindow?.from ?? left), Math.min(right, this.focusWindow?.to ?? right));
  }
  private async recover(): Promise<void> {
    const data = this.data!;
    this.recovering = true; this.recoveryText = "Requesting recovery for this calendar day..."; this.render();
    try {
      const queued = await api<RecoveryStatus>("/api/recovery", {
        expected_revision: data.config_revision, device_id: data.device_id, from: data.from, to: data.to,
      });
      if (!this.active || this.data !== data) return;
      this.recoveryQueued = true;
      this.recoveryText = `Recovery queued: ${this.clock(queued.from)} to ${this.clock(queued.to)}. ${this.connected ? "Waiting for live progress updates." : "Live updates are disconnected; progress is unknown."}`;
    } catch (error) {
      if (!this.active || this.data !== data) return;
      this.recoveryText = error instanceof Error ? `Recovery request failed: ${error.message}` : "Recovery request failed.";
      this.onError(error);
    } finally {
      this.recovering = false;
      if (this.active && this.data) this.render();
    }
  }
  connection(connected: boolean): void {
    this.connected = connected;
    if (!connected && this.recoveryQueued) this.showRecovery("Live updates disconnected. Recovery may still be running; progress is unknown.");
  }
  update(snapshot: Snapshot): void {
    if (!this.active || !this.data || !this.recoveryQueued || this.recovering || snapshot.device_id !== this.data.device_id) return;
    const health = snapshot.health;
    const state = health.recovery_error ? `Recovery paused: ${health.recovery_error.replaceAll("_", " ")}.`
      : health.recovery_pending ? `Recovery ${health.recovery_active ? "running" : "queued or waiting"}${health.recovery_cursor_at ? `; cursor ${this.clock(health.recovery_cursor_at)}` : ""}.`
      : health.recovery_state === "complete" ? `Recovery finished${health.recovery_gap_count ? ` with ${health.recovery_gap_count} unresolved historical windows` : ""}. Refresh this day to load updated evidence.`
      : `Recovery status: ${health.recovery_state}.`;
    this.showRecovery(state);
  }
  private showRecovery(message: string): void {
    this.recoveryText = message;
    const element = this.target.querySelector("#day-recovery-status");
    if (element) element.textContent = message;
  }
}
