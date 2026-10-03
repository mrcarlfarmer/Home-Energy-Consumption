from collections.abc import Iterable
from dataclasses import dataclass, field

from app.models import GAP_US, THRESHOLDS


@dataclass
class Metrics:
    observed_us: int = 0
    energy_ws: float = 0
    samples: int = 0
    invalid: int = 0
    peak_mw: int | None = None
    peak_at: int | None = None
    above: list[int] = field(default_factory=lambda: [0, 0, 0])
    load: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    excess: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])

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
        for j in range(3):
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
                watts = max(0, power) / 1000
                seconds = duration / 1_000_000
                result.observed_us += duration
                result.energy_ws += watts * seconds
                for j, threshold in enumerate(THRESHOLDS):
                    if watts > threshold:
                        result.above[j] += duration
                        result.load[j] += watts * seconds
                        result.excess[j] += (watts - threshold) * seconds
        previous = (time, demand)
    return result
