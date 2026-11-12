import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from app.analytics.service import Analytics
from app.api_models import Snapshot
from app.db.store import Conflict, Store, one
from app.kraken import Kraken, KrakenError
from app.main import create_app
from app.models import ConfigUpdate, iso, now_us, timestamp_us
from app.poller import Poller, daily_recovery_window
from app.settings import Settings
from app.storage import DAY_US
from app.streaming import Hub
from conftest import DEVICE
from pydantic import SecretStr
from test_analytics import readings


async def test_requested_recovery_repairs_internal_gaps_and_refreshes_metrics(store: Store) -> None:
    start = now_us() // 10_000_000 * 10_000_000 - 3 * 3600_000_000
    end = start + 7200_000_000
    rows = readings(start, 7200, 8000)
    await store.ingest(
        DEVICE,
        [r for r in rows if not start + 1800_000_000 < r.read_at_us < start + 2100_000_000],
        2,
    )
    await store.rebuild(100)
    analytics = Analytics(store)
    before = await analytics.summary(start, end, "UTC")
    assert before["coverage_pct"] < 100
    # A previous completed recovery does not discover holes behind its cursor.
    await store.plan_recovery(DEVICE, end + 180_000_000, start)
    await store.finish_recovery_chunk(DEVICE, start, end, 2, None)
    assert (await store.recovery(DEVICE))["status"] == "complete"
    await store.request_recovery(DEVICE, start, end, 2)
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO recovery_gaps VALUES(?,?,?,'partial')", (DEVICE, start, end)
        )
    client = Kraken()

    async def telemetry(
        key: str, device: str, left: str, right: str, interval: int
    ) -> list[dict[str, object]]:
        return [
            {"readAt": r.read_at, "demand": 8000}
            for r in rows
            if timestamp_us(left) <= r.read_at_us <= timestamp_us(right)
            and start + 1800_000_000 < r.read_at_us < start + 2100_000_000
        ]

    mock = AsyncMock(side_effect=telemetry)
    client.telemetry = mock  # type: ignore[method-assign]
    poller = Poller(store, client, Hub())
    try:
        await poller.recover(await store.config())
        await poller.recover(await store.config())
        assert mock.call_count == 1
        assert (await store.recovery(DEVICE))["status"] == "complete"
        assert (await store.recovery_health(DEVICE))["recovery_gap_count"] == 0
        after = await analytics.summary(start, end, "UTC")
        assert after["coverage_pct"] == 100
        assert after["import_energy_kwh"] == pytest.approx(16)
        assert after["thresholds"][0]["seconds_above"] == 7200
        assert after["thresholds"][0]["excess_energy_kwh"] == pytest.approx(4)
        chart = await analytics.history(start, end, "1m", 1500)
        assert chart["series"]["coverage_pct"] == [100] * len(chart["series"]["coverage_pct"])
        assert all(value == 8000 for value in chart["series"]["mean_import_w"])
    finally:
        await client.close()


async def test_requested_history_survives_restart_and_automatic_lookback(store: Store) -> None:
    at = now_us()
    start, end = at - 2 * DAY_US, at - DAY_US
    queued = await store.request_recovery(DEVICE, start, end, 2)
    await store.plan_recovery(DEVICE, at, at - 10_000_000)
    state = await store.recovery(DEVICE)
    assert state["cursor_us"] == start and state["requested"] == 1
    health = await store.recovery_health(DEVICE)
    assert health["recovery_state"] == "pending"
    assert health["recovery_last_progress_at"] is None
    await store.close()
    await store.open()
    assert await store.recovery(DEVICE) == state
    assert await store.recovery_health(DEVICE) == health
    assert queued["from"] == iso(start)
    # Preserve pending work when another selected range is requested.
    await store.request_recovery(DEVICE, at - 3600_000_000, at, 2)
    merged = await store.recovery(DEVICE)
    assert merged["cursor_us"] == start
    assert at - 180_000_000 <= merged["target_us"] <= now_us() - 180_000_000


@pytest.mark.parametrize(
    ("at", "day", "start", "end"),
    [
        (
            "2026-03-30T00:05:00+01:00",
            "2026-03-29",
            "2026-03-29T00:00:00Z",
            "2026-03-29T23:00:00Z",
        ),
        (
            "2026-10-26T00:05:00Z",
            "2026-10-25",
            "2026-10-24T23:00:00Z",
            "2026-10-26T00:00:00Z",
        ),
    ],
)
def test_daily_recovery_window_uses_previous_local_day_and_dst(
    at: str, day: str, start: str, end: str
) -> None:
    window = daily_recovery_window(timestamp_us(at), "Europe/London")
    assert window == (day, timestamp_us(start), timestamp_us(end))


