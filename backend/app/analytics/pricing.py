from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.analytics.intervals import Metrics
from app.models import THRESHOLDS, TariffRate


@dataclass
class PricedThreshold:
    priced_us: int = 0
    load_gbp: Decimal = Decimal(0)
    excess_gbp: Decimal = Decimal(0)

    def add_metrics(self, metrics: Metrics, rate: TariffRate) -> None:
        self.priced_us += metrics.above[0]
        self.load_gbp += Decimal(str(metrics.load[0])) * rate.pence_per_kwh / 360_000_000
        self.excess_gbp += Decimal(str(metrics.excess[0])) * rate.pence_per_kwh / 360_000_000

    def add_segments(
        self, segments: Iterable[tuple[int, int, int]], rates: list[TariffRate]
    ) -> None:
        index = 0
        for left, right, power_mw in segments:
            watts = Decimal(power_mw) / 1000
            if watts <= THRESHOLDS[0]:
                continue
            while index < len(rates) and rates[index].end <= left:
                index += 1
            j = index
            while j < len(rates) and rates[j].start < right:
                rate = rates[j]
                a, b = max(left, rate.start), min(right, rate.end)
                if a < b:
                    self.priced_us += b - a
                    seconds = Decimal(b - a) / 1_000_000
                    self.load_gbp += watts * seconds * rate.pence_per_kwh / 360_000_000
                    self.excess_gbp += (
                        (watts - THRESHOLDS[0]) * seconds * rate.pence_per_kwh / 360_000_000
                    )
                j += 1
