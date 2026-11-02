from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from app.analytics.service import Analytics
from app.db.store import Conflict, Store, one
from app.kraken import Kraken, KrakenError
from app.main import create_app
from app.models import ConfigUpdate, iso, now_us, timestamp_us
from app.poller import Poller
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
    await store.close()
    await store.open()
    assert await store.recovery(DEVICE) == state
    assert queued["from"] == iso(start)
    # Preserve pending work when another selected range is requested.
    await store.request_recovery(DEVICE, at - 3600_000_000, at, 2)
    merged = await store.recovery(DEVICE)
    assert merged["cursor_us"] == start
    assert at - 180_000_000 <= merged["target_us"] <= now_us() - 180_000_000


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
        async with store.read() as conn:
            gap = await one(conn, "SELECT * FROM recovery_gaps")
            assert gap["reason"] == unavailable
        summary = await Analytics(store).summary(start, end, "UTC")
        assert summary["coverage_pct"] == 0
        assert summary["import_energy_kwh"] is None
    finally:
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
