from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.analytics.day import calendar_bounds, daily_analysis
from app.analytics.service import Analytics
from app.api_models import DayAnalysis
from app.db.store import Store
from app.kraken import Kraken
from app.main import create_app
from app.models import ConfigUpdate, EVInterval, iso, timestamp_us
from app.settings import Settings
from app.storage import DAY_US
from app.tariffs import TariffPricing
from conftest import DEVICE
from test_analytics import readings
from test_ev_bonus import cache, configure, history
from test_retention_recovery import drain

AT = timestamp_us("2026-11-09T12:00:00Z")
START = timestamp_us("2026-11-07T00:00:00Z")
SECOND = 1_000_000


@pytest.fixture
async def services(store: Store) -> AsyncIterator[tuple[Analytics, TariffPricing]]:
    async def publish() -> None:
        pass

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))))
    try:
        yield Analytics(store), TariffPricing(store, client, publish)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "day,zone,start,end,hours",
    [
        ("2026-03-29", "Europe/London", "2026-03-29T00:00:00Z", "2026-03-29T23:00:00Z", 23),
        ("2026-10-25", "Europe/London", "2026-10-24T23:00:00Z", "2026-10-26T00:00:00Z", 25),
        ("2026-11-07", "Asia/Kathmandu", "2026-11-06T18:15:00Z", "2026-11-07T18:15:00Z", 24),
    ],
)
def test_calendar_bounds_use_calendar_timezone_not_elapsed_day(
    day: str, zone: str, start: str, end: str, hours: int
) -> None:
    a, b, calendar_end = calendar_bounds(day, zone, AT)
    assert (iso(a), iso(b)) == (iso(timestamp_us(start)), iso(timestamp_us(end)))
    assert calendar_end == b
    assert b - a == hours * 3600 * SECOND


@pytest.mark.parametrize(
    "day", ["20261107", "not-a-date", "2026-02-30", "9999-12-31", "1969-12-31", "2026-11-10"]
)
def test_calendar_bounds_reject_invalid_or_future_days(day: str) -> None:
    with pytest.raises(ValueError):
        calendar_bounds(day, "UTC", AT)


async def test_real_day_uses_full_calendar_evidence_and_selected_basis(
    store: Store, services: tuple[Analytics, TariffPricing]
) -> None:
    analytics, pricing = services
    config = await configure(store)
    await store.ingest(DEVICE, readings(START, 600, 8000), config["revision"])
    await history(
        store,
        config,
        [
            EVInterval(START, START + 200 * SECOND, 0),
            EVInterval(START + 200 * SECOND, START + 400 * SECOND, 7_000_000),
            EVInterval(START + 400 * SECOND, START + 600 * SECOND, 0),
        ],
    )
    await cache(store, config, START, START + 1800 * SECOND)
    result = await daily_analysis(
        analytics, pricing, "2026-11-07", "Europe/London", DEVICE, True, at=AT
    )
    DayAnalysis.model_validate(result)
    assert result["from"] == iso(START)
    assert result["to"] == iso(START + DAY_US)
    assert result["grid"]["observed_seconds"] == 600
    assert result["adjusted"]["observed_seconds"] == 360
    assert result["coverage"] == {
        "meter_seconds": 600,
        "ev_seconds": 600,
        "joint_seconds": 600,
        "adjusted_seconds": 360,
    }
    assert (
        sum(
            (timestamp_us(row["to"]) - timestamp_us(row["from"])) / SECOND
            for row in result["transition_gaps"]
        )
        == 240
    )
    assert result["exclude_ev"] is True
    assert result["history"]["method"] == result["adjusted"]["method"]
    assert result["grid_history"]["method"] == result["grid"]["method"]
    assert sum(result["history"]["series"]["observed_seconds"]) == 360
    assert result["charging"] == [
        {"from": iso(START + 200 * SECOND), "to": iso(START + 400 * SECOND)}
    ]
    assert result["tariff"]["rates"][0]["band"] == "EV_BONUS"
    assert (
        result["grid"]["pricing"]["pricing_revision"]
        == result["adjusted"]["pricing"]["pricing_revision"]
        == result["tariff"]["pricing_revision"]
    )
    assert result["grid"]["pricing"]["excess_energy_gbp"] == pytest.approx(2 * 600 / 3600 * 0.08)
    assert result["adjusted"]["pricing"]["excess_energy_gbp"] == pytest.approx(
        2 * 280 / 3600 * 0.08
    )
    assert result["peak_overlaps_ev"] is False
    assert result["raw_available"] and result["recovery_available"]
    assert not result["partial"]
    assert (
        result["grid"]["data_version"]
        == result["data_version"]
        == result["history"]["data_version"]
    )
    grid = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, False, at=AT)
    episodes = [row for row in grid["events"] if row["kind"] == "exceedance"]
    assert episodes == [
        {"kind": "exceedance", "from": iso(START), "to": iso(START + 600 * SECOND), "peak_w": 8000}
    ]
    assert grid["history"]["method"] == grid["grid"]["method"]
    assert grid["grid_history"] is None


