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


async def test_ev_chart_means_are_weighted_and_independent_of_meter_basis(store: Store) -> None:
    start = timestamp_us("2026-01-05T08:00:00Z")
    await store.ingest(DEVICE, readings(start, 180, 8000), 2)
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
        start + 300_000_000,
        [
            EVInterval(start, start + 15_000_000, 2000_000),
            EVInterval(start + 15_000_000, start + 60_000_000, 6000_000),
            EVInterval(start + 60_000_000, start + 120_000_000, 0),
            EVInterval(start + 120_000_000, start + 150_000_000, 7000_000),
            EVInterval(start + 150_000_000, start + 180_000_000, None),
            EVInterval(start + 180_000_000, start + 240_000_000, None),
            EVInterval(start + 240_000_000, start + 300_000_000, 3000_000),
        ],
    )
    for exclude_ev in (False, True):
        result = await Analytics(store).history(
            start, start + 300_000_000, "1m", 1500, exclude_ev=exclude_ev
        )
        series = result["series"]
        assert series["mean_ev_w"] == [5000, 0, 7000, None, 3000]
        assert series["ev_coverage_pct"] == [100, 100, 50, 0, 100]
        assert series["mean_import_w"][0] == (3000 if exclude_ev else 8000)
        assert series["coverage_pct"][4] == 0
        assert {len(values) for values in series.values()} == {5}

    await store.rebuild(100)
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?)",
            (DEVICE, start + 300_000_000),
        )
    archived = await Analytics(store).history(start, start + 300_000_000, "5m", 1500)
    assert archived["series"]["mean_ev_w"] == [None]
    assert archived["series"]["ev_coverage_pct"] == [0]


async def test_ev_chart_does_not_mix_sources_or_require_home_assistant(store: Store) -> None:
    start = timestamp_us("2026-01-05T08:00:00Z")
    result = await Analytics(store).history(start, start + 60_000_000, "1m", 1500)
    assert result["series"]["mean_ev_w"] == [None]
    assert result["series"]["ev_coverage_pct"] == [0]
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
        [EVInterval(start, start + 60_000_000, 7000_000)],
    )
    original = await Analytics(store).history(start, start + 60_000_000, "1m", 1500)
    assert original["series"]["mean_ev_w"] == [7000]
    await store.save_config(
        ConfigUpdate(expected_revision=3, home_assistant_entity="sensor.different")
    )
    changed_source = await Analytics(store).history(start, start + 60_000_000, "1m", 1500)
    assert changed_source["series"]["mean_ev_w"] == [None]
    await store.save_config(
        ConfigUpdate(
            expected_revision=4,
            active_device_id="11-22-33-44-55-66-77-88",
            home_assistant_entity="sensor.power",
        )
    )
    inactive = await Analytics(store).history(start, start + 60_000_000, "1m", 1500, DEVICE)
    assert inactive["series"]["mean_ev_w"] == [None]
    assert inactive["series"]["ev_coverage_pct"] == [0]


async def test_ev_chart_clips_retention_and_reads_beyond_one_day(store: Store) -> None:
    start = timestamp_us("2026-01-05T00:00:00Z")
    await store.save_config(
        ConfigUpdate(
            expected_revision=2,
            home_assistant_enabled=True,
            home_assistant_url="http://ha.fixture",
            home_assistant_entity="sensor.power",
            home_assistant_token=SecretStr("fixture-token"),
        )
    )
    config = await store.config()
    end = start + 25 * 3600_000_000
    for left in range(start, end, 900_000_000):
        assert (
            await store.save_ev_history(
                config,
                left,
                left + 900_000_000,
                [EVInterval(left, left + 900_000_000, 3000_000)],
            )
            == 1
        )
    complete = await Analytics(store).history(start, end, "6h", 1500)
    assert complete["series"]["mean_ev_w"] == [3000] * 5
    assert complete["series"]["ev_coverage_pct"] == [100] * 5
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?)",
            (DEVICE, start + 3 * 3600_000_000),
        )
    result = await Analytics(store).history(start, end, "6h", 1500)
    assert result["series"]["mean_ev_w"] == [3000] * 5
    assert result["series"]["ev_coverage_pct"] == [50, 100, 100, 100, 100]


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
