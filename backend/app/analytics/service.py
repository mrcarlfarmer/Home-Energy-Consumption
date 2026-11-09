import json
from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.analytics.effective_rates import HOUSEHOLD_WARNING, effective_rates
from app.analytics.intervals import (
    EV_TRANSITION_GUARD_US,
    Metrics,
    ev_exclusion_intervals,
    integrate,
    integrate_without_ev,
    supported_segments,
)
from app.analytics.pricing import PricedThreshold
from app.db.store import Store, one
from app.home_assistant import ev_source
from app.models import (
    ALGORITHM_VERSION,
    BUCKET_US,
    GAP_US,
    THRESHOLDS,
    EVInterval,
    iso,
    timestamp_us,
)
from app.storage import DAY_US
from app.tariffs import PRICING_WARNING

INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "6h": 21600, "1d": 86400}
METHOD = {
    "power_basis": "nonnegative_grid_import",
    "source_grouping": "TEN_SECONDS",
    "integration": "bounded_previous_sample_hold",
    "max_gap_seconds": 30,
    "algorithm_version": ALGORITHM_VERSION,
}
EV_METHOD = {
    **METHOD,
    "power_basis": "nonnegative_grid_import_excluding_ev",
    "integration": "meter_previous_sample_hold_minus_recorded_ev_state_hold_with_start_stop_gaps",
}
EV_WARNINGS = [
    "EV exclusion is a recorded-state estimate, not total household load",
    "The 60 seconds before and after recorded EV charging starts/stops are unknown "
    "because meter and Ohme updates are not synchronized; coverage and costs exclude these periods",
    "Unreported Home Assistant outages may leave stale recorded states; unavailable states are excluded",
]


class Rebuilding(Exception):
    pass


def bounds(start: str, end: str) -> tuple[int, int]:
    left, right = timestamp_us(start), timestamp_us(end)
    if left < 0 or right <= left or right - left > 3653 * 86400_000_000:
        raise ValueError("Range must be positive, after 1970 and at most 3653 days")
    return left, right