async def test_ev_overlap_requires_known_evidence_not_just_missing_positive_power(
    store: Store, services: tuple[Analytics, TariffPricing]
) -> None:
    analytics, pricing = services
    config = await configure(store)
    await store.ingest(DEVICE, readings(START, 60, 8000), config["revision"])
    missing = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, False, at=AT)
    assert missing["peak_overlaps_ev"] is None
    assert missing["adjusted"]["import_energy_kwh"] is None
    await history(store, config, [EVInterval(START, START + 60 * SECOND, 7_000_000)])
    known = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, False, at=AT)
    assert known["peak_overlaps_ev"] is True
    assert known["adjusted"]["sampled_peak_w"] == 1000
    assert known["adjusted"]["thresholds"][0]["seconds_above"] == 0


async def test_day_clips_today_and_keeps_no_data_unavailable(
    services: tuple[Analytics, TariffPricing],
) -> None:
    analytics, pricing = services
    result = await daily_analysis(analytics, pricing, "2026-11-09", "UTC", DEVICE, True, at=AT)
    assert result["partial"] is True
    assert result["to"] == iso(AT)
    assert result["calendar_to"] == "2026-11-10T00:00:00.000000Z"
    assert result["grid"]["requested_seconds"] == 12 * 3600
    assert result["grid"]["coverage_pct"] == 0
    assert result["grid"]["sampled_peak_w"] is None
    assert result["grid"]["import_energy_kwh"] is None
    assert result["grid"]["pricing"]["excess_energy_gbp"] is None
    assert result["adjusted"] is None and not result["exclude_ev"]
    assert result["ev_unavailable_reason"]


async def test_exceedance_episodes_do_not_bridge_missing_meter_evidence(
    store: Store, services: tuple[Analytics, TariffPricing]
) -> None:
    analytics, pricing = services
    await store.ingest(
        DEVICE, readings(START, 60, 8000) + readings(START + 120 * SECOND, 60, 8000), 2
    )
    result = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, False, at=AT)
    episodes = [row for row in result["events"] if row["kind"] == "exceedance"]
    gaps = [row for row in result["events"] if row["kind"] == "meter_gap"]
    assert len(episodes) == 2
    assert gaps[0]["from"] == iso(START + 60 * SECOND)
    assert gaps[0]["to"] == iso(START + 120 * SECOND)
    assert result["grid"]["thresholds"][0]["seconds_above"] == 120


async def test_archived_day_keeps_grid_metrics_and_disables_ev(
    store: Store, services: tuple[Analytics, TariffPricing]
) -> None:
    analytics, pricing = services
    config = await configure(store)
    await store.ingest(DEVICE, readings(START, 600, 8000), config["revision"])
    await store.rebuild(100)
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?)",
            (DEVICE, START + DAY_US),
        )
    result = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, True, at=AT)
    assert result["grid"]["observed_seconds"] == 600
    assert result["history"]["interval_seconds"] == 300
    assert result["exclude_ev"] is False and result["adjusted"] is None
    assert result["events"] == []
    assert result["coverage"]["ev_seconds"] is None
    assert not result["raw_available"] and not result["recovery_available"]
    assert "expired" in result["ev_unavailable_reason"]


