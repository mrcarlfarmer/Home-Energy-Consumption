import asyncio
import contextlib
import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from app.cli import execute
from app.db.store import Store, one
from app.kraken import Kraken
from app.models import ConfigUpdate, Reading, iso, now_us
from app.poller import Poller
from app.streaming import Hub
from conftest import DEVICE


async def test_native_keys_revision_fence_and_backup(store: Store, tmp_path: Path) -> None:
    at = now_us()
    row = Reading.from_kraken({"readAt": "2026-01-01T01:00:00+01:00", "demand": "0"}, at)
    duplicate = Reading.from_kraken({"readAt": "2026-01-01T00:00:00Z", "demand": "0"}, at)
    assert await store.ingest(DEVICE, [row, duplicate], 2) == 1
    assert await store.ingest(DEVICE, [row], 1) == 0
    async with store.read() as conn:
        assert (await one(conn, "PRAGMA journal_mode"))[0] == "wal"
        assert (await one(conn, "SELECT count(*) FROM readings"))[0] == 1
    await store.save_config(ConfigUpdate(expected_revision=2, polling_enabled=False))
    assert await store.ingest(DEVICE, [row], 3) == 0
    backup = tmp_path / "backup.sqlite3"
    await execute("backup", store.path, backup)
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 1


async def test_poller_collects_window_and_repeated_data_stays_stale(store: Store) -> None:
    at = now_us()

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "ObtainToken" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "obtainKrakenToken": {
                            "token": "test",
                            "payload": {"exp": time.time() + 3600},
                        }
                    }
                },
            )
        if "ElectricityDevices" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "account": {
                            "electricityAgreements": [
                                {
                                    "meterPoint": {
                                        "meters": [{"smartDevices": [{"deviceId": DEVICE}]}]
                                    },
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "smartMeterTelemetry": [
                        {"readAt": iso(at - i * 1_000_000), "demand": 4000} for i in (10, 30, 20)
                    ]
                }
            },
        )

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    poller = Poller(store, client, Hub())
    try:
        await poller.collect(await store.config())
        await poller.publish()
        assert poller.hub.current["reading"]["demand_w"] == 4000
        assert poller.health["telemetry_state"] == "fresh"
        async with store.read() as conn:
            assert (await one(conn, "SELECT count(*) FROM readings"))[0] == 3
        client.next_telemetry = 0
        await poller.collect(await store.config())
        assert poller.health["consecutive_failures"] == 0
    finally:
        await client.close()


async def test_read_cancellation_releases_connection(store: Store) -> None:
    async def expensive() -> None:
        async with store.read() as conn:
            await one(
                conn,
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n",
            )

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await expensive()
    async with store.read() as conn:
        assert (await one(conn, "SELECT 42"))[0] == 42
    assert store.readers.qsize() == 2


async def test_network_outage_enters_backoff_without_busy_retry(store: Store) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("synthetic outage")

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    hub = Hub()
    queue = hub.subscribe()
    queue.get_nowait()
    poller = Poller(store, client, hub)
    task = asyncio.create_task(poller.run())
    try:
        async with asyncio.timeout(3):
            _, snapshot = await queue.get()
        assert snapshot["health"]["polling_state"] == "backoff"
        assert snapshot["health"]["error_code"] == "network_unavailable"
        assert snapshot["health"]["next_attempt_at"] is not None
        assert calls == 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await client.close()
