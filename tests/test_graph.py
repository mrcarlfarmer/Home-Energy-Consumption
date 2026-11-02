from pathlib import Path

import httpx
import pytest
from app.analytics.service import Analytics
from app.db.store import Store
from app.main import create_app
from app.models import ConfigUpdate, EVInterval, timestamp_us
from app.settings import Settings
from conftest import DEVICE
from pydantic import SecretStr
from test_analytics import readings


@pytest.mark.parametrize(
    "end,dates,hours",
    [
        (
            "2026-10-25T18:00:00Z",
            ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"],
            19,
        ),
        (
            "2026-10-26T00:00:00Z",
            ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"],
            25,
        ),
        (
            "2026-03-29T18:00:00Z",
            ["2026-03-25", "2026-03-26", "2026-03-27", "2026-03-28", "2026-03-29"],
            18,
        ),
    ],
)
async def test_five_calendar_peaks_respect_timezone_dst_and_half_open_end(
    store: Store, end: str, dates: list[str], hours: int
) -> None:
    result = await Analytics(store).daily_peaks(timestamp_us(end), "Europe/London")
    assert [day["date"] for day in result["daily_peaks"]] == dates
    assert result["daily_peaks"][-1]["requested_seconds"] == hours * 3600
    assert all(day["sampled_peak_w"] is None for day in result["daily_peaks"])
    assert all(day["coverage_pct"] == 0 for day in result["daily_peaks"])


async def test_daily_peak_table_includes_readings_before_a_short_chart_window(store: Store) -> None:
    for day, peak in [(1, 2000), (2, 4000), (3, 8000), (4, 6000), (5, 3000)]:
        start = timestamp_us(f"2026-01-0{day}T08:00:00Z")
        await store.ingest(DEVICE, readings(start, 60, peak), 2)
    result = await Analytics(store).daily_peaks(timestamp_us("2026-01-05T09:00:00Z"), "UTC")
    assert [day["sampled_peak_w"] for day in result["daily_peaks"]] == [
        2000,
        4000,
        8000,
        6000,
        3000,
    ]
    assert [day["observed_seconds"] for day in result["daily_peaks"]] == [60] * 5


async def test_daily_ev_peaks_respect_joint_coverage_and_expired_history(store: Store) -> None:
    start = timestamp_us("2026-01-05T08:00:00Z")
    await store.ingest(DEVICE, readings(start, 60, 8000), 2)
    await store.save_config(
        ConfigUpdate(
            expected_revision=2,
            home_assistant_enabled=True,
            home_assistant_url="http://ha.fixture",
            home_assistant_entity="sensor.power",
            home_assistant_token=SecretStr("fixture-token"),
        )
    )
    await store.save_ev_history(
        await store.config(),
        start,
        start + 60_000_000,
        [
            EVInterval(start, start + 30_000_000, 7000_000),
            EVInterval(start + 30_000_000, start + 60_000_000, None),
        ],
    )
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?)",
            (DEVICE, timestamp_us("2026-01-04T00:00:00Z")),
        )
    result = await Analytics(store).daily_peaks(
        timestamp_us("2026-01-05T09:00:00Z"), "UTC", exclude_ev=True
    )
    assert len(result["daily_peaks"]) == 5
    assert result["daily_peaks"][-1]["sampled_peak_w"] == 1000
    assert result["daily_peaks"][-1]["observed_seconds"] == 30
    assert all(day["sampled_peak_w"] is None for day in result["daily_peaks"][:-1])
    assert any("Expired EV history" in warning for warning in result["warnings"])


async def test_daily_peaks_endpoint_requires_session_and_rejects_bad_inputs(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("fixture-password")
    app = create_app(
        Settings(
            database=tmp_path / "graph.sqlite3",
            password_file=password,
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
        params = {"to": "2026-11-02T18:00:00Z", "timezone": "Europe/London"}
        assert (await client.get("/api/analytics/daily-peaks", params=params)).status_code == 401
        assert (
            await client.post(
                "/api/auth/login", json={"username": "admin", "password": "fixture-password"}
            )
        ).status_code == 200
        await app.state.store.save_config(
            ConfigUpdate(
                expected_revision=1,
                account_number="A-TEST",
                active_device_id=DEVICE,
            )
        )
        result = await client.get("/api/analytics/daily-peaks", params=params)
        assert result.status_code == 200
        assert len(result.json()["daily_peaks"]) == 5
        for field, value in [
            ("to", "not-a-time"),
            ("to", "1969-12-31T23:59:59Z"),
            ("timezone", "Invalid/Zone"),
        ]:
            assert (
                await client.get("/api/analytics/daily-peaks", params={**params, field: value})
            ).status_code == 422
