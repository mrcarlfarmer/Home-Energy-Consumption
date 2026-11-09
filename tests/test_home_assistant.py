import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.analytics.intervals import ev_exclusion_intervals, integrate_without_ev
from app.analytics.service import Analytics
from app.db.store import Store
from app.ev_collector import EVCollector
from app.home_assistant import HomeAssistant, HomeAssistantError, ev_source, power
from app.main import create_app
from app.models import ConfigUpdate, EVInterval, Reading, iso, now_us, timestamp_us
from app.settings import Settings
from app.storage import DAY_US
from conftest import DEVICE
from pydantic import SecretStr, ValidationError

ENTITY = "sensor.ohme_home_pro_power"


async def configure(store: Store) -> dict[str, Any]:
    saved = await store.config()
    await store.save_config(
        ConfigUpdate(
            expected_revision=saved["revision"],
            home_assistant_url="http://homeassistant.local:8123",
            home_assistant_entity=ENTITY,
            home_assistant_token=SecretStr("fixture-ha-token"),
            home_assistant_enabled=True,
        )
    )
    return await store.config()


def state(at: int, value: str = "7", unit: str = "kW") -> dict[str, Any]:
    return {
        "entity_id": ENTITY,
        "state": value,
        "last_updated": iso(at),
        "last_reported": iso(at),
        "attributes": {"unit_of_measurement": unit},
    }


@pytest.fixture
def home_config() -> dict[str, Any]:
    return {
        "home_assistant_url": "http://ha",
        "home_assistant_token": "fixture-ha-token",
        "home_assistant_entity": ENTITY,
    }


@pytest.mark.parametrize(
    "cached_value,current_value,expected",
    [("7", "0", 0), ("0", "6.5", 6500)],
)
async def test_stale_rest_uses_atomic_template_state(
    home_config: dict[str, Any], cached_value: str, current_value: str, expected: int
) -> None:
    reported = now_us() - 5_000_000
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=state(now_us() - 600_000_000, cached_value))
        assert request.method == "POST" and request.url.path == "/api/template"
        assert request.headers["Authorization"] == "Bearer fixture-ha-token"
        template = json.loads(request.content)["template"]
        assert json.dumps(ENTITY) in template
        assert "s.state" in template and "s.last_reported" in template
        return httpx.Response(200, text=json.dumps(state(reported, current_value)))

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    try:
        latest = await client.latest(home_config)
        assert latest == {"power_w": expected, "reported_at": iso(reported), "fresh": True}
        assert [request.method for request in requests] == ["GET", "POST"]
    finally:
        await client.close()


@pytest.mark.parametrize("age,fresh", [(5_000_000, True), (600_000_000, False)])
async def test_older_home_assistant_uses_last_updated(
    home_config: dict[str, Any], age: int, fresh: bool
) -> None:
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        row = state(now_us() - age, "0")
        del row["last_reported"]
        return httpx.Response(200, json=row)

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    try:
        latest = await client.latest(home_config)
        assert latest["power_w"] == 0 and latest["fresh"] is fresh
        assert calls == 1
    finally:
        await client.close()


@pytest.mark.parametrize("value,age", [("0", 600_000_000), ("unavailable", 5_000_000)])
async def test_template_does_not_make_stale_or_unavailable_values_fresh(
    home_config: dict[str, Any], value: str, age: int
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        row = (
            state(now_us() - 600_000_000)
            if request.method == "GET"
            else state(now_us() - age, value)
        )
        return httpx.Response(200, json=row)

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    try:
        latest = await client.latest(home_config)
        assert latest["fresh"] is False
        assert latest["power_w"] == (None if value == "unavailable" else 0)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "problem,code",
    [
        ("invalid_json", "home_assistant_invalid_json"),
        ("too_large", "home_assistant_response_too_large"),
        ("wrong_entity", "home_assistant_invalid_state"),
        ("invalid_state", "home_assistant_invalid_state"),
        ("invalid_unit", "home_assistant_power_unit"),
        ("invalid_power", "home_assistant_invalid_power"),
        ("invalid_timestamp", "home_assistant_invalid_timestamp"),
        ("future_timestamp", "home_assistant_invalid_timestamp"),
    ],
)
async def test_template_rejects_invalid_snapshots(
    home_config: dict[str, Any], problem: str, code: str
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=state(now_us() - 600_000_000))
        if problem == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if problem == "too_large":
            return httpx.Response(200, content=b"x" * 1_048_577)
        if problem == "invalid_state":
            return httpx.Response(200, json=[])
        row = state(now_us())
        if problem == "wrong_entity":
            row["entity_id"] = "sensor.some_other_entity"
        elif problem == "invalid_unit":
            row["attributes"]["unit_of_measurement"] = "amps"
        elif problem == "invalid_power":
            row["state"] = "-1"
        elif problem == "invalid_timestamp":
            row["last_reported"] = "not-a-timestamp"
        elif problem == "future_timestamp":
            row["last_reported"] = iso(now_us() + 120_000_000)
        return httpx.Response(200, json=row)

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    try:
        with pytest.raises(HomeAssistantError, match=code):
            await client.latest(home_config)
    finally:
        await client.close()


