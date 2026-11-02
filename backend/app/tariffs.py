import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
import httpx

from app.db.store import Conflict, Store
from app.kraken import Kraken, KrakenError, retry_after
from app.models import TariffRate, iso, now_us, timestamp_us
from app.storage import DAY_US, StoragePressure

log = logging.getLogger(__name__)
HALF_HOUR_US = 1_800_000_000
REFRESH_SECONDS = 6 * 3600
PRICING_WARNING = (
    "Estimated using household import rates including VAT, excluding standing charges, "
    "EV-specific smart-charging adjustments and account-specific discounts"
)
AGREEMENTS = """query TariffAgreements($accountNumber:String!,$after:DateTime!) {
 account(accountNumber:$accountNumber) {
  electricityAgreements(active:true) {
   meterPoint {
    id direction
    meters(includeInactive:false) { smartDevices { deviceId } }
    agreements(includeInactive:true,validAfter:$after) {
     validFrom validTo isRevoked
     tariff { ... on TariffType { productCode tariffCode displayName isExport } }
     timeOfUseScheme {
      name timezone
      timeslots {
       timeslot activeFrom activeTo weekdays weekends saturdays sundays workdays holidays
       season { startDay startMonth endDay endMonth }
      }
     }
    }
   }
  }
 }
}"""


def rate_time(value: Any) -> int:
    if not isinstance(value, str):
        raise KrakenError("tariff_invalid_timestamp")
    try:
        result = timestamp_us(value)
    except ValueError as exc:
        raise KrakenError("tariff_invalid_timestamp") from exc
    if result < 0:
        raise KrakenError("tariff_invalid_timestamp")
    return result


def normalized(rows: list[TariffRate]) -> list[TariffRate]:
    result: list[TariffRate] = []
    for row in sorted(set(rows), key=lambda r: (r.start, r.end)):
        if result and row.start < result[-1].end:
            raise KrakenError("tariff_overlapping_rates")
        if (
            result
            and row.start == result[-1].end
            and (row.pence_per_kwh, row.tariff_code, row.band)
            == (result[-1].pence_per_kwh, result[-1].tariff_code, result[-1].band)
        ):
            previous = result.pop()
            row = TariffRate(previous.start, row.end, row.pence_per_kwh, row.tariff_code, row.band)
        result.append(row)
    return result


