import asyncio
import contextlib
import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from app.analytics.service import Analytics
from app.db.store import Store, one
from app.kraken import Kraken, KrakenError
from app.main import create_app
from app.models import BUCKET_US, iso, now_us, timestamp_us
from app.poller import Poller
from app.settings import Settings
from app.storage import DAY_US, StoragePolicy, StoragePressure
from app.streaming import Hub
from conftest import DEVICE
from test_analytics import readings


async def drain(store: Store, at: int) -> None:
    for _ in range(100):
        if not await store.compact(at=at):
            return
    pytest.fail("Compaction failed to converge")


async def test_raw_retention_preserves_integrals_and_boundary_context(store: Store) -> None:
    start = timestamp_us("2026-01-01T00:00:00Z")
    await store.ingest(DEVICE, readings(start, 1800), 2)
    api = Analytics(store)
    before = await api.summary(start, start + 1800_000_000, "Europe/London")
    await drain(store, start + 31 * DAY_US)
    after = await api.summary(start, start + 1800_000_000, "Europe/London")
    for key in (
        "coverage_pct",
        "import_energy_kwh",
        "sampled_peak_w",
        "thresholds",
        "sample_count",
    ):
        assert after[key] == before[key]
    async with store.read() as conn:
        assert (await one(conn, "SELECT count(*) FROM readings"))[0] <= 4
        assert (await one(conn, "SELECT count(*) FROM dirty_rollup_buckets"))[0] == 0
        assert (await store.retention(conn, DEVICE))["raw_before_us"] == start + DAY_US
    assert await store.ingest(DEVICE, readings(start, 10, 9000), 2) == 0
    history = await api.history(start + 1, start + 1800_000_000 - 1, "auto", 1500)
    assert history["interval_seconds"] == 300
    assert history["from"] == iso(start)
    assert history["series"]["mean_import_w"] == [4000] * 6
    with pytest.raises(ValueError, match="five minutes"):
        await api.history(start, start + 1800_000_000, "1m", 1500)
    await store.rebuild()
    assert (await api.summary(start, start + 1800_000_000, "UTC"))["thresholds"] == before[
        "thresholds"
    ]


@pytest.mark.parametrize(
    ("left", "right", "hours"),
    [
        ("2026-03-29T00:00:00Z", "2026-03-29T23:00:00Z", 23),
        ("2026-10-24T23:00:00Z", "2026-10-26T00:00:00Z", 25),
    ],
)
async def test_daily_archive_dst_and_expiry(
    store: Store,
    left: str,
    right: str,
    hours: int,
) -> None:
    start, end = timestamp_us(left), timestamp_us(right)
    await store.ingest(DEVICE, readings(start, 600), 2)
    before = await Analytics(store).summary(start, end, "Europe/London")
    await drain(store, end + 397 * DAY_US)
    api = Analytics(store)
    after = await api.summary(start, end, "Europe/London")
    assert after["requested_seconds"] == hours * 3600
    assert after["import_energy_kwh"] == before["import_energy_kwh"]
    assert after["thresholds"] == before["thresholds"]
    assert after["sampled_peak_at"] == before["sampled_peak_at"]
    assert len(after["daily_peaks"]) == 1
    history = await api.history(start, end, "auto", 1500)
    assert history["bucket_alignment"] == "Europe/London"
    assert (
        history["series"]["bucket_end_epoch_s"][0] - history["series"]["bucket_start_epoch_s"][0]
        == hours * 3600
    )
    assert history["series"]["rolling_15m_w"] == [None]
    with pytest.raises(ValueError, match="calendar timezone"):
        await api.summary(start, end, "UTC")
    with pytest.raises(ValueError, match="Daily archives"):
        await api.history(start, end, "5m", 1500)
    async with store.read() as conn:
        assert (await one(conn, "SELECT count(*) FROM daily_archive"))[0] >= 1
        assert (await one(conn, "SELECT count(*) FROM rollup_5m WHERE bucket_start_us<?", (end,)))[
            0
        ] == 0
    await drain(store, end + 3654 * DAY_US)
    expired = await api.summary(start, end, "Europe/London")
    assert expired["import_energy_kwh"] is None
    assert any("expired" in w for w in expired["warnings"])


async def test_failed_summary_rolls_back_before_deleting_raw(store: Store) -> None:
    start = timestamp_us("2026-01-01T00:00:00Z")
    await store.ingest(DEVICE, readings(start, 600), 2)
    async with store.write() as conn:
        await conn.execute(
            "CREATE TRIGGER fail_rollup BEFORE INSERT ON rollup_5m "
            "BEGIN SELECT RAISE(ABORT,'synthetic summary failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic summary failure"):
        await store.compact(at=start + 31 * DAY_US)
    async with store.read() as conn:
        assert (await one(conn, "SELECT count(*) FROM readings"))[0] == 61
        assert (await one(conn, "SELECT count(*) FROM retention_state"))[0] == 0