@pytest.mark.parametrize("status", [401, 403])
async def test_denied_template_is_not_repeated_and_fresh_rest_can_recover(
    home_config: dict[str, Any], status: int
) -> None:
    template_calls = 0
    fresh_rest, denied = False, True

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal template_calls
        if request.method == "GET":
            return httpx.Response(200, json=state(now_us() - (0 if fresh_rest else 600_000_000)))
        template_calls += 1
        return httpx.Response(status) if denied else httpx.Response(200, json=state(now_us(), "0"))

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    try:
        for _ in range(2):
            with pytest.raises(HomeAssistantError) as exc:
                await client.latest(home_config)
            assert exc.value.code == "home_assistant_freshness_access_denied"
            assert exc.value.retryable
        assert template_calls == 1

        fresh_rest = True
        assert (await client.latest(home_config))["fresh"] is True
        assert template_calls == 1

        fresh_rest, denied = False, False
        with pytest.raises(HomeAssistantError, match="freshness_access_denied"):
            await client.latest(home_config)
        changed_token = {**home_config, "home_assistant_token": "new-fixture-token"}
        assert (await client.latest(changed_token))["fresh"] is True
        assert template_calls == 2
        client.reset_freshness_access()
        assert (await client.latest(home_config))["fresh"] is True
        assert template_calls == 3
    finally:
        await client.close()


@pytest.mark.parametrize(
    "unit,value,expected",
    [
        ("W", "1234.5", 1_234_500),
        ("kW", "7.2", 7_200_000),
        ("kW", "0", 0),
    ],
)
def test_power_units(unit: str, value: str, expected: int) -> None:
    assert power(state(0, value, unit)) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "200001", "not-a-number"])
def test_invalid_power(value: str) -> None:
    with pytest.raises(HomeAssistantError):
        power(state(0, value, "W"))
    assert power({"state": "unavailable"}) is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:secret@localhost",
        "http://localhost?token=secret",
        "http://localhost#fragment",
        "http://localhost:99999",
        "http://localhost\n",
    ],
)
def test_url_validation(url: str) -> None:
    with pytest.raises(ValidationError):
        ConfigUpdate(expected_revision=1, home_assistant_url=url)


