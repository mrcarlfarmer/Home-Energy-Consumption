import pytest
from app.analytics.intervals import integrate
from app.analytics.service import Analytics, bounds, interval_for
from app.db.store import Store
from app.models import Reading, iso, timestamp_us
from conftest import DEVICE


def test_arithmetic_and_gaps() -> None:
    m = integrate(((t * 1_000_000, 4_000_000) for t in range(0, 121, 10)), 0, 120_000_000)
    assert m.observed_us == 120_000_000
    assert m.load[0] / 3_600_000 == pytest.approx(0.133333333333)
    assert m.excess[0] / 3_600_000 == pytest.approx(0.010666666667)
    assert integrate([(0, 4_000_000), (120_000_000, 4_000_000)], 0, 120_000_000).observed_us == 0
    assert integrate([(0, 3_680_000), (10_000_000, 3_680_000)], 0, 10_000_000).above[0] == 0
    assert integrate([(0, -1000), (10_000_000, -1000)], 0, 10_000_000).energy_ws == 0
    assert integrate([(0, 1000), (10_000_000, None)], 0, 10_000_000).observed_us == 0


def test_irregular_weighting_clipping_and_no_extrapolation() -> None:
    rows = [(0, 6_000_000), (10_000_000, 1_000_000), (30_000_000, 1_000_000)]
    m = integrate(rows, 0, 60_000_000)
    assert m.observed_us == 30_000_000
    assert m.above[0] / m.observed_us == pytest.approx(1 / 3)
    clipped = integrate(rows, 5_000_000, 15_000_000)
    assert clipped.observed_us == 10_000_000
    assert clipped.energy_ws == 35_000
    assert integrate([(0, 1000), (30_000_001, 1000)], 0, 60_000_000).observed_us == 0


def readings(start: int, seconds: int, watts: int = 4000) -> list[Reading]:
    return [
        Reading.from_kraken(
            {"readAt": iso(start + s * 1_000_000), "demand": watts},
            start + (seconds + 1) * 1_000_000,
        )
        for s in range(0, seconds + 1, 10)
    ]


async def test_rollups_match_raw_and_corrections(store: Store) -> None:
    start = timestamp_us("2026-01-01T00:00:00Z")
    data = readings(start, 1800)
    assert await store.ingest(DEVICE, data, 2) == 181
    assert await store.ingest(DEVICE, data, 2) == 0
    api = Analytics(store)
    before = await api.summary(start + 1_000_000, start + 1799_000_000, "Europe/London")
    await store.rebuild(100)
    after = await api.summary(start + 1_000_000, start + 1799_000_000, "Europe/London")
    assert before == after
    assert after["coverage_pct"] == 100
    assert after["thresholds"][0]["time_above_pct"] == 100
    history = await api.history(start, start + 1800_000_000, "1m", 1500)
    assert history["series"]["rolling_15m_w"][13] is None
    assert history["series"]["rolling_15m_w"][14] == 4000
    corrected = readings(start + 300_000_000, 0, 6001)
    await store.ingest(DEVICE, corrected, 2)
    changed = await api.summary(start, start + 1800_000_000, "UTC")
    await store.rebuild(100)
    assert changed == await api.summary(start, start + 1800_000_000, "UTC")
    assert changed["sampled_peak_w"] == 6001


async def test_no_data_nulls_and_dst(store: Store) -> None:
    api = Analytics(store)
    for a, b, hours in [
        ("2026-03-29T00:00:00Z", "2026-03-29T23:00:00Z", 23),
        ("2026-10-24T23:00:00Z", "2026-10-26T00:00:00Z", 25),
    ]:
        start, end = bounds(a, b)
        result = await api.summary(start, end, "Europe/London")
        assert len(result["daily_peaks"]) == 1
        assert result["requested_seconds"] == hours * 3600
        assert result["import_energy_kwh"] is None
        assert result["thresholds"][0]["time_above_pct"] is None


async def test_late_boundary_sample_invalidates_both_buckets(store: Store) -> None:
    start = timestamp_us("2026-01-01T00:00:00Z")
    initial = [
        Reading.from_kraken(
            {"readAt": iso(start + t * 1_000_000), "demand": 4000}, start + 400_000_000
        )
        for t in (290, 310)
    ]
    await store.ingest(DEVICE, initial, 2)
    await store.rebuild(100)
    service = Analytics(store)
    before = await service.summary(start + 290_000_000, start + 310_000_000, "UTC")
    assert before["observed_seconds"] == 20
    bad = Reading.from_kraken(
        {"readAt": iso(start + 300_000_000), "demand": None}, start + 400_000_000
    )
    await store.ingest(DEVICE, [bad], 2)
    dirty = await service.summary(start + 290_000_000, start + 310_000_000, "UTC")
    await store.rebuild(100)
    rebuilt = await service.summary(start + 290_000_000, start + 310_000_000, "UTC")
    assert dirty == rebuilt
    assert rebuilt["observed_seconds"] == 0


def test_bounds_and_downsampling() -> None:
    assert timestamp_us("2026-01-01T01:00:00+01:00") == timestamp_us("2026-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        timestamp_us("2026-01-01T00:00:00.0000001Z")
    with pytest.raises(ValueError):
        bounds("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")
    assert interval_for(0, 86400_000_000, "auto", 1500) == 60_000_000
    assert interval_for(0, 30 * 86400_000_000, "auto", 1500) == 1800_000_000
    with pytest.raises(ValueError):
        interval_for(0, 30 * 86400_000_000, "1m", 1500)