class OctopusTariffs:
    def __init__(self, client: Kraken) -> None:
        self.client = client

    async def rates(
        self, product: str, tariff: str, band: str, start: int, end: int
    ) -> list[TariffRate]:
        endpoint = {
            "STANDARD": "standard-unit-rates",
            "ECO7_DAY": "day-unit-rates",
            "ECO7_NIGHT": "night-unit-rates",
        }[band]
        url = (
            f"https://api.octopus.energy/v1/products/{product}/"
            f"electricity-tariffs/{tariff}/{endpoint}/"
        )
        result: list[TariffRate] = []
        for page in range(1, 5):
            try:
                async with (
                    asyncio.timeout(20),
                    self.client.http.stream(
                        "GET",
                        url,
                        params={
                            "period_from": iso(start),
                            "period_to": iso(end),
                            "page_size": "1500",
                            "page": str(page),
                        },
                        follow_redirects=False,
                    ) as response,
                ):
                    if response.status_code == 429 or response.status_code >= 500:
                        raise KrakenError(
                            "tariff_prices_unavailable",
                            retryable=True,
                            delay=max(60, retry_after(response.headers.get("Retry-After"))),
                        )
                    if response.status_code != 200:
                        raise KrakenError("tariff_prices_http_error")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > 1_048_576:
                            raise KrakenError("tariff_response_too_large")
                        content.extend(chunk)
                    data = json.loads(content)
            except (httpx.HTTPError, TimeoutError) as exc:
                raise KrakenError("tariff_network_error", retryable=True) from exc
            except (ValueError, UnicodeDecodeError) as exc:
                raise KrakenError("tariff_invalid_json") from exc
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("results"), list)
                or len(data["results"]) > 1500
                or "next" not in data
                or (data["next"] is not None and not isinstance(data["next"], str))
            ):
                raise KrakenError("tariff_invalid_rates")
            for row in data["results"]:
                if not isinstance(row, dict):
                    raise KrakenError("tariff_invalid_rates")
                if row.get("payment_method") is not None:
                    raise KrakenError("tariff_payment_method_unsupported")
                value = row.get("value_inc_vat")
                try:
                    price = Decimal(str(value))
                except InvalidOperation as exc:
                    raise KrakenError("tariff_invalid_price") from exc
                if isinstance(value, bool) or not price.is_finite() or abs(price) > 100_000:
                    raise KrakenError("tariff_invalid_price")
                a = rate_time(row.get("valid_from"))
                b = end if row.get("valid_to") is None else rate_time(row["valid_to"])
                if row.get("valid_to") is None and a >= end:
                    continue
                if b <= a:
                    raise KrakenError("tariff_invalid_timestamp")
                a, b = max(a, start), min(b, end)
                if a < b:
                    result.append(TariffRate(a, b, price, tariff, band))
            if data["next"] is None:
                return normalized(result)
        raise KrakenError("tariff_pagination_limit")

    def schedule(self, data: Any) -> tuple[ZoneInfo, list[tuple[str, int, int]]] | None:
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("timezone") not in ("Europe/London", "UTC"):
            raise KrakenError("tariff_schedule_unsupported")
        try:
            zone = ZoneInfo(data["timezone"])
        except ZoneInfoNotFoundError as exc:
            raise KrakenError("tariff_schedule_unsupported") from exc
        slots = data.get("timeslots")
        if not isinstance(slots, list) or len(slots) != 2:
            raise KrakenError("tariff_schedule_unsupported")
        parsed: list[tuple[str, int, int]] = []
        for slot in slots:
            if (
                not isinstance(slot, dict)
                or slot.get("timeslot") not in ("ECO7_DAY", "ECO7_NIGHT")
                or any(
                    slot.get(key) is not False
                    for key in (
                        "weekdays",
                        "weekends",
                        "saturdays",
                        "sundays",
                        "workdays",
                        "holidays",
                    )
                )
                or slot.get("season")
                != {"startDay": 1, "startMonth": 1, "endDay": 1, "endMonth": 1}
            ):
                raise KrakenError("tariff_schedule_unsupported")
            times: list[int] = []
            for key in ("activeFrom", "activeTo"):
                value = slot.get(key)
                if not isinstance(value, str) or not re.fullmatch(
                    r"(?:[01]\d|2[0-3]):(?:00|30):00", value
                ):
                    raise KrakenError("tariff_schedule_unsupported")
                times.append(int(value[:2]) * 60 + int(value[3:5]))
            if times[0] == times[1]:
                raise KrakenError("tariff_schedule_unsupported")
            parsed.append((slot["timeslot"], times[0], times[1]))
        if {band for band, _, _ in parsed} != {"ECO7_DAY", "ECO7_NIGHT"}:
            raise KrakenError("tariff_schedule_unsupported")
        for minute in range(0, 1440, 30):
            if (
                sum(a <= minute < b if a < b else minute >= a or minute < b for _, a, b in parsed)
                != 1
            ):
                raise KrakenError("tariff_schedule_unsupported")
        return zone, parsed

    async def fetch(
        self, config: dict[str, Any], start: int, end: int
    ) -> tuple[list[TariffRate], dict[str, Any]]:
        data = await self.client.authenticated(
            config["octopus_api_key"],
            AGREEMENTS,
            {"accountNumber": config["account_number"], "after": iso(start)},
        )
        try:
            outer = data["account"]["electricityAgreements"]
            if not isinstance(outer, list) or len(outer) > 100:
                raise ValueError
            points: dict[str, dict[str, Any]] = {}
            for agreement in outer:
                point = agreement["meterPoint"]
                if point["direction"] == "IMPORT" and any(
                    str(device["deviceId"]).upper() == config["active_device_id"]
                    for meter in point["meters"]
                    for device in meter["smartDevices"]
                ):
                    points[str(point["id"])] = point
            if len(points) != 1:
                raise KrakenError("tariff_meter_not_found_or_ambiguous")
            agreements = next(iter(points.values()))["agreements"]
            if not isinstance(agreements, list) or len(agreements) > 100:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise KrakenError("tariff_discovery_contract_error") from exc
        result: list[TariffRate] = []
        descriptions: list[dict[str, Any]] = []
        windows: list[tuple[int, int]] = []
        for agreement in agreements:
            if not isinstance(agreement, dict):
                raise KrakenError("tariff_discovery_contract_error")
            if agreement.get("isRevoked") is True:
                continue
            a = rate_time(agreement.get("validFrom"))
            b = end if agreement.get("validTo") is None else rate_time(agreement["validTo"])
            if agreement.get("validTo") is None and a >= end:
                continue
            if b <= a:
                raise KrakenError("tariff_invalid_timestamp")
            a, b = max(start, a), min(end, b)
            if a >= b:
                continue
            if any(a < right and b > left for left, right in windows):
                raise KrakenError("tariff_overlapping_agreements")
            windows.append((a, b))
            tariff = agreement.get("tariff")
            if not isinstance(tariff, dict) or tariff.get("isExport") is not False:
                raise KrakenError("tariff_discovery_contract_error")
            product, code, name = (
                tariff.get(k) for k in ("productCode", "tariffCode", "displayName")
            )
            if (
                not isinstance(product, str)
                or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,99}", product)
                or not isinstance(code, str)
                or not re.fullmatch(r"E-[12]R-" + re.escape(product) + r"-[A-P]", code)
                or not isinstance(name, str)
                or not 1 <= len(name) <= 200
            ):
                raise KrakenError("tariff_discovery_contract_error")
            schedule = self.schedule(agreement.get("timeOfUseScheme"))
            if schedule is None:
                result.extend(await self.rates(product, code, "STANDARD", a, b))
                description = "Time-stamped standard unit rates"
            else:
                zone, slots = schedule
                bands = {band: await self.rates(product, code, band, a, b) for band, _, _ in slots}
                indices = {band: 0 for band in bands}
                for left in range(a // HALF_HOUR_US * HALF_HOUR_US, b, HALF_HOUR_US):
                    local = datetime.fromisoformat(iso(left)).astimezone(zone)
                    minute = local.hour * 60 + local.minute
                    band = next(
                        band
                        for band, x, y in slots
                        if (x <= minute < y if x < y else minute >= x or minute < y)
                    )
                    rows = bands[band]
                    while indices[band] < len(rows) and rows[indices[band]].end <= left:
                        indices[band] += 1
                    j = indices[band]
                    while j < len(rows) and rows[j].start < left + HALF_HOUR_US:
                        row = rows[j]
                        x, y = max(a, left, row.start), min(b, left + HALF_HOUR_US, row.end)
                        if x < y:
                            result.append(TariffRate(x, y, row.pence_per_kwh, code, band))
                        j += 1
                description = f"API day/night schedule ({zone.key})"
            descriptions.append(
                {
                    "from": iso(a),
                    "to": iso(b),
                    "name": name,
                    "tariff_code": code,
                    "schedule": description,
                }
            )
        if not result:
            raise KrakenError("tariff_rates_missing")
        return normalized(result), {"agreements": descriptions, "warnings": [PRICING_WARNING]}


class TariffPricing:
    def __init__(
        self, store: Store, client: Kraken, publish: Callable[[], Awaitable[None]]
    ) -> None:
        self.store, self.provider, self.publish = store, OctopusTariffs(client), publish
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.inflight: asyncio.Task[None] | None = None
        self.not_before = 0.0
        self.error_code: str | None = None

    async def reconfigure(self) -> None:
        self.changed.set()
        if self.inflight is not None:
            self.inflight.cancel()
        self.error_code = None

    async def refresh(self, expected_revision: int | None = None) -> None:
        if self.lock.locked():
            raise KrakenError("tariff_refresh_busy", retryable=True, delay=30)
        async with self.lock:
            remaining = self.not_before - time.monotonic()
            if remaining > 0:
                raise KrakenError(
                    "tariff_refresh_throttled", retryable=True, delay=math.ceil(remaining)
                )
            config = await self.store.config()
            if expected_revision is not None and expected_revision != config["revision"]:
                raise Conflict("Settings changed; reload before fetching tariff pricing")
            if not all(
                config[x] for x in ("octopus_api_key", "account_number", "active_device_id")
            ):
                raise ValueError(
                    "Save your Octopus API key, account and meter before fetching pricing"
                )
            if (await self.store.capacity())["storage_paused"]:
                raise StoragePressure("Storage capacity exhausted")
            self.not_before = time.monotonic() + 60
            end = now_us() + 2 * DAY_US
            start = end - (min(self.store.policy.raw_days, 31) + 2) * DAY_US
            try:
                async with asyncio.timeout(90):
                    rates, details = await self.provider.fetch(config, start, end)
                    await self.store.save_tariff_rates(config, start, end, rates, details)
            except TimeoutError as exc:
                self.error_code = "tariff_refresh_timeout"
                raise KrakenError(self.error_code, retryable=True) from exc
            except KrakenError as exc:
                self.error_code = exc.code
                self.not_before = max(self.not_before, time.monotonic() + exc.delay)
                log.warning("Tariff pricing refresh failed: code=%s", exc.code)
                raise
            self.error_code = None
            self.changed.set()
            await self.publish()

    async def status(
        self, interval: tuple[int, int] | None = None, selected: str | None = None
    ) -> dict[str, Any]:
        config = await self.store.config()
        start, end = interval if interval is not None else (now_us(), now_us() + DAY_US)
        async with self.store.read() as conn:
            snapshot = (
                await self.store.tariff_snapshot(conn, config)
                if selected is None or selected == config["active_device_id"]
                else None
            )
            rates = (
                await self.store.tariff_rows(conn, snapshot["source"], start, end)
                if snapshot
                else []
            )
        details = json.loads(snapshot["details_json"]) if snapshot else {}
        at = now_us()
        current = next(
            (
                item
                for item in details.get("agreements", [])
                if timestamp_us(item["from"]) <= at < timestamp_us(item["to"])
            ),
            None,
        )
        return {
            "enabled": bool(config["tariff_pricing_enabled"]),
            "state": "refreshing"
            if self.lock.locked()
            else "error"
            if self.error_code
            else "ready"
            if rates
            else "rates_unavailable"
            if snapshot
            else "not_fetched",
            "error_code": self.error_code,
            "fetched_at": iso(snapshot["fetched_at_us"]) if snapshot else None,
            "from": iso(snapshot["start_us"]) if snapshot else None,
            "to": iso(snapshot["end_us"]) if snapshot else None,
            "tariff_name": current["name"] if current else None,
            "tariff_code": current["tariff_code"] if current else None,
            "schedule": current["schedule"] if current else None,
            "rates": [
                {
                    "from": iso(row.start),
                    "to": iso(row.end),
                    "pence_per_kwh": float(row.pence_per_kwh),
                    "band": row.band,
                }
                for row in rates
            ],
            "warnings": details.get("warnings", [])
            + (
                ["No cached household rates cover the requested period. Fetch pricing again."]
                if snapshot and not rates
                else []
            ),
        }

    async def run(self) -> None:
        while True:
            self.changed.clear()
            config = await self.store.config()
            delay = 0.0
            if config["tariff_pricing_enabled"]:
                async with self.store.read() as conn:
                    snapshot = await self.store.tariff_snapshot(conn, config)
                delay = (
                    max(0, (snapshot["fetched_at_us"] - now_us()) / 1_000_000 + REFRESH_SECONDS)
                    if snapshot
                    else 0
                )
                if not delay:
                    try:
                        self.inflight = asyncio.create_task(self.refresh())
                        await self.inflight
                        delay = REFRESH_SECONDS
                    except asyncio.CancelledError:
                        task = asyncio.current_task()
                        if task is None or task.cancelling() or not self.changed.is_set():
                            raise
                        continue
                    except KrakenError as exc:
                        delay = max(300, exc.delay) if exc.retryable else 0
                    except Conflict:
                        delay = 60
                    except StoragePressure:
                        self.error_code, delay = "storage_limit_reached", 300
                        log.warning("Tariff pricing refresh paused by storage capacity")
                    except aiosqlite.Error:
                        self.error_code, delay = "storage_unavailable", 300
                        log.exception("Tariff pricing storage failed")
                    finally:
                        self.inflight = None
            try:
                if delay:
                    await asyncio.wait_for(self.changed.wait(), delay)
                else:
                    await self.changed.wait()
            except TimeoutError:
                pass