async def test_history_holds_recorded_states_without_resampling() -> None:
    start = now_us() - 180_000_000
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer fixture-ha-token"
        if "/states/" in request.url.path:
            return httpx.Response(200, json=state(now_us()))
        assert request.url.params["filter_entity_id"] == ENTITY
        assert request.url.params["significant_changes_only"] == "0"
        return httpx.Response(
            200,
            json=[
                [
                    state(start - 60_000_000),
                    state(start + 20_500_000, "0"),
                    state(start + 40_000_000, "unavailable"),
                    state(start + 50_000_000, "2000", "W"),
                ]
            ],
        )

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    config = {
        "home_assistant_url": "http://ha",
        "home_assistant_token": "fixture-ha-token",
        "home_assistant_entity": ENTITY,
    }
    try:
        latest = await client.latest(config)
        assert latest["power_w"] == 7000 and latest["fresh"]
        assert await client.history(config, start, start + 60_000_000) == [
            EVInterval(start, start + 20_500_000, 7_000_000),
            EVInterval(start + 20_500_000, start + 40_000_000, 0),
            EVInterval(start + 40_000_000, start + 50_000_000, None),
            EVInterval(start + 50_000_000, start + 60_000_000, 2_000_000),
        ]
        assert len(requests) == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "home_assistant_access_denied", False),
        (403, "home_assistant_access_denied", False),
        (404, "home_assistant_entity_or_history_missing", False),
        (429, "home_assistant_unavailable", True),
        (503, "home_assistant_unavailable", True),
        (302, "home_assistant_http_error", False),
    ],
)
async def test_http_errors_and_no_redirects(status: int, code: str, retryable: bool) -> None:
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Location": "http://other-host"})

    client = HomeAssistant(
        httpx.AsyncClient(transport=httpx.MockTransport(upstream), follow_redirects=True)
    )
    try:
        with pytest.raises(HomeAssistantError) as exc:
            await client.get(
                {"home_assistant_url": "http://ha", "home_assistant_token": "secret"}, "test"
            )
        assert exc.value.code == code and exc.value.retryable == retryable
        assert calls == 1
    finally:
        await client.close()


async def test_empty_history_is_unknown_and_responses_are_bounded() -> None:
    config = {
        "home_assistant_url": "http://ha",
        "home_assistant_token": "secret",
        "home_assistant_entity": ENTITY,
    }
    client = HomeAssistant(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])))
    )
    try:
        assert await client.history(config, 0, 60_000_000) == [EVInterval(0, 60_000_000, None)]
    finally:
        await client.close()
    client = HomeAssistant(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1_048_577))
        )
    )
    try:
        with pytest.raises(HomeAssistantError, match="response_too_large"):
            await client.get(config, "test")
    finally:
        await client.close()


def test_ev_subtraction_clipping_thresholds_and_gaps() -> None:
    rows = [(t * 1_000_000, 8_000_000) for t in range(0, 61, 10)]
    intervals = [
        EVInterval(0, 20_000_000, 7_000_000),
        EVInterval(20_000_000, 40_000_000, 0),
        EVInterval(40_000_000, 60_000_000, None),
    ]
    result = integrate_without_ev(rows, intervals, 0, 60_000_000)
    assert result.observed_us == 40_000_000
    assert result.energy_ws == 180_000
    assert result.above[0] == 20_000_000
    assert result.load[0] == 160_000
    assert result.excess[0] == 40_000
    assert result.peak_mw == 8_000_000
    assert integrate_without_ev(rows, intervals, 15_000_000, 45_000_000).energy_ws == 165_000
    exact = integrate_without_ev(rows, [EVInterval(0, 60_000_000, 2_000_000)], 0, 60_000_000)
    assert exact.observed_us == 60_000_000 and exact.above[0] == 0
    zero = integrate_without_ev(rows, [EVInterval(0, 60_000_000, 9_000_000)], 0, 60_000_000)
    assert zero.energy_ws == 0 and zero.peak_mw == 0
    gap = integrate_without_ev(
        [(0, 8_000_000), (40_000_000, 8_000_000)],
        [EVInterval(0, 60_000_000, 0)],
        0,
        60_000_000,
    )
    assert gap.observed_us == 0
    missing = integrate_without_ev(rows, [], 0, 60_000_000)
    assert missing.observed_us == 0 and missing.peak_mw is None


def test_ev_transition_guards_merge_without_changing_recorded_evidence() -> None:
    second = 1_000_000
    intervals = [
        EVInterval(a * second, b * second, power_mw)
        for a, b, power_mw in [
            (0, 300, 0),
            (300, 340, 7_000_000),
            (340, 360, 0),
            (360, 600, 7_000_000),
            (600, 900, 0),
        ]
    ]
    original = list(intervals)
    adjusted = ev_exclusion_intervals(intervals)
    assert intervals == original
    assert adjusted == [
        EVInterval(a * second, b * second, power_mw)
        for a, b, power_mw in [
            (0, 240, 0),
            (240, 300, None),
            (300, 340, None),
            (340, 360, None),
            (360, 420, None),
            (420, 540, 7_000_000),
            (540, 600, None),
            (600, 660, None),
            (660, 900, 0),
        ]
    ]
    assert ev_exclusion_intervals(adjusted) == adjusted


