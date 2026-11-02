import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from app.db.store import Store
from app.home_assistant import HomeAssistant, HomeAssistantError, ev_source
from app.models import iso, now_us, timestamp_us
from app.storage import DAY_US, StoragePressure

log = logging.getLogger(__name__)


class EVCollector:
    def __init__(
        self, store: Store, client: HomeAssistant, publish: Callable[[], Awaitable[None]]
    ) -> None:
        self.store, self.client, self.publish = store, client, publish
        self.changed = asyncio.Event()
        self.inflight: asyncio.Task[None] | None = None
        self.health: dict[str, Any] = {
            "state": "disabled",
            "last_success_at": None,
            "reported_at": None,
            "power_w": None,
            "error_code": None,
            "next_attempt_at": None,
            "recovery_cursor_at": None,
        }

    async def reconfigure(self) -> None:
        self.changed.set()
        if self.inflight is not None:
            self.inflight.cancel()
        self.client.reset_freshness_access()
        self.health.update(
            state="disabled",
            power_w=None,
            reported_at=None,
            last_success_at=None,
            error_code=None,
            next_attempt_at=None,
            recovery_cursor_at=None,
        )
        await self.publish()

    async def collect(self, config: dict[str, Any]) -> None:
        if (await self.store.capacity())["storage_paused"]:
            raise StoragePressure("Storage capacity exhausted")
        latest = await self.client.latest(config)
        self.health.update(power_w=latest["power_w"], reported_at=latest["reported_at"])
        if not latest["fresh"]:
            raise HomeAssistantError("home_assistant_stale_power", retryable=True)
        end = min(now_us(), timestamp_us(latest["reported_at"]))
        start = max(0, end - 180_000_000)
        intervals = await self.client.history(config, start, end)
        await self.store.save_ev_history(config, start, end, intervals)
        incomplete = any(row.power_mw is None for row in intervals)
        source = ev_source(config)
        cursor = await self.store.ev_cursor(source)
        cursor = max(
            cursor if cursor is not None else end - self.store.policy.recovery_days * DAY_US,
            end - self.store.policy.raw_days * DAY_US,
            0,
        )
        if cursor < start:
            target = min(cursor + 900_000_000, start)
            recovered = await self.client.history(config, cursor, target)
            await self.store.save_ev_history(config, cursor, target, recovered, cursor=target)
            incomplete = incomplete or any(row.power_mw is None for row in recovered)
            cursor = target
        self.health.update(
            state="partial" if incomplete else "running",
            last_success_at=iso(now_us()),
            error_code="home_assistant_incomplete_history" if incomplete else None,
            recovery_cursor_at=iso(cursor) if cursor < start else None,
        )
        if incomplete:
            log.warning("Home Assistant history contains unavailable power intervals")

    async def run(self) -> None:
        failures = 0
        while True:
            self.changed.clear()
            config = await self.store.config()
            delay = 0
            if config["home_assistant_enabled"]:
                delay = 60
                self.health["state"] = "collecting"
                try:
                    self.inflight = asyncio.create_task(self.collect(config))
                    await self.inflight
                    failures = 0
                except asyncio.CancelledError:
                    if not self.changed.is_set():
                        raise
                    continue
                except HomeAssistantError as exc:
                    failures += 1
                    delay = min(300, 60 * 2 ** min(failures - 1, 3)) if exc.retryable else 0
                    self.health.update(
                        state="backoff" if exc.retryable else "configuration_error",
                        error_code=exc.code,
                        power_w=None,
                    )
                    log.warning("Home Assistant collection failed: code=%s", exc.code)
                except StoragePressure:
                    self.health.update(
                        state="storage_paused", error_code="storage_limit_reached", power_w=None
                    )
                except aiosqlite.Error:
                    self.health.update(
                        state="storage_error", error_code="storage_unavailable", power_w=None
                    )
                    log.exception("Home Assistant storage failed")
                finally:
                    self.inflight = None
            else:
                self.health.update(state="disabled", power_w=None)
            self.health["next_attempt_at"] = iso(now_us() + delay * 1_000_000) if delay else None
            await self.publish()
            try:
                if delay:
                    await asyncio.wait_for(self.changed.wait(), delay)
                else:
                    await self.changed.wait()
            except TimeoutError:
                pass
