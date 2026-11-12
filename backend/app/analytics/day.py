from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.analytics.effective_rates import append_interval
from app.analytics.intervals import integrate_without_ev, supported_segments
from app.analytics.service import Analytics
from app.db.store import one
from app.home_assistant import ev_source
from app.models import EVInterval, iso, now_us, timestamp_us
from app.tariffs import TariffPricing

EVENT_LIMIT = 200


def calendar_bounds(day: str, timezone: str, at: int) -> tuple[int, int, int]:
    selected = date.fromisoformat(day)
    if selected.isoformat() != day or selected == date.max:
        raise ValueError("Expected a calendar date in YYYY-MM-DD format")
    zone = ZoneInfo(timezone)
    start = timestamp_us(datetime.combine(selected, time(), zone).isoformat())
    end = timestamp_us(datetime.combine(selected + timedelta(days=1), time(), zone).isoformat())
    if start < 0 or start >= at or end <= start:
        raise ValueError("Select a calendar day after 1970 that has already started")
    return start, min(end, at), end


def missing_periods(known: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    left = start
    for a, b in known:
        a, b = max(start, a), min(end, b)
        if a >= b:
            continue
        if a > left:
            result.append((left, a))
        left = max(left, b)
    if left < end:
        result.append((left, end))
    return result


async def daily_analysis(
    analytics: Analytics,
    pricing: TariffPricing,
    day: str,
    timezone: str,
    selected: str | None,
    exclude_ev: bool,
    *,
    at: int | None = None,
) -> dict[str, Any]:
    at = now_us() if at is None else at
    start, end, calendar_end = calendar_bounds(day, timezone, at)
    store = analytics.store
    async with store.read() as conn:
        device, version = await analytics.device(conn, selected)
        config = dict(await one(conn, "SELECT * FROM app_config WHERE singleton=1"))
        retained = await store.retention(conn, device)
        warnings: list[str] = []
        if start < retained["five_before_us"] and retained["archive_timezone"]:
            archive_zone = retained["archive_timezone"]
            if archive_zone != timezone:
                timezone = archive_zone
                start, end, calendar_end = calendar_bounds(day, timezone, at)
                warnings.append(
                    f"Daily archives use {timezone}; calendar bounds follow that timezone"
                )
        raw_available = start >= retained["raw_before_us"]
        ev_reason = None
        if device != config["active_device_id"] or not all(
            config[key] for key in ("home_assistant_url", "home_assistant_entity")
        ):
            ev_reason = "Configure Home Assistant for this active meter to enable EV exclusion"
        elif not raw_available:
            ev_reason = "EV exclusion is unavailable: retained raw meter/Ohme history has expired"
        elif end - start > store.policy.raw_days * 86_400_000_000:
            ev_reason = "EV exclusion requires a range within the configured raw retention"
        grid = await analytics.summary(start, end, timezone, device, connection=conn)
        adjusted = (
            await analytics.summary(start, end, timezone, device, True, connection=conn)
            if ev_reason is None
            else None
        )
        use_ev = exclude_ev and adjusted is not None
        history = await analytics.history(start, end, "auto", 1500, device, use_ev, connection=conn)
        grid_history = (
            await analytics.history(start, end, "auto", 1500, device, connection=conn)
            if use_ev
            else None
        )
        tariff = await pricing.status((start, end), device, connection=conn)
        events: list[dict[str, Any]] = []
        charging: list[tuple[int, int]] = []
        ev_known: list[tuple[int, int]] = []
        meter_known: list[tuple[int, int]] = []
        guards: list[tuple[int, int]] = []
        joint_seconds: float | None = None
        ev_seconds: float | None = None

        def add_event(kind: str, a: int, b: int, peak_w: int | None = None) -> None:
            events.append({"kind": kind, "from": iso(a), "to": iso(b), "peak_w": peak_w})

        if raw_available:
            rows = await store.raw_rows(conn, device, start, end)
            for a, b, _ in supported_segments(rows, start, end):
                append_interval(meter_known, a, b)
            for a, b in missing_periods(meter_known, start, end):
                add_event("meter_gap", a, b)
            ev: list[EVInterval] = []
            if ev_reason is None:
                ev = await store.ev_rows(conn, ev_source(config, device), start, end)
                for interval in ev:
                    a, b = max(start, interval.start), min(end, interval.end)
                    if a < b and interval.power_mw is not None:
                        append_interval(ev_known, a, b)
                        if interval.power_mw > 0:
                            append_interval(charging, a, b)
                ev_seconds = sum(b - a for a, b in ev_known) / 1_000_000
                joint_seconds = integrate_without_ev(rows, ev, start, end).observed_us / 1_000_000
                for a, b in missing_periods(ev_known, start, end):
                    add_event("ev_gap", a, b)
                guarded = await analytics.ev_exclusion_history(conn, ev_source(config), start, end)
                index = 0
                for original in ev:
                    if original.power_mw is None:
                        continue
                    while index < len(guarded) and guarded[index].end <= original.start:
                        index += 1
                    j = index
                    while j < len(guarded) and guarded[j].start < original.end:
                        interval = guarded[j]
                        a = max(start, original.start, interval.start)
                        b = min(end, original.end, interval.end)
                        if a < b and interval.power_mw is None:
                            append_interval(guards, a, b)
                        j += 1
                for a, b in guards:
                    add_event("ev_transition", a, b)
                ev = guarded
            episodes: list[tuple[int, int, int]] = []
            for a, b, power in supported_segments(rows, start, end, ev if use_ev else None):
                if power <= 6_000_000:
                    continue
                if episodes and episodes[-1][1] == a:
                    episodes[-1] = (episodes[-1][0], b, max(episodes[-1][2], power))
                else:
                    episodes.append((a, b, power))
            for a, b, peak in episodes:
                add_event("exceedance", a, b, peak // 1000)
        else:
            warnings.append(
                "Detailed events require retained raw readings; archived gaps are not reconstructed"
            )
        summary = adjusted if use_ev else grid
        assert summary is not None
        peak_at = timestamp_us(summary["sampled_peak_at"]) if summary["sampled_peak_at"] else None
        peak_overlaps_ev = (
            any(a <= peak_at < b for a, b in charging)
            if peak_at is not None and any(a <= peak_at < b for a, b in ev_known)
            else None
        )
        events.sort(key=lambda event: (event["from"], event["kind"]))
        return {
            "date": day,
            "today": datetime.fromisoformat(iso(at))
            .astimezone(ZoneInfo(timezone))
            .date()
            .isoformat(),
            "timezone": timezone,
            "from": iso(start),
            "to": iso(end),
            "calendar_to": iso(calendar_end),
            "partial": end < calendar_end,
            "device_id": device,
            "data_version": version,
            "config_revision": config["revision"],
            "ev_savings_from": config["ev_savings_from"],
            "exclude_ev": use_ev,
            "ev_unavailable_reason": ev_reason,
            "grid": grid,
            "adjusted": adjusted,
            "history": history,
            "grid_history": grid_history,
            "tariff": tariff,
            "charging": [{"from": iso(a), "to": iso(b)} for a, b in charging],
            "meter_gaps": [
                {"from": iso(a), "to": iso(b)} for a, b in missing_periods(meter_known, start, end)
            ]
            if raw_available
            else [],
            "transition_gaps": [{"from": iso(a), "to": iso(b)} for a, b in guards],
            "coverage": {
                "meter_seconds": grid["observed_seconds"],
                "ev_seconds": ev_seconds,
                "joint_seconds": joint_seconds,
                "adjusted_seconds": adjusted["observed_seconds"] if adjusted else None,
            },
            "peak_overlaps_ev": peak_overlaps_ev,
            "events": events[:EVENT_LIMIT],
            "event_count": len(events),
            "raw_available": raw_available,
            "recovery_available": (
                raw_available
                and device == config["active_device_id"]
                and bool(config["polling_enabled"])
                and start < at - 180_000_000
                and at - start <= min(store.policy.raw_days, 31) * 86_400_000_000
            ),
            "warnings": warnings + ([ev_reason] if exclude_ev and ev_reason else []),
        }