@pytest.mark.parametrize(
    "before,after,gap",
    [(0, 0, 0), (7_000_000, 6_800_000, 0), (0, None, 0), (None, 7_000_000, 0), (0, 7_000_000, 1)],
)
def test_ev_transition_guards_require_known_contiguous_start_stop(
    before: int | None, after: int | None, gap: int
) -> None:
    intervals = [
        EVInterval(0, 100_000_000, before),
        EVInterval(100_000_000 + gap, 200_000_000, after),
    ]
    assert ev_exclusion_intervals(intervals) == intervals


@pytest.mark.parametrize("lag", [-30, 30])
async def test_asynchronous_ev_edges_are_unknown_but_household_peaks_remain(
    store: Store, lag: int
) -> None:
    config = await configure(store)
    second = 1_000_000
    start = timestamp_us("2026-11-07T23:55:00Z")
    end = start + 2100 * second
    on, off = start + (300 + lag) * second, start + (900 + lag) * second
    intervals = [
        EVInterval(start, on, 0),
        EVInterval(on, off, 7_000_000),
        EVInterval(off, end, 0),
    ]
    rows = [
        (
            start + t * second,
            (300 + (7000 if 300 <= t < 900 else 0) + (7400 if 600 <= t < 610 else 0)) * 1000,
        )
        for t in range(0, 2101, 10)
    ]
    # Unaligned subtraction counts a false 30-second exceedance as well as the real one.
    assert integrate_without_ev(rows, intervals, start, end).above[0] == 40 * second
    await store.ingest(
        DEVICE,
        [
            Reading.from_kraken({"readAt": iso(at), "demand": power_mw / 1000}, end)
            for at, power_mw in rows
        ],
        config["revision"],
    )
    for left in range(start, end, 900 * second):
        right = min(end, left + 900 * second)
        await store.save_ev_history(
            config,
            left,
            right,
            [
                EVInterval(max(left, row.start), min(right, row.end), row.power_mw)
                for row in intervals
                if row.start < right and row.end > left
            ],
        )
    api = Analytics(store)
    adjusted = await api.summary(start, end, "UTC", exclude_ev=True)
    assert adjusted["observed_seconds"] == 1860
    assert adjusted["import_energy_kwh"] == pytest.approx(632_000 / 3_600_000)
    assert adjusted["thresholds"][0]["seconds_above"] == 10
    assert adjusted["thresholds"][0]["excess_energy_kwh"] == pytest.approx(17_000 / 3_600_000)
    assert adjusted["sampled_peak_w"] == 7700
    assert adjusted["sampled_peak_at"] == iso(start + 600 * second)
    assert any("60 seconds before and after" in warning for warning in adjusted["warnings"])
    for interval in ("1m", "5m", "15m", "1h"):
        history = await api.history(start, end, interval, 1500, exclude_ev=True)
        assert sum(history["series"]["observed_seconds"]) == adjusted["observed_seconds"]
        assert max(p for p in history["series"]["sampled_peak_w"] if p is not None) == 7700
        assert history["series"]["rolling_15m_w"][-1] == 300
        if interval == "1m":
            assert None in history["series"]["rolling_15m_w"]
            assert None in history["series"]["sampled_peak_w"]

    daily = (await api.daily_peaks(end, "UTC", exclude_ev=True))["daily_peaks"]
    assert sum(day["observed_seconds"] for day in daily) == adjusted["observed_seconds"]
    assert max(day["sampled_peak_w"] for day in daily if day["sampled_peak_w"] is not None) == 7700
    for transition in (on, off):
        for a, b in (
            (transition - 30 * second, transition - 10 * second),
            (transition + 30 * second, transition + 50 * second),
        ):
            zoom = await api.summary(a, b, "UTC", exclude_ev=True)
            chart = await api.history(a, b, "1m", 1500, exclude_ev=True)
            assert zoom["observed_seconds"] == 0
            assert zoom["import_energy_kwh"] is None
            assert zoom["sampled_peak_w"] is None
            assert all(value is None for value in chart["series"]["sampled_peak_w"])
            assert all(value is None for value in chart["series"]["mean_import_w"])
    stable = await api.summary(on + 60 * second, on + 120 * second, "UTC", exclude_ev=True)
    assert stable["coverage_pct"] == 100
    assert stable["sampled_peak_w"] == 300
    grid = await api.summary(start, end, "UTC")
    assert grid["observed_seconds"] == 2100
    assert grid["thresholds"][0]["seconds_above"] == 600
    assert grid["sampled_peak_w"] == 14700
    async with store.read() as conn:
        assert (
            sum(
                row.end - row.start
                for row in await store.ev_rows(conn, ev_source(config), start, end)
                if row.power_mw is not None
            )
            == end - start
        )


