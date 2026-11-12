import asyncio
import logging
import random
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.db.store import Store, one
from app.kraken import Kraken, KrakenError
from app.models import Reading, iso, now_us, timestamp_us
from app.storage import StoragePressure
from app.streaming import Hub

log = logging.getLogger(__name__)


def daily_recovery_window(at: int, timezone: str) -> tuple[str, int, int] | None:
    local_now = (
        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=at)
    ).astimezone(ZoneInfo(timezone))
    if local_now.hour == 0 and local_now.minute < 5:
        return None
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    zone = ZoneInfo(timezone)
    start = datetime.combine(yesterday, time.min, zone)
    end = datetime.combine(today, time.min, zone)
    return yesterday.isoformat(), timestamp_us(start.isoformat()), timestamp_us(end.isoformat())


class Poller:
    def __init__(self, store: Store, client: Kraken, hub: Hub) -> None:
        self.store, self.client, self.hub = store, client, hub
        self.changed = asyncio.Event()
        self.publish_lock = asyncio.Lock()
        self.health: dict[str, Any] = {
            "polling_state": "stopped",
            "telemetry_state": "missing",
            "storage_state": "ok",
            "last_success_at": None,
            "last_attempt_at": None,
            "last_stored_at": None,
            "last_read_at": None,
            "next_attempt_at": None,
            "consecutive_failures": 0,
            "error_code": None,
            "recovery_incomplete": False,
            "recovery_active": False,
            "disk_free_bytes": None,
            "database_bytes": None,
            "low_disk": False,
        }
        self.task: asyncio.Task[None] | None = None
        self.inflight: asyncio.Task[None] | None = None
        self.recovery_due = False
        self.ev_health: dict[str, Any] | None = None

    async def publish(self) -> None:
        async with self.publish_lock:
            await self._publish()

    async def _publish(self) -> None:
        config = await self.store.config()
        self.health.update(await self.store.capacity())
        self.health.update(await self.store.recovery_health(config["active_device_id"]))
        self.health["recovery_active"] = bool(
            self.health["recovery_pending"]
            and self.recovery_due
            and self.inflight is not None
            and not self.inflight.done()
        )
        latest = await self.store.latest(config["active_device_id"])
        data_version = 0
        if config["active_device_id"]:
            async with self.store.read() as conn:
                data_version = int(
                    (
                        await one(
                            conn,
                            "SELECT data_version FROM devices WHERE device_id=?",
                            (config["active_device_id"],),
                        )
                    )[0]
                )
        reading = None
        if latest:
            demand = latest["demand_mw"]
            reading = {
                "read_at": latest["read_at"],
                "demand_w": demand / 1000 if demand is not None else None,
                "import_demand_w": max(0, demand) / 1000 if demand is not None else None,
                "consumption_kwh": latest["consumption_mwh"] / 1_000_000
                if latest["consumption_mwh"] is not None
                else None,
                "quality": latest["quality"],
            }
            age = (now_us() - latest["read_at_us"]) / 1_000_000
            self.health["telemetry_state"] = (
                "fresh"
                if demand is not None and age <= max(120, 3 * config["poll_interval_seconds"])
                else "stale"
            )
            self.health["last_read_at"] = latest["read_at"]
        else:
            self.health["telemetry_state"], self.health["last_read_at"] = "missing", None
        if not config["polling_enabled"]:
            self.health["polling_state"] = "stopped"
        self.hub.publish(
            {
                "device_id": config["active_device_id"],
                "data_version": data_version,
                "reading": reading,
                "health": dict(self.health),
                "home_assistant": dict(self.ev_health) if self.ev_health is not None else None,
            }
        )

    async def reconfigure(self) -> None:
        self.changed.set()
        self.recovery_due = False
        if self.inflight is not None:
            self.inflight.cancel()
        await self.store.resume_recovery()
        self.health.update(error_code=None, consecutive_failures=0, next_attempt_at=None)
        await self.publish()

    async def collect(self, config: dict[str, Any]) -> None:
        if (await self.store.capacity())["storage_paused"]:
            raise StoragePressure("Storage capacity exhausted")
        key, device = config["octopus_api_key"], config["active_device_id"]
        self.health["last_attempt_at"] = iso(now_us())
        await self.client.verify(key, config["account_number"], device)
        at = now_us()
        daily = daily_recovery_window(at, config["display_timezone"])
        if daily is not None:
            day, start, end = daily
            await self.store.schedule_daily_recovery(
                device, day, start, end, config["revision"], at
            )
        latest = await self.store.latest(device)
        await self.store.plan_recovery(device, at, latest["read_at_us"] if latest else None)
        start = at - 180_000_000
        try:
            rows = await self.client.telemetry(
                key, device, iso(start), iso(at), config["poll_interval_seconds"]
            )
        except KrakenError as exc:
            if exc.code == "range_too_old":
                raise KrakenError("recent_range_unsupported") from exc
            raise
        readings: list[Reading] = []
        invalid = 0
        for row in rows:
            try:
                reading = Reading.from_kraken(row, at)
                # Kraken may round to a ten-second bucket just outside the request.
                if reading.read_at_us < start - 10_000_000 or reading.read_at_us > at:
                    raise ValueError("Reading outside requested range")
                readings.append(reading)
            except ValueError:
                invalid += 1
        changed = await self.store.ingest(device, readings, config["revision"])
        if changed:
            self.health["last_stored_at"] = iso(now_us())
        if invalid:
            log.warning("Rejected %s malformed telemetry records", invalid)
        if invalid and not readings:
            raise KrakenError("invalid_telemetry_records")
        self.health.update(
            polling_state="running",
            storage_state="ok",
            last_success_at=iso(now_us()),
            consecutive_failures=0,
            error_code="partial_invalid_records" if invalid else None,
        )
        log.info(
            "Collection completed: rows=%s changed=%s rejected=%s", len(rows), changed, invalid
        )

    async def recover(self, config: dict[str, Any]) -> None:
        if (await self.store.capacity())["storage_paused"]:
            raise StoragePressure("Storage capacity exhausted")
        device = config["active_device_id"]
        state = await self.store.recovery(device)
        if state is None or state["status"] != "pending":
            await self.collect(config)
            return
        # Skip already supported history without spending another upstream request.
        for _ in range(96):
            start = int(state["cursor_us"])
            end = min(start + 900_000_000, int(state["target_us"]))
            async with self.store.read() as conn:
                metrics = await self.store.raw_metrics(conn, device, start, end)
            if metrics.observed_us < end - start:
                break
            await self.store.finish_recovery_chunk(device, start, end, config["revision"], None)
            state = await self.store.recovery(device)
            if state is None or state["status"] != "pending":
                return
        else:
            return
        try:
            self.health["last_attempt_at"] = iso(now_us())
            await self.publish()
            rows = await self.client.telemetry(
                config["octopus_api_key"],
                device,
                iso(start - 10_000_000),
                iso(end + 10_000_000),
                config["poll_interval_seconds"],
            )
        except KrakenError as exc:
            if exc.code != "range_too_old":
                raise
            await self.store.finish_recovery_chunk(
                device,
                start,
                end,
                config["revision"],
                "range_too_old",
            )
            log.warning("Historical telemetry unavailable for requested recovery chunk")
            return
        readings: list[Reading] = []
        for row in rows:
            try:
                reading = Reading.from_kraken(row, now_us())
                if not start - 20_000_000 <= reading.read_at_us <= end + 20_000_000:
                    raise ValueError("Recovery reading outside requested range")
                readings.append(reading)
            except ValueError as exc:
                log.warning("Invalid recovery telemetry record")
                raise KrakenError("invalid_telemetry_records") from exc
        changed = await self.store.ingest(device, readings, config["revision"])
        if changed:
            self.health["last_stored_at"] = iso(now_us())
        async with self.store.read() as conn:
            metrics = await self.store.raw_metrics(conn, device, start, end)
        reason = (
            None
            if metrics.observed_us == end - start
            else "empty"
            if not readings and not metrics.observed_us
            else "partial"
        )
        await self.store.finish_recovery_chunk(device, start, end, config["revision"], reason)
        self.health.update(
            error_code=None,
            consecutive_failures=0,
            storage_state="ok",
            last_success_at=iso(now_us()),
        )
        log.info("Recovery chunk stored: rows=%s coverage=%s", len(readings), reason or "available")

    async def run(self) -> None:
        self.recovery_due = False
        while True:
            self.changed.clear()
            config = await self.store.config()
            delay: float = config["poll_interval_seconds"]
            if config["polling_enabled"]:
                try:
                    self.health["polling_state"] = "running"
                    operation = self.recover if self.recovery_due else self.collect
                    self.inflight = asyncio.create_task(operation(config))
                    await self.inflight
                    self.recovery_due = not self.recovery_due
                except asyncio.CancelledError:
                    if not self.changed.is_set():
                        raise
                    continue
                except KrakenError as exc:
                    self.health["consecutive_failures"] += 1
                    self.health["error_code"] = exc.code
                    failures = min(self.health["consecutive_failures"], 7)
                    delay = max(
                        delay, exc.delay, random.uniform(2.5, min(300, 5 * 2 ** (failures - 1)))
                    )
                    historical = self.recovery_due
                    self.recovery_due = False
                    if historical and not exc.retryable and not exc.auth:
                        await self.store.pause_recovery(
                            config["active_device_id"],
                            config["revision"],
                            exc.code,
                        )
                        self.health["polling_state"] = "running"
                    elif exc.retryable or exc.code == "range_too_old":
                        self.health["polling_state"] = "backoff"
                    else:
                        self.health["polling_state"] = (
                            "auth_error" if exc.auth else "configuration_error"
                        )
                        delay = 0
                    log.warning("Collection failed: code=%s retry_seconds=%s", exc.code, delay)
                except StoragePressure:
                    self.health.update(
                        polling_state="storage_paused",
                        storage_state="paused",
                        error_code="storage_limit_reached",
                    )
                    delay = max(delay, 60)
                except aiosqlite.Error:
                    self.recovery_due = False
                    self.health.update(storage_state="error", error_code="storage_unavailable")
                    log.exception("Collection storage failed")
                    delay = max(delay, 60)
                finally:
                    self.inflight = None
            else:
                delay = 0
                self.health["polling_state"] = "stopped"
            self.health["next_attempt_at"] = (
                iso(now_us() + int(delay * 1_000_000)) if delay else None
            )
            await self.publish()
            try:
                if delay:
                    await asyncio.wait_for(self.changed.wait(), delay)
                else:
                    await self.changed.wait()
            except TimeoutError:
                pass

    async def health_timer(self) -> None:
        while True:
            await self.publish()
            await asyncio.sleep(15)
