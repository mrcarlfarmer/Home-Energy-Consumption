import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { History } from "./api";

export class DemandChart {
  private plot?: uPlot;
  private observer: ResizeObserver;
  constructor(private target: HTMLElement) {
    this.observer = new ResizeObserver(() => this.plot?.setSize({
      width: Math.max(1, target.clientWidth), height: 340,
    }));
    this.observer.observe(target);
  }
  update(history: History): void {
    this.plot?.destroy();
    const s = history.series;
    // Incomplete bucket means are not drawn as a fully supported continuous curve.
    const means = s.mean_import_w.map((value, i) => s.coverage_pct[i] === 100 ? value : null);
    const theme = getComputedStyle(this.target);
    const color = (token: string): string => theme.getPropertyValue(token).trim();
    const axis = {
      stroke: color("--muted"),
      grid: { stroke: color("--line"), width: 1 },
      ticks: { stroke: color("--line"), width: 1 },
      font: '11px "Cascadia Code", Consolas, monospace',
    };
    this.plot = new uPlot({
      width: Math.max(1, this.target.clientWidth), height: 340,
      cursor: { drag: { x: true, y: false } },
      series: [
        { label: "Bucket end" },
        { label: "Observed mean (W)", stroke: color("--chart-mean"), width: 2, spanGaps: false },
        { label: "Sampled peak (W)", stroke: color("--chart-peak"), width: 1, spanGaps: false },
        { label: "Trailing 15 min (W)", stroke: color("--chart-rolling"), width: 2, spanGaps: false },
      ],
      axes: [
        { ...axis },
        { ...axis, label: "Grid import (W)", size: 65 },
      ],
    }, [s.bucket_end_epoch_s, means, s.sampled_peak_w, s.rolling_15m_w], this.target);
  }
  destroy(): void { this.observer.disconnect(); this.plot?.destroy(); }
}