async def test_daily_archive_adopts_stored_calendar_timezone_and_dst(
    store: Store, services: tuple[Analytics, TariffPricing]
) -> None:
    analytics, pricing = services
    start = timestamp_us("2026-10-24T23:00:00Z")
    end = timestamp_us("2026-10-26T00:00:00Z")
    await store.ingest(DEVICE, readings(start, 600, 8000), 2)
    await drain(store, end + 397 * DAY_US)
    result = await daily_analysis(
        analytics, pricing, "2026-10-25", "UTC", DEVICE, True, at=end + 398 * DAY_US
    )
    assert result["timezone"] == "Europe/London"
    assert result["from"] == iso(start)
    assert result["grid"]["requested_seconds"] == 25 * 3600
    assert result["grid"]["observed_seconds"] == 600
    assert result["history"]["bucket_alignment"] == "Europe/London"
    assert len(result["history"]["series"]["mean_import_w"]) == 1
    assert not result["raw_available"]
    assert any("archives use" in warning for warning in result["warnings"])


async def test_day_read_snapshot_does_not_mix_a_concurrent_correction(
    store: Store, services: tuple[Analytics, TariffPricing], monkeypatch: pytest.MonkeyPatch
) -> None:
    analytics, pricing = services
    config = await configure(store)
    await store.ingest(DEVICE, readings(START, 60, 8000), config["revision"])
    await history(store, config, [EVInterval(START, START + 60 * SECOND, 7_000_000)])
    original = analytics.summary
    corrected = False

    async def correcting_summary(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal corrected
        result = await original(*args, **kwargs)
        if not corrected:
            corrected = True
            await store.ingest(DEVICE, readings(START, 60, 9000), config["revision"])
        return result

    monkeypatch.setattr(analytics, "summary", correcting_summary)
    first = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, True, at=AT)
    assert first["grid"]["sampled_peak_w"] == 8000
    assert first["adjusted"]["sampled_peak_w"] == 1000
    assert (
        first["grid"]["data_version"]
        == first["adjusted"]["data_version"]
        == first["history"]["data_version"]
    )
    second = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, True, at=AT)
    assert second["grid"]["sampled_peak_w"] == 9000
    assert second["adjusted"]["sampled_peak_w"] == 2000
    assert second["data_version"] > first["data_version"]


async def test_day_event_log_is_bounded_and_reports_truncation(
    store: Store, services: tuple[Analytics, TariffPricing]
) -> None:
    analytics, pricing = services
    await store.ingest(
        DEVICE,
        [
            reading
            for offset in range(0, 7200, 60)
            for reading in readings(START + offset * SECOND, 10, 8000)
        ],
        2,
    )
    result = await daily_analysis(analytics, pricing, "2026-11-07", "UTC", DEVICE, False, at=AT)
    assert len(result["events"]) == 200
    assert result["event_count"] == 240
    assert len(result["meter_gaps"]) == 120
    assert result["events"] == sorted(result["events"], key=lambda row: (row["from"], row["kind"]))


async def test_day_endpoint_is_authenticated_validated_and_typed(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("fixture-password")
    app = create_app(
        Settings(
            database=tmp_path / "daily.sqlite3",
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
        params: dict[str, Any] = {"date": "2026-11-07", "timezone": "UTC", "exclude_ev": True}
        assert (await client.get("/api/analytics/day", params=params)).status_code == 401
        await client.post(
            "/api/auth/login", json={"username": "admin", "password": "fixture-password"}
        )
        await app.state.store.save_config(
            ConfigUpdate(expected_revision=1, active_device_id=DEVICE, account_number="A-TEST")
        )
        response = await client.get("/api/analytics/day", params=params)
        assert response.status_code == 200
        DayAnalysis.model_validate(response.json())
        assert response.json()["adjusted"] is None
        for field, value in [
            ("date", "no-date"),
            ("date", "9999-12-31"),
            ("date", "2026-02-30"),
            ("timezone", "Invalid/Zone"),
        ]:
            assert (
                await client.get("/api/analytics/day", params={**params, field: value})
            ).status_code == 422
        assert "fixture-password" not in response.text
