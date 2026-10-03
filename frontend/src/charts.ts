import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { History } from "./api";

export class DemandChart {
  private plot?: uPlot;
  private observer: ResizeObserver;
  constructor(private target: HTMLElement) {
    this.observer = new ResizeObserver(() => this.plot?.setSize({
      width: Math.max(280, target.clientWidth), height: 340,
    }));
    this.observer.observe(target);
  }
  update(history: History): void {
    this.plot?.destroy();
    const s = history.series;
    // Incomplete bucket means are not drawn as a fully supported continuous curve.
    const means = s.mean_import_w.map((value, i) => s.coverage_pct[i] === 100 ? value : null);
    this.plot = new uPlot({
      width: Math.max(280, this.target.clientWidth), height: 340,
      cursor: { drag: { x: true, y: false } },
      series: [
        { label: "Bucket end" },
        { label: "Observed mean (W)", stroke: "#087e73", width: 2, spanGaps: false },
        { label: "Sampled peak (W)", stroke: "#c9852c", width: 1, spanGaps: false },
        { label: "Trailing 15 min (W)", stroke: "#6263b5", width: 2, spanGaps: false },
      ],
      axes: [
        {},
        { label: "Grid import (W)", size: 65 },
      ],
    }, [s.bucket_end_epoch_s, means, s.sampled_peak_w, s.rolling_15m_w], this.target);
  }
  destroy(): void { this.observer.disconnect(); this.plot?.destroy(); }
}