def test_daily_recovery_waits_for_local_day_to_settle() -> None:
    assert daily_recovery_window(
        timestamp_us("2026-11-12T00:04:59Z"), "Europe/London"
    ) is None


async def test_daily_recovery_is_persisted_and_scheduled_once(store: Store) -> None:
    at = timestamp_us("2026-03-30T00:05:00+01:00")
    day, start, end = daily_recovery_window(at, "Europe/London")
    assert await store.schedule_daily_recovery(DEVICE, day, start, end, 2, at)
    state = await store.recovery(DEVICE)
    assert state is not None
    assert state["cursor_us"] == start
    assert state["target_us"] == end
    assert state["requested"] == 1
    await store.close()
    await store.open()
    assert await store.recovery(DEVICE) == state
    assert not await store.schedule_daily_recovery(DEVICE, day, start, end, 2, at)
    assert await store.recovery(DEVICE) == state
    async with store.read() as conn:
        assert (
            await one(
                conn,
                "SELECT last_day FROM daily_recovery_schedule WHERE device_id=?",
                (DEVICE,),
            )
        )[0] == day


async def test_live_collection_queues_the_previous_local_day(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = timestamp_us("2026-11-12T00:05:00Z")
    monkeypatch.setattr("app.poller.now_us", lambda: at)
    client = Kraken()
    client.verify = AsyncMock(return_value=None)  # type: ignore[method-assign]
    client.telemetry = AsyncMock(return_value=[])  # type: ignore[method-assign]
    try:
        await Poller(store, client, Hub()).collect(await store.config())
        state = await store.recovery(DEVICE)
        assert state is not None
        assert state["cursor_us"] == timestamp_us("2026-11-11T00:00:00Z")
        assert state["target_us"] == timestamp_us("2026-11-12T00:00:00Z")
    finally:
        await client.close()


@pytest.mark.parametrize("problem", ["revision", "meter", "disabled", "expired", "recent"])
async def test_invalid_requests_preserve_collection_and_recovery(
    store: Store, problem: str
) -> None:
    at = now_us()
    start, end, revision, device = at - DAY_US, at, 2, DEVICE
    if problem == "revision":
        revision = 1
    elif problem == "meter":
        device = "AA-BB-CC-DD-EE-FF-00-12"
    elif problem == "disabled":
        await store.save_config(ConfigUpdate(expected_revision=2, polling_enabled=False))
        revision = 3
    elif problem == "expired":
        start = at - 32 * DAY_US
    elif problem == "recent":
        start = at - 60_000_000
    with pytest.raises(Conflict if problem == "revision" else ValueError):
        await store.request_recovery(device, start, end, revision)
    assert await store.recovery(DEVICE) is None
    assert bool((await store.config())["polling_enabled"]) == (problem != "disabled")


async def test_stale_chunk_cannot_advance_a_newly_requested_earlier_range(store: Store) -> None:
    at = now_us()
    old_start = at - 2 * 3600_000_000
    old_end = old_start + 900_000_000
    await store.request_recovery(DEVICE, old_start, old_end, 2)
    await store.request_recovery(DEVICE, at - 3 * 3600_000_000, at, 2)
    queued = await store.recovery(DEVICE)
    await store.finish_recovery_chunk(DEVICE, old_start, old_end, 2, None)
    assert await store.recovery(DEVICE) == queued


async def test_recovery_rejects_history_that_has_already_been_compacted(store: Store) -> None:
    at = now_us()
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?)",
            (DEVICE, at - DAY_US),
        )
    with pytest.raises(ValueError, match="retained raw"):
        await store.request_recovery(DEVICE, at - 2 * DAY_US, at - DAY_US, 2)
    assert await store.recovery(DEVICE) is None


@pytest.mark.parametrize("unavailable", ["empty", "range_too_old", "partial"])
async def test_unavailable_readings_remain_gaps(store: Store, unavailable: str) -> None:
    start = now_us() - 3600_000_000
    end = start + 900_000_000
    await store.request_recovery(DEVICE, start, end, 2)
    client = Kraken()
    mock = AsyncMock(
        return_value=[{"readAt": iso(start), "demand": 8000}] if unavailable == "partial" else [],
        side_effect=KrakenError("range_too_old") if unavailable == "range_too_old" else None,
    )
    client.telemetry = mock  # type: ignore[method-assign]
    try:
        await Poller(store, client, Hub()).recover(await store.config())
        assert (await store.recovery(DEVICE))["status"] == "complete"
        health = await store.recovery_health(DEVICE)
        assert health["recovery_state"] == "complete"
        assert health["recovery_last_progress_at"] is not None
        assert health["recovery_gap_count"] == 1
        async with store.read() as conn:
            gap = await one(conn, "SELECT * FROM recovery_gaps")
            assert gap["reason"] == unavailable
        summary = await Analytics(store).summary(start, end, "UTC")
        assert summary["coverage_pct"] == 0
        assert summary["import_energy_kwh"] is None
    finally:
        await client.close()