async def test_storage_overlap_corrections_fencing_and_retention(store: Store) -> None:
    config = await configure(store)
    start = now_us() - 600_000_000
    source = ev_source(config)
    original = [EVInterval(start, start + 120_000_000, 7_000_000)]
    assert await store.save_ev_history(config, start, start + 120_000_000, original) == 1
    assert await store.save_ev_history(config, start, start + 120_000_000, original) == 0
    await store.save_ev_history(
        config,
        start + 30_000_000,
        start + 60_000_000,
        [EVInterval(start + 30_000_000, start + 60_000_000, 0)],
        cursor=start + 60_000_000,
    )
    async with store.read() as conn:
        assert await store.ev_rows(conn, source, start, start + 120_000_000) == [
            EVInterval(start, start + 30_000_000, 7_000_000),
            EVInterval(start + 30_000_000, start + 60_000_000, 0),
            EVInterval(start + 60_000_000, start + 120_000_000, 7_000_000),
        ]
    assert await store.ev_cursor(source) == start + 60_000_000
    public = await store.public_config()
    assert public["has_home_assistant_token"]
    assert "fixture-ha-token" not in str(public)
    await store.save_config(
        ConfigUpdate(expected_revision=config["revision"], home_assistant_entity="sensor.other")
    )
    assert await store.save_ev_history(config, start, start + 120_000_000, original) == 0
    assert ev_source(await store.config()) != source
    await store.compact(limit=0, at=start + 31 * DAY_US)
    async with store.read() as conn:
        assert await store.ev_rows(conn, source, start, start + 120_000_000) == []


async def test_summary_history_and_grid_view_are_consistent(store: Store) -> None:
    config = await configure(store)
    start = timestamp_us("2026-01-01T00:00:00Z")
    await store.ingest(
        DEVICE,
        [
            Reading.from_kraken(
                {"readAt": iso(start + t * 1_000_000), "demand": 8000}, start + 600_000_000
            )
            for t in range(0, 601, 10)
        ],
        config["revision"],
    )
    await store.save_ev_history(
        config,
        start,
        start + 600_000_000,
        [
            EVInterval(start, start + 200_000_000, 7_000_000),
            EVInterval(start + 200_000_000, start + 400_000_000, 0),
            EVInterval(start + 400_000_000, start + 600_000_000, None),
        ],
    )
    api = Analytics(store)
    grid = await api.summary(start, start + 600_000_000, "UTC")
    adjusted = await api.summary(start, start + 600_000_000, "UTC", exclude_ev=True)
    history = await api.history(start, start + 600_000_000, "15m", 1500, exclude_ev=True)
    assert len(grid["thresholds"]) == 1
    assert grid["thresholds"][0]["threshold_w"] == 6000
    assert grid["thresholds"][0]["seconds_above"] == 600
    assert grid["coverage_pct"] == 100
    assert adjusted["coverage_pct"] == pytest.approx(100 * 280 / 600)
    assert adjusted["thresholds"][0]["seconds_above"] == 140
    assert adjusted["import_energy_kwh"] == pytest.approx(0.35)
    assert adjusted["daily_peaks"][0]["observed_seconds"] == 280
    assert history["series"]["mean_import_w"] == [4500]
    assert history["series"]["coverage_pct"] == [adjusted["coverage_pct"]]
    assert history["series"]["sampled_peak_w"] == [adjusted["sampled_peak_w"]]
    assert history["method"] == adjusted["method"]
    await store.rebuild()
    assert (await api.summary(start, start + 600_000_000, "UTC")) == grid
    with pytest.raises(ValueError, match="retained raw"):
        await api.summary(start, start + 31 * DAY_US, "UTC", exclude_ev=True)
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?)",
            (DEVICE, start + 60_000_000),
        )
    with pytest.raises(ValueError, match="retained raw"):
        await api.history(start, start + 60_000_000, "1m", 1500, exclude_ev=True)


