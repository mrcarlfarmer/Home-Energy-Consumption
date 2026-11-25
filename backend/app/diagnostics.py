import asyncio
import logging
import sys
from typing import Any

import aiosqlite

from app.db.store import Store
from app.storage import StoragePressure

SAFE_MESSAGES = {
    "Collection started: from=%s to=%s",
    "Collection attempt started",
    "Collection completed: rows=%s changed=%s rejected=%s",
    "Recovery started: from=%s to=%s",
    "Recovery range stored: rows=%s",
    "Rejected %s malformed telemetry records",
    "Historical response too large; retrying in smaller ranges",
    "Historical range rejected; retrying in smaller windows",
    "Historical telemetry unavailable for requested recovery chunk",
    "Invalid recovery telemetry record",
    "Collection failed: code=%s retry_seconds=%s",
    "Collection storage failed",
    "Collection paused: storage capacity exhausted",
    "Home Assistant history contains unavailable power intervals",
    "Home Assistant collection failed: code=%s",
    "Home Assistant storage failed",
    "Rollup storage maintenance failed",
    "Tariff pricing refresh failed: code=%s",
    "Tariff pricing refresh paused by storage capacity",
    "Tariff pricing storage failed",
    "Background worker stopped unexpectedly: %s",
    "Database request failed: %s",
    "Request failed: code=%s status=%s",
    "Request validation failed",
    "Login disabled: anyone who can reach this service can view data and change settings. Restrict access to a trusted network.",
}


class Diagnostics(logging.Handler):
    def __init__(self, store: Store) -> None:
        super().__init__(logging.INFO)
        self.store = store
        self.queue: asyncio.Queue[tuple[int, str, str, str]] = asyncio.Queue(maxsize=2048)
        self.stopped = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and record.name != "app.poller":
            return
        message = (
            record.getMessage()
            if isinstance(record.msg, str) and record.msg in SAFE_MESSAGES
            else "Application error (details withheld)"
        )
        if record.exc_info and record.exc_info[0] is not None:
            message += f" ({record.exc_info[0].__name__})"
        event = (int(record.created * 1_000_000), record.levelname, record.name, message[:512])
        self.loop.call_soon_threadsafe(self.enqueue, event)

    def enqueue(self, event: tuple[int, str, str, str]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            print("Diagnostics queue full; an event was not recorded", file=sys.stderr)

    async def flush_events(self) -> None:
        events: list[tuple[int, str, str, str]] = []
        while not self.queue.empty() and len(events) < 100:
            events.append(self.queue.get_nowait())
        if not events:
            return
        try:
            if (await self.store.capacity())["storage_paused"]:
                raise StoragePressure("Diagnostics storage paused")
            await self.store.save_diagnostics(events)
        except (aiosqlite.Error, OSError, StoragePressure) as exc:
            self.dropped += len(events)
            print(f"Diagnostics could not be saved: {type(exc).__name__}", file=sys.stderr)

    async def run(self) -> None:
        while not self.stopped.is_set():
            try:
                await asyncio.wait_for(self.stopped.wait(), 1)
            except TimeoutError:
                pass
            await self.flush_events()

    async def close_events(self) -> None:
        await asyncio.sleep(0)
        while not self.queue.empty():
            await self.flush_events()

    async def results(self, before: int | None, limit: int) -> dict[str, Any]:
        return {"events": await self.store.diagnostics(before, limit), "dropped": self.dropped}