async def test_storage_sidecars_reserve_and_hysteresis(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.policy = replace(store.policy, budget_bytes=8 * 1024**2, reserve_bytes=0)
    # A journal file models extra storage not included in the main database size.
    journal = Path(str(store.path) + "-journal")
    await asyncio.to_thread(journal.write_bytes, b"\0" * (8 * 1024**2))
    try:
        status = await store.capacity()
        assert status["database_bytes"] >= 8 * 1024**2
        assert status["storage_paused"]
        with pytest.raises(StoragePressure):
            await store.ingest(DEVICE, readings(now_us() - BUCKET_US, 10), 2)
    finally:
        await asyncio.to_thread(journal.unlink)
    async with store.lock:
        await store.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert not (await store.capacity())["storage_paused"]
    import shutil

    usage = shutil.disk_usage(store.path.parent)
    monkeypatch.setattr("app.db.store.shutil.disk_usage", lambda _: usage._replace(free=0))
    store.policy = replace(store.policy, reserve_bytes=1024)
    assert (await store.capacity())["storage_paused"]


async def test_bounded_filesystem_and_sqlite_page_limit(tmp_path: Path) -> None:
    policy = StoragePolicy(budget_bytes=8 * 1024**2, reserve_bytes=0)
    bounded = Store(tmp_path / "rejected.sqlite3", replace(policy, require_bounded_filesystem=True))
    with pytest.raises(RuntimeError, match="dedicated filesystem"):
        await bounded.open()
    store = Store(tmp_path / "bounded.sqlite3", policy)
    await store.open()
    try:
        async with store.write() as conn:
            await conn.execute("CREATE TABLE payload(value BLOB)")
        with pytest.raises(sqlite3.OperationalError, match="full"):
            async with store.write() as conn:
                await conn.execute("INSERT INTO payload VALUES(zeroblob(?))", (8 * 1024**2,))
        assert store.path.stat().st_size <= policy.budget_bytes * 3 // 4
        assert (await one(store.writer, "PRAGMA max_page_count"))[0] == store.page_limit
    finally:
        await store.close()


async def test_recovery_restart_dedup_and_revision_fence(store: Store) -> None:
    at = now_us()
    await store.plan_recovery(DEVICE, at, at - 3600_000_000)
    state = await store.recovery(DEVICE)
    assert state is not None
    start = state["cursor_us"]
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["variables"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "smartMeterTelemetry": [
                        {"readAt": iso(start + i * 1_000_000), "demand": 4000}
                        for i in range(0, 911, 10)
                    ]
                }
            },
        )

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client.owner = hashlib.sha256(b"test-key").hexdigest()
    client.access, client.expires = "synthetic", time.time() + 3600
    try:
        config = await store.config()
        await Poller(store, client, Hub()).recover(config)
        updated = await store.recovery(DEVICE)
        assert updated is not None and updated["cursor_us"] == start + 900_000_000
        assert calls[0]["grouping"] == "TEN_SECONDS"
        async with store.read() as conn:
            assert (await one(conn, "SELECT count(*) FROM readings"))[0] == 92
        path = store.path
        await store.close()
        await store.open()
        assert (await store.recovery(DEVICE)) == updated
        assert store.path == path
        await store.finish_recovery_chunk(
            DEVICE, updated["cursor_us"], updated["target_us"], 1, None
        )
        assert (await store.recovery(DEVICE)) == updated
    finally:
        await client.close()


async def test_recovery_unavailable_empty_and_storage_pause(store: Store) -> None:
    at = now_us()
    await store.plan_recovery(DEVICE, at, at - 3600_000_000)
    client = Kraken()
    client.telemetry = AsyncMock(side_effect=KrakenError("range_too_old"))  # type: ignore[method-assign]
    poller = Poller(store, client, Hub())
    try:
        await poller.recover(await store.config())
        assert (await store.recovery_health(DEVICE))["recovery_gap_count"] == 1
        client.telemetry = AsyncMock(return_value=[])  # type: ignore[method-assign]
        await poller.recover(await store.config())
        assert (await store.recovery_health(DEVICE))["recovery_gap_count"] == 2
        store.capacity = AsyncMock(return_value={"storage_paused": True})  # type: ignore[method-assign]
        with pytest.raises(StoragePressure):
            await poller.recover(await store.config())
        assert client.telemetry.call_count == 1
    finally:
        await client.close()


async def test_storage_pause_is_published_without_stopping_worker(store: Store) -> None:
    original = store.capacity

    async def capacity() -> dict[str, object]:
        return {**await original(), "storage_paused": True}

    store.capacity = capacity  # type: ignore[method-assign]
    hub = Hub()
    queue = hub.subscribe()
    queue.get_nowait()
    client = Kraken()
    poller = Poller(store, client, hub)
    task = asyncio.create_task(poller.run())
    try:
        async with asyncio.timeout(3):
            _, snapshot = await queue.get()
        assert snapshot["health"]["polling_state"] == "storage_paused"
        assert not task.done()
        assert (await store.recovery(DEVICE)) is None
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await client.close()


