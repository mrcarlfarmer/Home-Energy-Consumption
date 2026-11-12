import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { History, TariffStatus } from "./api";

export interface ChartIndicators { offPeak: boolean; threshold: boolean }

export class DemandChart {
  private plot?: uPlot;
  private observer: ResizeObserver;
  constructor(
    private target: HTMLElement,
    private indicators: (visible: ChartIndicators) => void,
    private rangeChanged: (from: Date, to: Date) => void,
  ) {
    this.observer = new ResizeObserver(() => this.plot?.setSize({
      width: Math.max(1, target.clientWidth), height: 340,
    }));
    this.observer.observe(target);
  }
  update(history: History, rates: TariffStatus["rates"]): void {
    this.plot?.destroy();
    const s = history.series;
    // Incomplete bucket means are not drawn as a fully supported continuous curve.
    const means = s.mean_import_w.map((value, i) => s.coverage_pct[i] === 100 ? value : null);
    const evMeans = s.mean_ev_w.map((value, i) => s.ev_coverage_pct[i] === 100 ? value : null);
    const hasEV = s.mean_ev_w.some(value => value !== null);
    const theme = getComputedStyle(this.target);
    const color = (token: string): string => theme.getPropertyValue(token).trim();
    const offPeak = rates.filter(rate => rate.band === "ECO7_NIGHT").map(rate => [
      new Date(rate.from).getTime() / 1000, new Date(rate.to).getTime() / 1000,
    ]);
    const visibleBands = (plot: uPlot): number[][] => offPeak.map(([from, to]) => [
      Math.max(from, plot.scales.x.min!), Math.min(to, plot.scales.x.max!),
    ]).filter(([from, to]) => from < to);
    const shadeOffPeak = (plot: uPlot): void => {
      const { ctx, bbox } = plot;
      ctx.save();
      ctx.beginPath(); ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height); ctx.clip();
      ctx.fillStyle = color("--chart-off-peak");
      for (const [from, to] of visibleBands(plot)) {
        const left = plot.valToPos(from, "x", true);
        ctx.fillRect(left, bbox.top, plot.valToPos(to, "x", true) - left, bbox.height);
      }
      ctx.restore();
    };
    const drawThreshold = (plot: uPlot): void => {
      const { ctx, bbox } = plot;
      const threshold = plot.scales.y.min != null && plot.scales.y.max != null
        && plot.scales.y.min <= 6000 && plot.scales.y.max >= 6000;
      this.indicators({ offPeak: visibleBands(plot).length > 0, threshold });
      if (!threshold) return;
      const y = plot.valToPos(6000, "y", true);
      const ratio = plot.ctx.canvas.width / plot.width;
      ctx.save();
      ctx.beginPath(); ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height); ctx.clip();
      ctx.strokeStyle = color("--chart-threshold"); ctx.lineWidth = ratio;
      ctx.setLineDash([6 * ratio, 4 * ratio]);
      ctx.beginPath(); ctx.moveTo(bbox.left, y); ctx.lineTo(bbox.left + bbox.width, y); ctx.stroke();
      ctx.fillStyle = color("--chart-threshold");
      ctx.font = `${11 * ratio}px Consolas, monospace`;
      ctx.textAlign = "right";
      ctx.fillText("6 kW", bbox.left + bbox.width - 6 * ratio, Math.max(bbox.top + 13 * ratio, y - 5 * ratio));
      ctx.restore();
    };
    const axis = {
      stroke: color("--muted"),
      grid: { stroke: color("--line"), width: 1 },
      ticks: { stroke: color("--line"), width: 1 },
      font: '11px "Cascadia Code", Consolas, monospace',
    };
    this.plot = new uPlot({
      width: Math.max(1, this.target.clientWidth), height: 340,
      cursor: { drag: { x: true, y: false } },
      scales: { x: { min: new Date(history.from).getTime() / 1000, max: new Date(history.to).getTime() / 1000 } },
      hooks: {
        drawClear: [shadeOffPeak], draw: [drawThreshold],
        setScale: [(plot, key) => {
          if (key !== "x" || plot.status !== 1) return;
          const { min, max } = plot.scales.x;
          if (min == null || max == null) return;
          const from = new Date(Math.round(min) * 1000), to = new Date(Math.round(max) * 1000);
          if (from >= to) return;
          // Finish uPlot's scale update before the range callback replaces the chart.
          queueMicrotask(() => {
            if (this.plot === plot) this.rangeChanged(from, to);
          });
        }],
      },
      series: [
        { label: "Bucket end" },
        { label: "Observed mean (W)", stroke: color("--chart-mean"), width: 2, spanGaps: false },
        { label: "Sampled peak (W)", stroke: color("--chart-peak"), width: 1, spanGaps: false },
        ...(hasEV ? [{ label: "EV charging (W)", stroke: color("--chart-ev"), width: 2, spanGaps: false }] : []),
      ],
      axes: [
        { ...axis },
        { ...axis, label: history.method.power_basis === "nonnegative_grid_import_excluding_ev"
          ? hasEV ? "Non-EV / EV power (W)" : "Non-EV grid import (W)"
          : hasEV ? "Grid / EV power (W)" : "Grid import (W)", size: 65 },
      ],
    }, [s.bucket_end_epoch_s, means, s.sampled_peak_w, ...(hasEV ? [evMeans] : [])], this.target);
  }
  clear(): void {
    this.plot?.destroy(); this.plot = undefined;
    this.indicators({ offPeak: false, threshold: false });
  }
  destroy(): void { this.observer.disconnect(); this.plot?.destroy(); }
}
