import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { History, TariffStatus } from "./api";

export interface ChartIndicators {
  offPeak: boolean; bonus: boolean; charging: boolean; unresolved: boolean; threshold: boolean;
}
interface ChartContext {
  timezone?: string;
  grid?: History | null;
  gaps?: { from: string; to: string }[];
  transitions?: { from: string; to: string }[];
}

export class DemandChart {
  private plot?: uPlot;
  private observer: ResizeObserver;
  private lastArgs?: [History, TariffStatus["rates"], TariffStatus["charging"], TariffStatus["unresolved"], ChartContext];
  private suppressingScale = false;
  constructor(
    private target: HTMLElement,
    private indicators: (visible: ChartIndicators) => void,
    private rangeChanged?: (from: Date, to: Date) => void,
  ) {
    this.observer = new ResizeObserver(() => this.plot?.setSize({
      width: Math.max(1, target.clientWidth), height: 340,
    }));
    this.observer.observe(target);
  }
  update(
    history: History, rates: TariffStatus["rates"],
    charging: TariffStatus["charging"] = [], unresolved: TariffStatus["unresolved"] = [],
    context: ChartContext = {},
  ): void {
    this.lastArgs = [history, rates, charging, unresolved, context];
    this.plot?.destroy();
    const s = history.series;
    // Incomplete EV bucket means are not drawn as a fully supported continuous curve.
    const evMeans = s.mean_ev_w.map((value, i) => s.ev_coverage_pct[i] === 100 ? value : null);
    const hasEV = s.mean_ev_w.some(value => value !== null);
    const theme = getComputedStyle(this.target);
    const color = (token: string): string => theme.getPropertyValue(token).trim();
    const seconds = (intervals: { from: string; to: string }[]): number[][] => intervals.map(interval => [
      new Date(interval.from).getTime() / 1000, new Date(interval.to).getTime() / 1000,
    ]);
    const offPeak = seconds(rates.filter(rate => rate.band === "ECO7_NIGHT"));
    const bonus = seconds(rates.filter(rate => rate.band === "EV_BONUS"));
    const chargingBands = seconds(charging);
    const unknownBands = seconds(unresolved);
    const gapBands = seconds(context.gaps ?? []);
    const transitionBands = seconds(context.transitions ?? []);
    const visibleBands = (plot: uPlot, bands = offPeak): number[][] => bands.map(([from, to]) => [
      Math.max(from, plot.scales.x.min!), Math.min(to, plot.scales.x.max!),
    ]).filter(([from, to]) => from < to);
    const shadeOffPeak = (plot: uPlot): void => {
      const { ctx, bbox } = plot;
      ctx.save();
      ctx.beginPath(); ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height); ctx.clip();
      for (const [bands, token, strip] of [
        [offPeak, "--chart-off-peak", false], [bonus, "--chart-bonus", false],
        [unknownBands, "--chart-unknown", true], [chargingBands, "--chart-charging", true],
        [gapBands, "--chart-gap", false], [transitionBands, "--chart-transition", false],
      ] as const) {
        ctx.fillStyle = color(token);
        for (const [from, to] of visibleBands(plot, bands)) {
          const left = plot.valToPos(from, "x", true);
          const height = strip ? Math.max(4, bbox.height * 0.025) : bbox.height;
          ctx.fillRect(left, bbox.top + bbox.height - height, plot.valToPos(to, "x", true) - left, height);
        }
      }
      ctx.restore();
    };
    const drawThreshold = (plot: uPlot): void => {
      const { ctx, bbox } = plot;
      const threshold = plot.scales.y.min != null && plot.scales.y.max != null
        && plot.scales.y.min <= 6000 && plot.scales.y.max >= 6000;
      this.indicators({
        offPeak: visibleBands(plot).length > 0, bonus: visibleBands(plot, bonus).length > 0,
        charging: visibleBands(plot, chargingBands).length > 0,
        unresolved: visibleBands(plot, unknownBands).length > 0, threshold,
      });
      if (!threshold) return;
      const y = plot.valToPos(6000, "y", true);
      const ratio = plot.ctx.canvas.width / plot.width;
      const accent = color("--chart-threshold");
      ctx.save();
      ctx.beginPath(); ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height); ctx.clip();
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5 * ratio;
      ctx.setLineDash([6 * ratio, 4 * ratio]);
      ctx.beginPath(); ctx.moveTo(bbox.left, y); ctx.lineTo(bbox.left + bbox.width, y); ctx.stroke();
      ctx.setLineDash([]);
      const badgeW = 46 * ratio;
      const badgeH = 18 * ratio;
      const badgeX = bbox.left + bbox.width - badgeW - 6 * ratio;
      const badgeY = Math.max(bbox.top + 4 * ratio, y - badgeH - 4 * ratio);
      ctx.fillStyle = accent;
      ctx.fillRect(badgeX, badgeY, badgeW, badgeH);
      ctx.fillStyle = color("--chart-threshold-ink") || "#FFFFFF";
      ctx.font = `700 ${10 * ratio}px "Helvetica Neue", "Segoe UI", Arial, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("6 kW", badgeX + badgeW / 2, badgeY + badgeH / 2);
      ctx.restore();
    };
    const axis = {
      stroke: color("--muted"),
      grid: { stroke: color("--rule-grid") || color("--line"), width: 1 },
      ticks: { stroke: color("--line"), width: 1 },
      font: '11px "Helvetica Neue", "Segoe UI", Arial, sans-serif',
    };
    this.plot = new uPlot({
      width: Math.max(1, this.target.clientWidth), height: 340,
      cursor: { drag: { x: true, y: false } },
      scales: { x: { min: new Date(history.from).getTime() / 1000, max: new Date(history.to).getTime() / 1000 } },
      hooks: {
        drawClear: [shadeOffPeak], draw: [drawThreshold],
        setScale: [(plot, key) => {
          if (this.suppressingScale || !this.rangeChanged || key !== "x" || plot.status !== 1) return;
          const { min, max } = plot.scales.x;
          if (min == null || max == null) return;
          const from = new Date(Math.round(min) * 1000), to = new Date(Math.round(max) * 1000);
          if (from >= to) return;
          // Finish uPlot's scale update before the range callback replaces the chart.
          queueMicrotask(() => {
            if (this.plot === plot) this.rangeChanged?.(from, to);
          });
        }],
      },
      series: [
        { label: "Bucket end", ...(context.timezone ? { value: (_plot: uPlot, value: number) =>
          value == null ? "--" : new Date(value * 1000).toLocaleString(undefined, { timeZone: context.timezone, timeZoneName: "short" }) } : {}) },
        { label: "Sampled peak (W)", stroke: color("--chart-peak"), fill: color("--chart-area") || undefined, width: 1.75, spanGaps: false },
        ...(hasEV ? [{ label: "EV charging (W)", stroke: color("--chart-ev"), width: 2, spanGaps: false }] : []),
        ...(context.grid ? [{ label: "Grid peak reference (W)", stroke: color("--muted"), width: 1, dash: [4, 4], spanGaps: false }] : []),
      ],
      axes: [
        { ...axis, ...(context.timezone ? { values: (_plot: uPlot, splits: number[]) =>
          splits.map(value => new Date(value * 1000).toLocaleTimeString(undefined, {
            timeZone: context.timezone, hour: "2-digit", minute: "2-digit", hour12: false,
          })) } : {}) },
        { ...axis, label: history.method.power_basis === "nonnegative_grid_import_excluding_ev"
          ? hasEV ? "Non-EV / EV power (W)" : "Non-EV grid import (W)"
          : hasEV ? "Grid / EV power (W)" : "Grid import (W)", size: 65 },
      ],
    }, [s.bucket_end_epoch_s, s.sampled_peak_w,
      ...(hasEV ? [evMeans] : []),
      ...(context.grid ? [context.grid.series.sampled_peak_w] : [])], this.target);
  }
  focus(start: number, end: number): void {
    this.plot?.setScale("x", { min: start, max: end });
  }
  redraw(): void {
    if (!this.lastArgs) return;
    const xMin = this.plot?.scales.x.min;
    const xMax = this.plot?.scales.x.max;
    this.suppressingScale = true;
    try {
      this.update(...this.lastArgs);
      if (xMin != null && xMax != null && this.plot) {
        this.plot.setScale("x", { min: xMin, max: xMax });
      }
    } finally {
      this.suppressingScale = false;
    }
  }
  clear(): void {
    this.lastArgs = undefined;
    this.plot?.destroy(); this.plot = undefined;
    this.indicators({ offPeak: false, bonus: false, charging: false, unresolved: false, threshold: false });
  }
  destroy(): void { this.observer.disconnect(); this.plot?.destroy(); }
}