async def test_collector_backfills_and_keeps_source_freshness(store: Store) -> None:
    config = await configure(store)
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/states/" in request.url.path:
            return httpx.Response(200, json=state(now_us()))
        return httpx.Response(200, json=[[state(now_us() - 2 * DAY_US, "0")]])

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))

    async def publish() -> None:
        pass

    collector = EVCollector(store, client, publish)
    try:
        await collector.collect(config)
        assert collector.health["state"] == "running"
        assert collector.health["power_w"] == 7000
        assert collector.health["recovery_cursor_at"]
        assert await store.ev_cursor(ev_source(config)) is not None
        assert len(requests) == 3
        task = asyncio.create_task(collector.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await client.close()
    stale = HomeAssistant(
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=state(now_us() - DAY_US))
            )
        )
    )
    try:
        with pytest.raises(HomeAssistantError, match="stale_power"):
            await EVCollector(store, stale, publish).collect(config)
    finally:
        await stale.close()


async def test_monthly_ev_history_is_bounded_and_preserves_missing_coverage(store: Store) -> None:
    config = await configure(store)
    start = timestamp_us("2026-01-01T00:00:00Z")
    end = start + 30 * DAY_US
    for left in (start, end - 60_000_000):
        await store.ingest(
            DEVICE,
            [
                Reading.from_kraken({"readAt": iso(left + t * 1_000_000), "demand": 8000}, end)
                for t in range(0, 61, 10)
            ],
            config["revision"],
        )
        await store.save_ev_history(
            config,
            left,
            left + 60_000_000,
            [EVInterval(left, left + 60_000_000, 7_000_000)],
        )
    api = Analytics(store)
    summary = await api.summary(start, end, "UTC", exclude_ev=True)
    history = await api.history(start, end, "auto", 1500, exclude_ev=True)
    assert summary["observed_seconds"] == 120
    assert summary["thresholds"][0]["seconds_above"] == 0
    assert summary["import_energy_kwh"] == pytest.approx(120 / 3600)
    assert sum(history["series"]["observed_seconds"]) == 120
    assert history["series"]["mean_import_w"][0] == 1000
    assert history["series"]["mean_import_w"][-1] == 1000
    assert any(value is None for value in history["series"]["mean_import_w"])
    assert len(history["series"]["bucket_start_epoch_s"]) <= 1500
    await store.save_config(
        ConfigUpdate(
            expected_revision=config["revision"], home_assistant_entity="sensor.other_power"
        )
    )
    missing = await api.summary(start, end, "UTC", exclude_ev=True)
    assert missing["observed_seconds"] == 0
    assert missing["import_energy_kwh"] is None
    assert missing["sampled_peak_w"] is None