@pytest.mark.parametrize(
    "state,reason",
    [("complete", None), ("complete", "partial"), ("paused", "invalid_telemetry_shape")],
)
async def test_recovery_completion_and_pause_feedback_survive_restart(
    store: Store, state: str, reason: str | None
) -> None:
    start = now_us() - 3600_000_000
    end = start + 900_000_000
    idle = await store.recovery_health(DEVICE)
    assert idle["recovery_state"] == "idle"
    assert idle["recovery_last_progress_at"] is None
    await store.request_recovery(DEVICE, start, end, 2)
    if state == "paused":
        assert reason is not None
        await store.pause_recovery(DEVICE, 2, reason)
    else:
        await store.finish_recovery_chunk(DEVICE, start, end, 2, reason)
    health = await store.recovery_health(DEVICE)
    assert health["recovery_state"] == state
    assert health["recovery_pending"] is False
    assert health["recovery_error"] == (reason if state == "paused" else None)
    assert health["recovery_gap_count"] == int(reason == "partial")
    assert bool(health["recovery_last_progress_at"]) == (state == "complete")
    await store.close()
    await store.open()
    assert await store.recovery_health(DEVICE) == health
    client = Kraken()
    poller = Poller(store, client, Hub())
    try:
        await poller.publish()
        snapshot = Snapshot.model_validate(poller.hub.current)
        assert snapshot.health.recovery_state == state
        assert snapshot.health.recovery_active is False
        assert snapshot.health.recovery_last_progress_at == health["recovery_last_progress_at"]
    finally:
        await client.close()


async def test_worker_publishes_active_recovery_and_then_completed_progress(store: Store) -> None:
    start = now_us() // 10_000_000 * 10_000_000 - 3600_000_000
    end = start + 60_000_000
    await store.request_recovery(DEVICE, start, end, 2)
    rows = readings(start, 60)
    started, finish = asyncio.Event(), asyncio.Event()

    async def telemetry(*args: object) -> list[dict[str, object]]:
        started.set()
        await finish.wait()
        return [{"readAt": row.read_at, "demand": 4000} for row in rows]

    client = Kraken()
    client.telemetry = AsyncMock(side_effect=telemetry)  # type: ignore[method-assign]
    hub = Hub()
    queue = hub.subscribe()
    queue.get_nowait()
    poller = Poller(store, client, hub)
    poller.collect = AsyncMock()  # type: ignore[method-assign]
    task = asyncio.create_task(poller.run())
    try:
        async with asyncio.timeout(3):
            _, queued = await queue.get()
            assert queued["health"]["recovery_state"] == "pending"
            assert queued["health"]["recovery_active"] is False
            poller.changed.set()
            await started.wait()
            _, active = await queue.get()
            assert active["health"]["recovery_active"] is True
            assert active["health"]["recovery_cursor_at"] == iso(start)
            finish.set()
            _, completed = await queue.get()
            assert completed["health"]["recovery_state"] == "complete"
            assert completed["health"]["recovery_active"] is False
            assert completed["health"]["recovery_cursor_at"] == iso(end)
            assert completed["health"]["recovery_last_progress_at"] is not None
            assert completed["health"]["recovery_gap_count"] == 0
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await client.close()


async def test_recovery_api_enforces_session_csrf_revision_and_cooldown(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            database=tmp_path / "recovery.sqlite3",
            auth_required=False,
            secure=False,
            start_workers=False,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        store = app.state.store
        await store.save_config(
            ConfigUpdate(
                expected_revision=1,
                account_number="A-TEST",
                active_device_id=DEVICE,
                api_key=SecretStr("fixture-key"),
                polling_enabled=True,
            )
        )
        at = now_us()
        request = {
            "expected_revision": 2,
            "device_id": DEVICE,
            "from": iso(at - DAY_US),
            "to": iso(at),
        }
        assert (await client.post("/api/recovery", json=request)).status_code == 403
        client.headers["X-CSRF-Token"] = (await client.get("/api/auth/session")).json()[
            "csrf_token"
        ]
        assert (
            await client.post("/api/recovery", json={**request, "expected_revision": 1})
        ).status_code == 409
        result = await client.post("/api/recovery", json=request)
        assert result.status_code == 202
        assert result.json()["state"] == "pending"
        assert timestamp_us(result.json()["to"]) <= now_us() - 179_000_000
        assert (await store.config())["revision"] == 2
        throttled = await client.post("/api/recovery", json=request)
        assert throttled.status_code == 429
        assert int(throttled.headers["Retry-After"]) > 0