def slots(start: int, end: int, step: int) -> list[tuple[int, int]]:
    return [(max(start, t), min(end, t + step)) for t in range(start // step * step, end, step)]


def interval_for(start: int, end: int, interval: str, maximum: int) -> int:
    if not 1 <= maximum <= 4000:
        raise ValueError("max_points must be between 1 and 4000")
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

    async def retained_range(
        self,
        conn: aiosqlite.Connection,
        device: str,
        start: int,
        end: int,
        timezone: str | None = None,
    ) -> tuple[int, int, dict[str, Any], list[str]]:
        state = await self.store.retention(conn, device)
        warnings: list[str] = []
        original = (start, end)
        if start < state["five_before_us"]:
            archive_zone = state["archive_timezone"] or "UTC"
            if timezone is not None and timezone != archive_zone:
                raise ValueError(
                    f"Daily archives use {archive_zone}; request that calendar timezone"
                )
            zone = ZoneInfo(archive_zone)

            def midnight(value: int, ceiling: bool) -> int:
                date = datetime.fromisoformat(iso(value)).astimezone(zone).date()
                floor = timestamp_us(datetime.combine(date, time(), zone).isoformat())
                if ceiling and value != floor:
                    floor = timestamp_us(
                        datetime.combine(date + timedelta(days=1), time(), zone).isoformat()
                    )
                return floor

            start = midnight(start, False)
            # Use whole local days across mixed daily/five-minute ranges too.
            end = midnight(end, True)
            warnings.append(f"Daily archived resolution; calendar alignment: {archive_zone}")
        elif start < state["raw_before_us"]:
            start = start // BUCKET_US * BUCKET_US
            if end < state["raw_before_us"]:
                end = (end + BUCKET_US - 1) // BUCKET_US * BUCKET_US
            warnings.append("Raw readings expired; minimum historical resolution is five minutes")
        if (start, end) != original:
            warnings.append("Requested range expanded to retained bucket boundaries; see from/to")
        if start < state["expired_before_us"]:
            warnings.append("Some requested history has expired under the retention policy")
        return start, end, state, warnings

    async def ev_selection(
        self,
        conn: aiosqlite.Connection,
        device: str,
        start: int,
        end: int,
        exclude_ev: bool,
        *,
        allow_expired: bool = False,
    ) -> str | None:
        if not exclude_ev:
            return None
        config = dict(await one(conn, "SELECT * FROM app_config WHERE singleton=1"))
        if device != config["active_device_id"] or not all(
            config[x] for x in ("home_assistant_url", "home_assistant_entity")
        ):
            raise ValueError(
                "Configure Home Assistant for the selected meter to exclude EV charging"
            )
        retained = await self.store.retention(conn, device)
        if not allow_expired and (
            start < retained["raw_before_us"] or end - start > self.store.policy.raw_days * DAY_US
        ):
            raise ValueError(
                "EV exclusion requires retained raw readings; select a range within raw retention"
            )
        return ev_source(config, device)

    async def scan(
        self,
        conn: aiosqlite.Connection,
        device: str,
        start: int,
        end: int,
        budget: list[int],
    ) -> AsyncIterator[tuple[int, Metrics]]:
        state = await self.store.retention(conn, device)
        async with conn.execute(
            "SELECT * FROM daily_archive WHERE device_id=? AND start_us<? AND end_us>? ORDER BY start_us",
            (device, end, start),
        ) as archive:
            async for row in archive:
                if start > row["start_us"] or end < row["end_us"]:
                    raise ValueError("Daily archives require whole calendar-day boundaries")
                yield int(row["start_us"]), Metrics(**json.loads(row["metrics_json"]))
        start = max(start, int(state["five_before_us"]))
        if start >= end:
            return
        left = start // BUCKET_US * BUCKET_US
        sql = """
        WITH keys AS (
          SELECT bucket_start_us FROM rollup_5m WHERE device_id=? AND bucket_start_us>=? AND bucket_start_us<?
          UNION SELECT bucket_start_us FROM dirty_rollup_buckets WHERE device_id=? AND bucket_start_us>=? AND bucket_start_us<?
        )
        SELECT k.bucket_start_us AS bucket, r.*, d.bucket_start_us AS dirty,
          t.above_us AS a0,t.load_above_watt_seconds AS l0,t.excess_watt_seconds AS e0
        FROM keys k
        LEFT JOIN rollup_5m r ON r.device_id=? AND r.bucket_start_us=k.bucket_start_us
        LEFT JOIN dirty_rollup_buckets d ON d.device_id=? AND d.bucket_start_us=k.bucket_start_us
        LEFT JOIN rollup_threshold_5m t ON t.device_id=r.device_id AND t.bucket_start_us=r.bucket_start_us AND t.threshold_w=?
        ORDER BY k.bucket_start_us
        """
        async with conn.execute(
            sql, (device, left, end, device, left, end, device, device, THRESHOLDS[0])
        ) as cur:
            while rows := await cur.fetchmany(128):
                for row in rows:
                    bucket = int(row["bucket"])
                    a, b = max(start, bucket), min(end, bucket + BUCKET_US)
                    if (
                        row["dirty"] is not None
                        or (
                            row["algorithm_version"] != ALGORITHM_VERSION
                            and bucket >= state["raw_before_us"]
                        )
                        or row["a0"] is None
                        or a != bucket
                        or b != bucket + BUCKET_US
                    ):
                        if bucket < state["raw_before_us"]:
                            raise ValueError(
                                "Archived five-minute data requires whole bucket boundaries"
                            )
                        if (
                            row["dirty"] is not None
                            or row["algorithm_version"] != ALGORITHM_VERSION
                        ):
                            budget[0] -= 1
                            if budget[0] < 0:
                                raise Rebuilding("Historical rollups are rebuilding; retry shortly")
                        yield bucket, await self.store.raw_metrics(conn, device, a, b)
                    else:
                        yield bucket, self.store.rollup_metrics(row)

    async def ev_exclusion_history(
        self, conn: aiosqlite.Connection, source: str, start: int, end: int
    ) -> list[EVInterval]:
        # Include neighbouring transitions even when they lie outside a bucket/day/query.
        return ev_exclusion_intervals(
            await self.store.ev_rows(
                conn,
                source,
                max(0, start - EV_TRANSITION_GUARD_US),
                end + EV_TRANSITION_GUARD_US,
            )
        )

    async def aggregate(
        self,
        conn: aiosqlite.Connection,
        device: str,
        start: int,
        end: int,
        budget: list[int],
        source: str | None = None,
    ) -> Metrics:
        if source is not None:
            return integrate_without_ev(
                await self.store.raw_rows(conn, device, start, end),
                await self.ev_exclusion_history(conn, source, start, end),
                start,
                end,
            )
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
        exclude_ev: bool = False,
    ) -> dict[str, Any]:
        zone = ZoneInfo(timezone)
        days: list[dict[str, Any]] = []
        total = Metrics()
        budget = [120]
        async with self.store.read() as conn:
            device, version = await self.device(conn, selected)
            source = await self.ev_selection(conn, device, start, end, exclude_ev)
            start, end, _, warnings = await self.retained_range(
                conn,
                device,
                start,
                end,
                timezone,
            )
            date = datetime.fromisoformat(iso(start)).astimezone(zone).date()
            left = start
            while left < end:
                midnight = datetime.combine(date + timedelta(days=1), time(), zone).astimezone(UTC)
                right = min(end, timestamp_us(midnight.isoformat()))
                m = await self.aggregate(conn, device, left, right, budget, source)
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
            pricing = await self.price_threshold(conn, device, start, end, total, budget, source)
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
            "pricing": pricing,
            "method": EV_METHOD if source is not None else METHOD,
            "warnings": warnings
            + (EV_WARNINGS if source is not None else [])
            + (["Incomplete telemetry coverage"] if total.observed_us < end - start else []),
        }

    async def daily_peaks(
        self, end: int, timezone: str, selected: str | None = None, exclude_ev: bool = False
    ) -> dict[str, Any]:
        if end <= 0:
            raise ValueError("Select an end time after 1970")
        zone = ZoneInfo(timezone)
        date = datetime.fromisoformat(iso(end - 1)).astimezone(zone).date() - timedelta(days=4)
        start = max(0, timestamp_us(datetime.combine(date, time(), zone).isoformat()))
        days: list[dict[str, Any]] = []
        budget = [120]
        async with self.store.read() as conn:
            device, _ = await self.device(conn, selected)
            source = await self.ev_selection(
                conn, device, start, end, exclude_ev, allow_expired=True
            )
            if source is None:
                start, end, retained, warnings = await self.retained_range(
                    conn, device, start, end, timezone
                )
            else:
                retained = await self.store.retention(conn, device)
                warnings = list(EV_WARNINGS)
                if start < retained["raw_before_us"]:
                    warnings.append("Expired EV history is unavailable, not zero")
            left = start
            date = datetime.fromisoformat(iso(start)).astimezone(zone).date()
            while left < end:
                right = min(
                    end,
                    timestamp_us(
                        datetime.combine(date + timedelta(days=1), time(), zone).isoformat()
                    ),
                )
                pricing_start = left
                if source is not None:
                    supported_start = max(left, int(retained["raw_before_us"]))
                    pricing_start = supported_start
                    m = (
                        await self.aggregate(conn, device, supported_start, right, budget, source)
                        if supported_start < right
                        else Metrics()
                    )
                else:
                    m = await self.aggregate(conn, device, left, right, budget)
                days.append(
                    {
                        "date": date.isoformat(),
                        "from": iso(left),
                        "to": iso(right),
                        **describe(m, right - left),
                        "pricing": await self.price_threshold(
                            conn, device, pricing_start, right, m, budget, source
                        ),
                    }
                )
                left, date = right, date + timedelta(days=1)
        return {
            "device_id": device,
            "from": iso(start),
            "to": iso(end),
            "timezone": timezone,
            "daily_peaks": days,
            "warnings": warnings,
        }

    async def price_threshold(
        self,
        conn: aiosqlite.Connection,
        device: str,
        start: int,
        end: int,
        total: Metrics,
        budget: list[int],
        source: str | None,
    ) -> dict[str, Any]:
        config = dict(await one(conn, "SELECT * FROM app_config WHERE singleton=1"))
        result: dict[str, Any] = {
            "enabled": bool(config["tariff_pricing_enabled"]),
            "available": False,
            "energy_when_above_gbp": None,
            "excess_energy_gbp": None,
            "price_coverage_pct": None,
            "priced_seconds_above": 0,
            "fetched_at": None,
            "warning": "Enable tariff pricing in Settings to estimate costs",
        }
        if not result["enabled"]:
            return result
        result["warning"] = PRICING_WARNING
        if device != config["active_device_id"]:
            result["warning"] = "Tariff pricing is only available for the configured active meter"
            return result
        snapshot = await self.store.tariff_snapshot(conn, config)
        if snapshot is None:
            result["warning"] = "Fetch Octopus tariff pricing in Settings before estimating costs"
            return result
        result["fetched_at"] = iso(snapshot["fetched_at_us"])
        if not total.observed_us:
            result["warning"] = "No observed energy is available to price"
            return result
        resolved = await effective_rates(self.store, conn, config, device, start, end, snapshot)
        rates = resolved.rates
        result["pricing_revision"] = resolved.revision
        if resolved.adjustment_enabled:
            result["warning"] = HOUSEHOLD_WARNING + ". " + " ".join(resolved.warnings)
        cost = PricedThreshold()
        if source is not None:
            for left in range(start, end, DAY_US):
                right = min(end, left + DAY_US)
                raw = await self.store.raw_rows(conn, device, left, right)
                ev = await self.ev_exclusion_history(conn, source, left, right)
                cost.add_segments(supported_segments(raw, left, right, ev), rates)
        else:
            retained = await self.store.retention(conn, device)
            ends = [row.end for row in rates]
            async for bucket, metrics in self.scan(conn, device, start, end, budget):
                if not metrics.above[0]:
                    continue
                left = max(start, bucket)
                if bucket < retained["five_before_us"]:
                    zone = ZoneInfo(retained["archive_timezone"] or "UTC")
                    date = datetime.fromisoformat(iso(bucket)).astimezone(zone).date()
                    right = min(
                        end,
                        timestamp_us(
                            datetime.combine(date + timedelta(days=1), time(), zone).isoformat()
                        ),
                    )
                else:
                    right = min(end, bucket + BUCKET_US)
                index = bisect_right(ends, left)
                rate = rates[index] if index < len(rates) else None
                if rate is not None and rate.start <= left and rate.end >= right:
                    cost.add_metrics(metrics, rate)
                elif left >= retained["raw_before_us"]:
                    raw = await self.store.raw_rows(conn, device, left, right)
                    cost.add_segments(supported_segments(raw, left, right), rates)
        complete = cost.priced_us == total.above[0]
        result.update(
            available=complete,
            energy_when_above_gbp=float(cost.load_gbp) if complete else None,
            excess_energy_gbp=float(cost.excess_gbp) if complete else None,
            price_coverage_pct=100 * cost.priced_us / total.above[0] if total.above[0] else 100,
            priced_seconds_above=cost.priced_us / 1_000_000,
        )
        if not complete:
            result["warning"] = (
                "Costs unavailable: some observed above-threshold energy lacks applicable "
                "rates, EV eligibility evidence or sufficiently detailed retained readings. "
                + result["warning"]
            )
        return result

    async def history(
        self,
        start: int,
        end: int,
        interval: str,
        maximum: int,
        selected: str | None = None,
        exclude_ev: bool = False,
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
            source = await self.ev_selection(conn, device, start, end, exclude_ev)
            start, end, retained, warnings = await self.retained_range(conn, device, start, end)
            daily = start < retained["five_before_us"]
            if daily:
                if interval not in ("auto", "1d"):
                    raise ValueError("Daily archives only support interval=auto or 1d")
                step = 86_400_000_000
            elif start < retained["raw_before_us"] and step < BUCKET_US:
                if interval != "auto":
                    raise ValueError(
                        "Raw readings expired; request an interval of at least five minutes"
                    )
                step = BUCKET_US
            chart_slots = slots(start, end, step)
            if daily:
                chart_slots = []
                zone = ZoneInfo(retained["archive_timezone"] or "UTC")
                a = start
                while a < end:
                    date = datetime.fromisoformat(iso(a)).astimezone(zone).date()
                    b = timestamp_us(
                        datetime.combine(date + timedelta(days=1), time(), zone).isoformat()
                    )
                    chart_slots.append((a, min(b, end)))
                    a = b
            if len(chart_slots) > maximum:
                raise ValueError(
                    "Range exceeds max_points at retained resolution; request a shorter range"
                )
            raw = (
                await self.store.raw_rows(conn, device, start - 900_000_000, end)
                if step < BUCKET_US and source is None
                else None
            )
            times = [r[0] for r in raw] if raw is not None else []
            ev_raw: list[tuple[int, int | None]] = []
            ev_intervals: list[EVInterval] = []
            ev_times: list[int] = []
            ev_starts: list[int] = []
            ev_ends: list[int] = []
            cache_left, cache_right = 0, 0

            async def metric(a: int, b: int) -> Metrics:
                nonlocal ev_raw, ev_intervals, ev_times, cache_left, cache_right
                nonlocal ev_starts, ev_ends
                if source is not None:
                    if a < retained["raw_before_us"]:
                        return Metrics()
                    if not cache_left <= a < b <= cache_right:
                        cache_left = max(retained["raw_before_us"], a - 900_000_000)
                        cache_right = max(b, min(end, a + DAY_US))
                        ev_raw = await self.store.raw_rows(conn, device, cache_left, cache_right)
                        ev_times = [r[0] for r in ev_raw]
                        ev_intervals = await self.ev_exclusion_history(
                            conn, source, cache_left, cache_right
                        )
                        ev_starts = [r.start for r in ev_intervals]
                        ev_ends = [r.end for r in ev_intervals]
                    return integrate_without_ev(
                        ev_raw[
                            bisect_left(ev_times, a - GAP_US) : bisect_right(ev_times, b + GAP_US)
                        ],
                        ev_intervals[bisect_right(ev_ends, a) : bisect_left(ev_starts, b)],
                        a,
                        b,
                    )
                if raw is not None:
                    return integrate(
                        raw[bisect_left(times, a - GAP_US) : bisect_right(times, b + GAP_US)], a, b
                    )
                return await self.aggregate(conn, device, a, b, budget)

            for a, b in chart_slots:
                m = await metric(a, b)
                rolling = (
                    Metrics()
                    if b - 900_000_000 < retained["five_before_us"]
                    else await metric(b - 900_000_000, b)
                )
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
            "bucket_alignment": retained["archive_timezone"] if daily else "UTC",
            "rolling_evaluation": "bucket_end",
            "series": series,
            "method": EV_METHOD if source is not None else METHOD,
            "warnings": warnings
            + (EV_WARNINGS if source is not None else [])
            + ["Peaks are sampled grid import, not continuous household load"]
            + (["Trailing 15-minute demand is unavailable in daily archives"] if daily else []),
        }