async def test_worker_reports_authorization_failure_without_stopping_meter_collection(
    store: Store,
) -> None:
    await configure(store)
    published = asyncio.Event()

    async def publish() -> None:
        published.set()

    client = HomeAssistant(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401)))
    )
    collector = EVCollector(store, client, publish)
    worker = asyncio.create_task(collector.run())
    try:
        await asyncio.wait_for(published.wait(), 2)
        assert collector.health["state"] == "configuration_error"
        assert collector.health["error_code"] == "home_assistant_access_denied"
        assert collector.health["next_attempt_at"] is None
        assert (await store.config())["polling_enabled"]
        assert not worker.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_worker_recovers_after_denied_freshness_check(store: Store) -> None:
    config = await configure(store)
    published = asyncio.Event()
    fresh_rest, denied = False, True
    template_calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal template_calls
        if "/states/" in request.url.path:
            return httpx.Response(
                200, json=state(now_us() - (0 if fresh_rest else 600_000_000), "0")
            )
        if request.url.path == "/api/template":
            template_calls += 1
            return httpx.Response(401) if denied else httpx.Response(200, json=state(now_us(), "0"))
        return httpx.Response(200, json=[[state(now_us() - 2 * DAY_US, "0")]])

    async def publish() -> None:
        published.set()

    client = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    collector = EVCollector(store, client, publish)
    worker = asyncio.create_task(collector.run())
    try:
        await asyncio.wait_for(published.wait(), 2)
        assert collector.health["state"] == "backoff"
        assert collector.health["error_code"] == "home_assistant_freshness_access_denied"
        assert collector.health["power_w"] is None
        assert collector.health["next_attempt_at"] is not None
        assert template_calls == 1
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        fresh_rest = True
        await collector.collect(config)
        assert collector.health["state"] == "running"
        assert collector.health["power_w"] == 0
        assert template_calls == 1

        fresh_rest, denied = False, False
        await collector.reconfigure()
        await collector.collect(config)
        assert collector.health["state"] == "running"
        assert template_calls == 2
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_api_explains_denied_freshness_check(tmp_path: Path) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=state(now_us() - 600_000_000))
        return httpx.Response(401)

    app = create_app(
        Settings(
            database=tmp_path / "db.sqlite3",
            auth_required=False,
            secure=False,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            start_workers=False,
        ),
        home_client=HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream))),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        session = (await client.get("/api/auth/session")).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        tested = await client.post(
            "/api/home-assistant/test",
            json={
                "home_assistant_url": "http://ha",
                "home_assistant_entity": ENTITY,
                "home_assistant_token": "fixture-ha-token",
            },
        )
        assert tested.status_code == 422
        assert tested.json()["code"] == "home_assistant_freshness_access_denied"
        assert "administrator access" in tested.json()["detail"]
        assert "fixture-ha-token" not in tested.text


async def test_api_redaction_draft_test_and_credentials(tmp_path: Path) -> None:
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer fixture-ha-token"
        return httpx.Response(
            200,
            json=state(now_us())
            if "/states/" in request.url.path
            else [[state(now_us() - 300_000_000)]],
        )

    app = create_app(
        Settings(
            database=tmp_path / "db.sqlite3",
            auth_required=False,
            secure=False,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            start_workers=False,
        ),
        home_client=HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(upstream))),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        session = (await client.get("/api/auth/session")).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        draft = {
            "home_assistant_url": "http://ha",
            "home_assistant_entity": ENTITY,
            "home_assistant_token": "fixture-ha-token",
        }
        tested = await client.post("/api/home-assistant/test", json=draft)
        assert tested.status_code == 200, tested.text
        assert tested.json()["history_available"] and tested.json()["fresh"]
        assert not (await client.get("/api/config")).json()["has_home_assistant_token"]
        assert (await client.post("/api/home-assistant/test", json=draft)).status_code == 429
        assert calls == 2
        failed = await client.post(
            "/api/config",
            json={
                "expected_revision": 1,
                "home_assistant_enabled": True,
                **draft,
            },
        )
        assert failed.status_code == 422 and "fixture-ha-token" not in failed.text
        saved = await client.post(
            "/api/config",
            json={
                "expected_revision": 1,
                "account_number": "A-TEST",
                "active_device_id": DEVICE,
                "home_assistant_enabled": True,
                **draft,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["has_home_assistant_token"] and "fixture-ha-token" not in saved.text
        kept = await client.post(
            "/api/config", json={"expected_revision": 2, "display_timezone": "UTC"}
        )
        assert kept.json()["has_home_assistant_token"]
        assert (
            await client.post(
                "/api/config",
                json={
                    "expected_revision": 3,
                    "home_assistant_token": None,
                },
            )
        ).status_code == 422
        cleared = await client.post(
            "/api/config",
            json={
                "expected_revision": 3,
                "home_assistant_enabled": False,
                "home_assistant_token": None,
            },
        )
        assert cleared.status_code == 200 and not cleared.json()["has_home_assistant_token"]
