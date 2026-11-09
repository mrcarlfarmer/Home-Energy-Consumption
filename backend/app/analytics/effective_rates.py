import json
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.db.store import Store
from app.home_assistant import ev_source
from app.models import TariffRate, timestamp_us

HALF_HOUR_US = 1_800_000_000
HOUSEHOLD_WARNING = (
    "Estimated using effective household import rates including VAT, excluding standing "
    "charges and account-specific discounts"
)
BONUS_WARNING = (
    "Bonus half-hours are estimated from recorded Ohme power, assuming all charging uses "
    "Prioritise Savings (no Max/Boost or charging beyond the allowance). "
    "They are not supplier-confirmed billing slots. Unknown EV history is not a confirmed day rate."
)


@dataclass
class EffectiveRates:
    rates: list[TariffRate]
    revision: str
    adjustment_enabled: bool = False
    ev_coverage_pct: float | None = None
    charging: list[tuple[int, int]] = field(default_factory=list)
    unresolved: list[tuple[int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def append_interval(rows: list[tuple[int, int]], start: int, end: int) -> None:
    if start >= end:
        return
    if rows and rows[-1][1] == start:
        rows[-1] = (rows[-1][0], end)
    else:
        rows.append((start, end))


async def effective_rates(
    store: Store,
    conn: aiosqlite.Connection,
    config: dict[str, Any],
    device: str,
    start: int,
    end: int,
    snapshot: dict[str, Any] | None,
) -> EffectiveRates:
    async with conn.execute(
        "SELECT data_version FROM devices WHERE device_id=?", (device,)
    ) as cursor:
        row = await cursor.fetchone()
    revision = f"{config['revision']}:{row[0] if row else 0}:{snapshot['fetched_at_us'] if snapshot else 0}"
    base = await store.tariff_rows(conn, snapshot["source"], start, end) if snapshot else []
    result = EffectiveRates(base, revision)
    if not config["ev_savings_from"] or device != config["active_device_id"]:
        return result
    result.adjustment_enabled = True
    result.warnings.append(BONUS_WARNING)
    activation = timestamp_us(
        datetime.fromisoformat(config["ev_savings_from"])
        .replace(tzinfo=ZoneInfo("Europe/London"))
        .isoformat()
    )
    details = json.loads(snapshot["details_json"]) if snapshot else {}
    eligible_codes = {
        agreement["tariff_code"]
        for agreement in details.get("agreements", [])
        if agreement.get("smart_ev") is True
        or (
            "smart_ev" not in agreement
            and agreement["tariff_code"].startswith(("E-1R-INTELLI-", "E-2R-INTELLI-"))
        )
    }
    candidates = [
        rate
        for rate in base
        if rate.tariff_code in eligible_codes
        and rate.band in ("ECO7_DAY", "ECO7_NIGHT")
        and min(end, rate.end) > max(start, activation)
    ]
    if not candidates:
        if base and not eligible_codes:
            result.warnings.append(
                "No supported Intelligent Octopus agreement is identified in this cache. "
                "Fetch pricing again if the tariff has changed; published-schedule rates are unchanged."
            )
        return result
    left = max(start, activation, min(rate.start for rate in candidates))
    right = min(end, max(rate.end for rate in candidates))
    context_start = left // HALF_HOUR_US * HALF_HOUR_US
    context_end = (right + HALF_HOUR_US - 1) // HALF_HOUR_US * HALF_HOUR_US
    # Read the entire settlement slot: a charge outside the visible range can earn its rate.
    ev = await store.ev_rows(conn, ev_source(config, device), context_start, context_end)
    known_us = 0
    for interval in ev:
        a, b = max(left, interval.start), min(right, interval.end)
        if a < b and interval.power_mw is not None:
            known_us += b - a
            if interval.power_mw > 0:
                append_interval(result.charging, a, b)
    result.ev_coverage_pct = 100 * known_us / (right - left)
    ev_ends = [interval.end for interval in ev]
    nights: dict[str, list[TariffRate]] = {}
    for item in details.get("off_peak_rates", []):
        night = TariffRate(
            timestamp_us(item["from"]),
            timestamp_us(item["to"]),
            Decimal(item["pence_per_kwh"]),
            item["tariff_code"],
            "EV_BONUS",
        )
        nights.setdefault(night.tariff_code, []).append(night)
    for rows in nights.values():
        rows.sort(key=lambda rate: rate.start)
    result.rates = []
    for rate in base:
        a, limit = max(start, rate.start), min(end, rate.end)
        if a >= limit:
            continue
        if rate.tariff_code not in eligible_codes or rate.band != "ECO7_DAY" or limit <= activation:
            result.rates.append(
                TariffRate(a, limit, rate.pence_per_kwh, rate.tariff_code, rate.band)
            )
            continue
        if a < activation:
            result.rates.append(
                TariffRate(a, activation, rate.pence_per_kwh, rate.tariff_code, rate.band)
            )
            a = activation
        while a < limit:
            slot_start = a // HALF_HOUR_US * HALF_HOUR_US
            slot_end = slot_start + HALF_HOUR_US
            b = min(limit, slot_end)
            index = bisect_right(ev_ends, slot_start)
            covered, charging = 0, False
            while index < len(ev) and ev[index].start < slot_end:
                interval = ev[index]
                duration = min(slot_end, interval.end) - max(slot_start, interval.start)
                if duration > 0 and interval.power_mw is not None:
                    covered += duration
                    charging |= interval.power_mw > 0
                index += 1
            if charging:
                priced_to = a
                for night in nights.get(rate.tariff_code, []):
                    x, y = max(a, night.start), min(b, night.end)
                    if x < y:
                        append_interval(result.unresolved, priced_to, x)
                        result.rates.append(
                            TariffRate(x, y, night.pence_per_kwh, rate.tariff_code, "EV_BONUS")
                        )
                        priced_to = y
                append_interval(result.unresolved, priced_to, b)
            elif covered == HALF_HOUR_US:
                result.rates.append(
                    TariffRate(a, b, rate.pence_per_kwh, rate.tariff_code, rate.band)
                )
            else:
                append_interval(result.unresolved, a, b)
            a = b
    if result.unresolved:
        result.warnings.append(
            "Some daytime slots lack complete EV evidence or dated off-peak prices. "
            "Adjusted costs are unavailable where above-threshold energy intersects those slots. "
            "Future slots are not guaranteed: charging can still earn a bonus."
        )
    return result