async def test_pinned_wal_pauses_and_checkpoint_allows_resume(tmp_path: Path) -> None:
    from app.models import ConfigUpdate
    from pydantic import SecretStr

    store = Store(
        tmp_path / "pinned.sqlite3",
        StoragePolicy(budget_bytes=8 * 1024**2, reserve_bytes=0),
    )
    await store.open()
    try:
        await store.save_config(
            ConfigUpdate(
                expected_revision=1,
                account_number="A-TEST",
                active_device_id=DEVICE,
                api_key=SecretStr("test-key"),
                polling_enabled=True,
            )
        )
        start = now_us() - DAY_US
        async with store.read() as reader:
            await one(reader, "SELECT count(*) FROM readings")
            # Repeated corrections pin WAL without growing the main history.
            for i in range(100):
                try:
                    await store.ingest(DEVICE, readings(start, 9000, 4000 + i), 2)
                except StoragePressure:
                    break
            else:
                pytest.fail("Pinned WAL did not trigger the storage guard")
            assert (await store.capacity())["storage_paused"]
            assert (await asyncio.to_thread(Path(str(store.path) + "-wal").stat)).st_size > 0
        async with store.lock:
            await store.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert not (await store.capacity())["storage_paused"]
        assert await store.ingest(DEVICE, readings(start, 10, 7000), 2) == 2
    finally:
        await store.close()


async def test_collection_before_login_and_after_logout(tmp_path: Path) -> None:
    from app.models import ConfigUpdate
    from pydantic import SecretStr

    password = tmp_path / "password"
    password.write_text("a-long-test-password")
    client = Kraken()
    client.verify = AsyncMock(return_value=None)  # type: ignore[method-assign]
    client.telemetry = AsyncMock(
        return_value=[
            {  # type: ignore[method-assign]
                "readAt": iso(now_us() - 10_000_000),
                "demand": 4000,
            }
        ]
    )
    app = create_app(
        Settings(
            database=tmp_path / "app.sqlite3",
            password_file=password,
            secure=False,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
        ),
        client,
    )
    # Configure before startup; no browser session exists.
    await app.state.store.open()
    await app.state.store.save_config(
        ConfigUpdate(
            expected_revision=1,
            account_number="A-TEST",
            active_device_id=DEVICE,
            api_key=SecretStr("test-key"),
            polling_enabled=True,
        )
    )
    await app.state.store.close()
    queue = app.state.hub.subscribe()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as browser,
    ):
        async with asyncio.timeout(3):
            while True:
                _, snapshot = await queue.get()
                if snapshot["reading"] is not None:
                    break
        assert (await browser.get("/api/config")).status_code == 401
        login = await browser.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": "a-long-test-password",
            },
        )
        browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await browser.post("/api/auth/logout")).status_code == 200
        assert (await browser.get("/api/storage")).status_code == 401
        client.telemetry.return_value = [
            {
                "readAt": iso(now_us() - 10_000_000),
                "demand": 5000,
            }
        ]
        await app.state.poller.reconfigure()
        async with asyncio.timeout(3):
            while True:
                _, snapshot = await queue.get()
                if snapshot["reading"] and snapshot["reading"]["demand_w"] == 5000:
                    break
        assert (await app.state.store.config())["polling_enabled"]


async def test_recovery_clamps_horizon_and_does_not_repeat_finished_work(store: Store) -> None:
    at = now_us()
    await store.plan_recovery(DEVICE, at, at - 10 * DAY_US)
    state = await store.recovery(DEVICE)
    assert state is not None
    assert state["cursor_us"] == at - DAY_US
    assert (await store.recovery_health(DEVICE))["recovery_gap_count"] == 1
    await store.finish_recovery_chunk(
        DEVICE,
        state["cursor_us"],
        state["target_us"],
        2,
        None,
    )
    await store.plan_recovery(DEVICE, at + 60_000_000, at - 10 * DAY_US)
    next_state = await store.recovery(DEVICE)
    assert next_state is not None and next_state["cursor_us"] == state["target_us"]
    assert next_state["target_us"] == state["target_us"] + 60_000_000


async def test_historical_failure_does_not_stop_live_collection(store: Store) -> None:
    at = now_us()
    await store.plan_recovery(DEVICE, at, at - 3600_000_000)
    client = Kraken()
    hub = Hub()
    queue = hub.subscribe()
    queue.get_nowait()
    poller = Poller(store, client, hub)
    poller.collect = AsyncMock()  # type: ignore[method-assign]
    poller.recover = AsyncMock(side_effect=KrakenError("invalid_telemetry_shape"))  # type: ignore[method-assign]
    task = asyncio.create_task(poller.run())
    try:
        async with asyncio.timeout(3):
            await queue.get()
            poller.changed.set()
            _, snapshot = await queue.get()
            assert snapshot["health"]["recovery_error"] == "invalid_telemetry_shape"
            assert snapshot["health"]["polling_state"] == "running"
            poller.changed.set()
            await queue.get()
        assert poller.collect.call_count == 2
        assert (await store.recovery(DEVICE))["status"] == "paused"
        await poller.reconfigure()
        assert (await store.recovery(DEVICE))["status"] == "pending"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await client.close()
