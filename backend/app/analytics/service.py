from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.analytics.intervals import Metrics, integrate
from app.db.store import Store, one
from app.models import ALGORITHM_VERSION, BUCKET_US, GAP_US, THRESHOLDS, iso, timestamp_us

INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "6h": 21600, "1d": 86400}
METHOD = {
    "power_basis": "nonnegative_grid_import",
    "source_grouping": "TEN_SECONDS",
    "integration": "bounded_previous_sample_hold",
    "max_gap_seconds": 30,
    "algorithm_version": ALGORITHM_VERSION,
}


class Rebuilding(Exception):
    pass


def bounds(start: str, end: str) -> tuple[int, int]:
    left, right = timestamp_us(start), timestamp_us(end)
    if left < 0 or right <= left or right - left > 366 * 86400_000_000:
        raise ValueError("Range must be positive, after 1970 and at most 366 days")
    return left, right


def slots(start: int, end: int, step: int) -> list[tuple[int, int]]:
    return [(max(start, t), min(end, t + step)) for t in range(start // step * step, end, step)]


def interval_for(start: int, end: int, interval: str, maximum: int) -> int:
    if not 1 <= maximum <= 2000:
        raise ValueError("max_points must be between 1 and 2000")
    for name, seconds in INTERVALS.items():
        if interval not in ("auto", name):
            continue
        step = seconds * 1_000_000
        count = (end - 1) // step - start // step + 1
        if count <= maximum:
            return step
        if interval != "auto":
            break
    raise ValueError("Interval exceeds max_points; use auto, a coarser interval or a shorter range")


def describe(m: Metrics, duration: int) -> dict[str, Any]:
    return {
        "requested_seconds": duration / 1_000_000,
        "observed_seconds": m.observed_us / 1_000_000,
        "coverage_pct": 100 * m.observed_us / duration,
        "import_energy_kwh": m.energy_ws / 3_600_000 if m.observed_us else None,
        "sample_count": m.samples,
        "invalid_sample_count": m.invalid,
        "sampled_peak_w": m.peak_mw / 1000 if m.peak_mw is not None else None,
        "sampled_peak_at": iso(m.peak_at) if m.peak_at is not None else None,
    }


class Analytics:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def device(self, conn: aiosqlite.Connection, selected: str | None) -> tuple[str, int]:
        device = selected or (await one(conn, "SELECT active_device_id FROM app_config"))[0]
        if not device:
            raise ValueError("Configure an electricity meter first")
        row = await one(conn, "SELECT data_version FROM devices WHERE device_id=?", (device,))
        return str(device), int(row[0])

    async def scan(
        self,
        conn: aiosqlite.Connection,
        device: str,
        start: int,
        end: int,
        budget: list[int],
    ) -> AsyncIterator[tuple[int, Metrics]]:
        left = start // BUCKET_US * BUCKET_US
        sql = """
        WITH keys AS (
          SELECT bucket_start_us FROM rollup_5m WHERE device_id=? AND bucket_start_us>=? AND bucket_start_us<?
          UNION SELECT bucket_start_us FROM dirty_rollup_buckets WHERE device_id=? AND bucket_start_us>=? AND bucket_start_us<?
        )
        SELECT k.bucket_start_us AS bucket, r.*, d.bucket_start_us AS dirty,
          t0.above_us AS a0,t0.load_above_watt_seconds AS l0,t0.excess_watt_seconds AS e0,
          t1.above_us AS a1,t1.load_above_watt_seconds AS l1,t1.excess_watt_seconds AS e1,
          t2.above_us AS a2,t2.load_above_watt_seconds AS l2,t2.excess_watt_seconds AS e2
        FROM keys k
        LEFT JOIN rollup_5m r ON r.device_id=? AND r.bucket_start_us=k.bucket_start_us
        LEFT JOIN dirty_rollup_buckets d ON d.device_id=? AND d.bucket_start_us=k.bucket_start_us
        LEFT JOIN rollup_threshold_5m t0 ON t0.device_id=r.device_id AND t0.bucket_start_us=r.bucket_start_us AND t0.threshold_w=3680
        LEFT JOIN rollup_threshold_5m t1 ON t1.device_id=r.device_id AND t1.bucket_start_us=r.bucket_start_us AND t1.threshold_w=5000
        LEFT JOIN rollup_threshold_5m t2 ON t2.device_id=r.device_id AND t2.bucket_start_us=r.bucket_start_us AND t2.threshold_w=6000
        ORDER BY k.bucket_start_us
        """
        async with conn.execute(sql, (device, left, end, device, left, end, device, device)) as cur:
            while rows := await cur.fetchmany(128):
                for row in rows:
                    bucket = int(row["bucket"])
                    a, b = max(start, bucket), min(end, bucket + BUCKET_US)
                    if (
                        row["dirty"] is not None
                        or row["algorithm_version"] != ALGORITHM_VERSION
                        or row["a0"] is None
                        or a != bucket
                        or b != bucket + BUCKET_US
                    ):
                        if (
                            row["dirty"] is not None
                            or row["algorithm_version"] != ALGORITHM_VERSION
                        ):
                            budget[0] -= 1
                            if budget[0] < 0:
                                raise Rebuilding("Historical rollups are rebuilding; retry shortly")
                        yield bucket, await self.store.raw_metrics(conn, device, a, b)
                    else:
                        yield (
                            bucket,
                            Metrics(
                                observed_us=row["observed_us"],
                                energy_ws=row["import_watt_seconds"],
                                samples=row["sample_count"],
                                invalid=row["invalid_sample_count"],
                                peak_mw=row["peak_import_mw"],
                                peak_at=row["peak_at_us"],
                                above=[row[f"a{j}"] for j in range(3)],
                                load=[row[f"l{j}"] for j in range(3)],
                                excess=[row[f"e{j}"] for j in range(3)],
                            ),
                        )

    async def aggregate(
        self,
        conn: aiosqlite.Connection,
        device: str,
        start: int,
        end: int,
        budget: list[int],
    ) -> Metrics:
        result = Metrics()
        async for _, m in self.scan(conn, device, start, end, budget):
            result.merge(m)
        return result

    async def summary(
        self,
        start: int,
        end: int,
        timezone: str,
        selected: str | None = None,
    ) -> dict[str, Any]:
        zone = ZoneInfo(timezone)
        days: list[dict[str, Any]] = []
        total = Metrics()
        budget = [120]
        async with self.store.read() as conn:
            device, version = await self.device(conn, selected)
            date = datetime.fromisoformat(iso(start)).astimezone(zone).date()
            left = start
            while left < end:
                midnight = datetime.combine(date + timedelta(days=1), time(), zone).astimezone(UTC)
                right = min(end, timestamp_us(midnight.isoformat()))
                m = await self.aggregate(conn, device, left, right, budget)
                total.merge(m)
                days.append(
                    {
                        "date": date.isoformat(),
                        "from": iso(left),
                        "to": iso(right),
                        **describe(m, right - left),
                    }
                )
                left, date = right, date + timedelta(days=1)
        thresholds = [
            {
                "threshold_w": threshold,
                "seconds_above": total.above[j] / 1_000_000,
                "time_above_pct": 100 * total.above[j] / total.observed_us
                if total.observed_us
                else None,
                "energy_when_above_kwh": total.load[j] / 3_600_000 if total.observed_us else None,
                "excess_energy_kwh": total.excess[j] / 3_600_000 if total.observed_us else None,
            }
            for j, threshold in enumerate(THRESHOLDS)
        ]
        return {
            "device_id": device,
            "data_version": version,
            "from": iso(start),
            "to": iso(end),
            "timezone": timezone,
            **describe(total, end - start),
            "thresholds": thresholds,
            "daily_peaks": days,
            "method": METHOD,
            "warnings": ["Incomplete telemetry coverage"]
            if total.observed_us < end - start
            else [],
        }

    async def history(
        self,
        start: int,
        end: int,
        interval: str,
        maximum: int,
        selected: str | None = None,
    ) -> dict[str, Any]:
        step = interval_for(start, end, interval, maximum)
        series: dict[str, list[float | None]] = {
            key: []
            for key in (
                "bucket_start_epoch_s",
                "bucket_end_epoch_s",
                "mean_import_w",
                "sampled_peak_w",
                "peak_at_epoch_s",
                "rolling_15m_w",
                "observed_seconds",
                "coverage_pct",
                "rolling_15m_coverage_pct",
            )
        }
        budget = [120]
        async with self.store.read() as conn:
            device, version = await self.device(conn, selected)
            raw = (
                await self.store.raw_rows(conn, device, start - 900_000_000, end)
                if step < BUCKET_US
                else None
            )
            times = [r[0] for r in raw] if raw is not None else []

            async def metric(a: int, b: int) -> Metrics:
                if raw is not None:
                    return integrate(
                        raw[bisect_left(times, a - GAP_US) : bisect_right(times, b + GAP_US)], a, b
                    )
                return await self.aggregate(conn, device, a, b, budget)

            for a, b in slots(start, end, step):
                m = await metric(a, b)
                rolling = await metric(b - 900_000_000, b)
                values: tuple[float | None, ...] = (
                    a / 1_000_000,
                    b / 1_000_000,
                    m.energy_ws * 1_000_000 / m.observed_us if m.observed_us else None,
                    m.peak_mw / 1000 if m.peak_mw is not None else None,
                    m.peak_at / 1_000_000 if m.peak_at is not None else None,
                    rolling.energy_ws / 900 if rolling.observed_us == 900_000_000 else None,
                    m.observed_us / 1_000_000,
                    100 * m.observed_us / (b - a),
                    100 * rolling.observed_us / 900_000_000,
                )
                for key, value in zip(series, values, strict=True):
                    series[key].append(value)
        return {
            "device_id": device,
            "data_version": version,
            "from": iso(start),
            "to": iso(end),
            "interval_seconds": step // 1_000_000,
            "bucket_alignment": "UTC",
            "rolling_evaluation": "bucket_end",
            "series": series,
            "method": METHOD,
            "warnings": ["Peaks are sampled grid import, not continuous household load"],
        }
