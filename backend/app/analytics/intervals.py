from collections.abc import Iterable
from dataclasses import dataclass, field

from app.models import GAP_US, THRESHOLDS, EVInterval


@dataclass
class Metrics:
    observed_us: int = 0
    energy_ws: float = 0
    samples: int = 0
    invalid: int = 0
    peak_mw: int | None = None
    peak_at: int | None = None
    above: list[int] = field(default_factory=lambda: [0] * len(THRESHOLDS))
    load: list[float] = field(default_factory=lambda: [0.0] * len(THRESHOLDS))
    excess: list[float] = field(default_factory=lambda: [0.0] * len(THRESHOLDS))

    def add_duration(self, power_mw: int, duration: int) -> None:
        watts = max(0, power_mw) / 1000
        seconds = duration / 1_000_000
        self.observed_us += duration
        self.energy_ws += watts * seconds
        for j, threshold in enumerate(THRESHOLDS):
            if watts > threshold:
                self.above[j] += duration
                self.load[j] += watts * seconds
                self.excess[j] += (watts - threshold) * seconds

    def record_peak(self, at: int, power_mw: int) -> None:
        power_mw = max(0, power_mw)
        if (
            self.peak_mw is None
            or power_mw > self.peak_mw
            or (power_mw == self.peak_mw and (self.peak_at is None or at < self.peak_at))
        ):
            self.peak_mw, self.peak_at = power_mw, at

    def merge(self, other: "Metrics") -> None:
        self.observed_us += other.observed_us
        self.energy_ws += other.energy_ws
        self.samples += other.samples
        self.invalid += other.invalid
        if other.peak_mw is not None and (
            self.peak_mw is None
            or other.peak_mw > self.peak_mw
            or (
                other.peak_mw == self.peak_mw
                and other.peak_at is not None
                and (self.peak_at is None or other.peak_at < self.peak_at)
            )
        ):
            self.peak_mw, self.peak_at = other.peak_mw, other.peak_at
        for j in range(len(THRESHOLDS)):
            self.above[j] += other.above[j]
            self.load[j] += other.load[j]
            self.excess[j] += other.excess[j]


def integrate(rows: Iterable[tuple[int, int | None]], start: int, end: int) -> Metrics:
    result = Metrics()
    previous: tuple[int, int | None] | None = None
    for time, demand in rows:
        if start <= time < end:
            if demand is None:
                result.invalid += 1
            else:
                result.samples += 1
                imported = max(0, demand)
                if result.peak_mw is None or imported > result.peak_mw:
                    result.peak_mw, result.peak_at = imported, time
        if previous is not None:
            before, power = previous
            duration = min(time, end) - max(before, start)
            if (
                duration > 0
                and 0 < time - before <= GAP_US
                and power is not None
                and demand is not None
            ):
                result.add_duration(power, duration)
        previous = (time, demand)
    return result


def integrate_without_ev(
    rows: list[tuple[int, int | None]],
    intervals: list[EVInterval],
    start: int,
    end: int,
) -> Metrics:
    result = Metrics()
    index = 0
    for at, demand in rows:
        while index < len(intervals) and intervals[index].end <= at:
            index += 1
        ev = intervals[index] if index < len(intervals) else None
        if start <= at < end:
            if demand is None or ev is None or ev.start > at or ev.power_mw is None:
                result.invalid += 1
            else:
                result.samples += 1
                result.record_peak(at, demand - ev.power_mw)
    index = 0
    for (before, power_mw), (after, next_power) in zip(rows, rows[1:], strict=False):
        if power_mw is None or next_power is None or not 0 < after - before <= GAP_US:
            continue
        left, right = max(start, before), min(end, after)
        if left >= right:
            continue
        while index < len(intervals) and intervals[index].end <= left:
            index += 1
        j = index
        while j < len(intervals) and intervals[j].start < right:
            ev = intervals[j]
            a, b = max(left, ev.start), min(right, ev.end)
            if a < b and ev.power_mw is not None:
                adjusted = max(0, power_mw - ev.power_mw)
                result.add_duration(adjusted, b - a)
                result.record_peak(a, adjusted)
            j += 1
    return result
