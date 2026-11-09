import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.analytics.service import Analytics
from app.db.store import Conflict, Store
from app.kraken import Kraken, KrakenError
from app.main import create_app
from app.models import ConfigUpdate, EVInterval, Reading, TariffRate, iso, timestamp_us
from app.settings import Settings
from app.storage import DAY_US
from app.tariffs import AGREEMENTS, OctopusTariffs, TariffPricing
from conftest import DEVICE
from pydantic import SecretStr

START = timestamp_us("2026-10-25T00:00:00Z")
PRODUCT = "TEST-26-01-01"
CODE = f"E-1R-{PRODUCT}-A"


def schedule() -> dict[str, Any]:
    return {
        "name": "IOG_ECO7_TOU",
        "timezone": "Europe/London",
        "timeslots": [
            {
                "timeslot": band,
                "activeFrom": a,
                "activeTo": b,
                **dict.fromkeys(
                    ("weekdays", "weekends", "saturdays", "sundays", "workdays", "holidays"),
                    False,
                ),
                "season": {"startDay": 1, "startMonth": 1, "endDay": 1, "endMonth": 1},
            }
            for band, a, b in (
                ("ECO7_DAY", "05:30:00", "23:30:00"),
                ("ECO7_NIGHT", "23:30:00", "05:30:00"),
            )
        ],
    }


def agreement(tou: bool = False) -> dict[str, Any]:
    return {
        "validFrom": "2020-01-01T00:00:00Z",
        "validTo": None,
        "isRevoked": False,
        "tariff": {
            "productCode": PRODUCT,
            "tariffCode": CODE,
            "displayName": "Fixture tariff",
            "isExport": False,
        },
        "timeOfUseScheme": schedule() if tou else None,
    }


def account(agreements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "account": {
            "electricityAgreements": [
                {
                    "meterPoint": {
                        "id": "fixture-point",
                        "direction": "IMPORT",
                        "meters": [{"smartDevices": [{"deviceId": DEVICE}]}],
                        "agreements": agreements,
                    }
                }
            ]
        }
    }


def public_rate(
    value: object = 30, start: int = START - 100 * DAY_US, end: int | None = None
) -> dict[str, Any]:
    return {
        "value_inc_vat": value,
        "value_exc_vat": 999,
        "valid_from": iso(start),
        "valid_to": iso(end) if end is not None else None,
        "payment_method": None,
    }


class FixtureKraken(Kraken):
    def __init__(self, data: dict[str, Any], handler: Any) -> None:
        super().__init__(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        self.data = data
        self.discoveries = 0

    async def authenticated(
        self, key: str, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        assert query == AGREEMENTS
        assert variables["accountNumber"] == "A-TEST"
        self.discoveries += 1
        return self.data


async def enable(store: Store) -> dict[str, Any]:
    config = await store.config()
    await store.save_config(
        ConfigUpdate(expected_revision=config["revision"], tariff_pricing_enabled=True)
    )
    return await store.config()


def cache_details(start: int, end: int) -> dict[str, Any]:
    return {
        "agreements": [
            {
                "from": iso(start),
                "to": iso(end),
                "name": "Fixture tariff",
                "tariff_code": CODE,
                "schedule": "Fixture",
            }
        ],
        "warnings": [],
    }


async def seed(
    store: Store, start: int = START, seconds: int = 60, demand: int = 8000
) -> dict[str, Any]:
    config = await enable(store)
    await store.ingest(
        DEVICE,
        [
            Reading.from_kraken(
                {"readAt": iso(start + t * 1_000_000), "demand": demand},
                start + seconds * 1_000_000,
            )
            for t in range(0, seconds + 1, 10)
        ],
        config["revision"],
    )
    return config


async def test_public_rates_follow_safe_pagination_and_include_vat() -> None:
    calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "api.octopus.energy"
        assert "Authorization" not in request.headers
        assert request.url.params["page_size"] == "1500"
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "next": "https://untrusted.invalid/never-follow" if page == 1 else None,
                "results": [
                    public_rate(
                        -5 if page == 1 else 30,
                        START + (page - 1) * 30_000_000,
                        START + page * 30_000_000,
                    )
                ],
            },
        )

    client = FixtureKraken(account([agreement()]), upstream)
    try:
        rows = await OctopusTariffs(client).rates(
            PRODUCT, CODE, "STANDARD", START, START + 60_000_000
        )
        assert [row.pence_per_kwh for row in rows] == [Decimal(-5), Decimal(30)]
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "left,right,change_at,night_hours",
    [
        ("2026-10-24T22:30:00Z", "2026-10-25T06:30:00Z", "2026-10-25T02:15:00Z", 7),
        ("2026-03-28T23:30:00Z", "2026-03-29T05:30:00Z", "2026-03-29T02:15:00Z", 5),
    ],
)
async def test_home_day_night_schedule_uses_real_dst_and_rate_changes(
    left: str, right: str, change_at: str, night_hours: int
) -> None:
    start, end, change = map(timestamp_us, (left, right, change_at))

    def upstream(request: httpx.Request) -> httpx.Response:
        night = "/night-unit-rates/" in request.url.path
        return httpx.Response(
            200,
            json={
                "next": None,
                "results": [
                    public_rate(10 if night else 30, start, change),
                    public_rate(12 if night else 40, change, end),
                ],
            },
        )

    client = FixtureKraken(account([agreement(True)]), upstream)
    try:
        rows, details = await OctopusTariffs(client).fetch(
            {"octopus_api_key": "test-key", "account_number": "A-TEST", "active_device_id": DEVICE},
            start,
            end,
        )
        assert (
            sum(row.end - row.start for row in rows if row.band == "ECO7_NIGHT")
            == night_hours * 3600_000_000
        )
        assert rows[0].start == start and rows[-1].end == end
        assert any(row.start == change and row.pence_per_kwh == 12 for row in rows)
        assert all(row.band not in ("EV_DEVICE_PEAK", "EV_DEVICE_OFF_PEAK") for row in rows)
        assert details["off_peak_rates"] == [
            {"from": iso(start), "to": iso(change), "pence_per_kwh": "10", "tariff_code": CODE},
            {"from": iso(change), "to": iso(end), "pence_per_kwh": "12", "tariff_code": CODE},
        ]
        assert details["agreements"][0]["smart_ev"] is False
    finally:
        await client.close()


@pytest.mark.parametrize("problem", ["season", "weekday", "overlap", "minute", "timezone"])
async def test_unsupported_schedules_are_not_guessed(problem: str) -> None:
    data = schedule()
    if problem == "season":
        data["timeslots"][0]["season"]["endMonth"] = 6
    elif problem == "weekday":
        data["timeslots"][0]["weekdays"] = True
    elif problem == "overlap":
        data["timeslots"][0]["activeTo"] = "23:00:00"
    elif problem == "minute":
        data["timeslots"][0]["activeFrom"] = "05:31:00"
    elif problem == "timezone":
        data["timezone"] = "Invalid/Zone"
    client = Kraken()
    try:
        with pytest.raises(KrakenError, match="schedule_unsupported"):
            OctopusTariffs(client).schedule(data)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "problem,code",
    [
        ("invalid_json", "tariff_invalid_json"),
        ("large", "tariff_response_too_large"),
        ("redirect", "tariff_prices_http_error"),
        ("rate_limit", "tariff_prices_unavailable"),
        ("nan", "tariff_invalid_price"),
        ("bool", "tariff_invalid_price"),
        ("timestamp", "tariff_invalid_timestamp"),
        ("payment", "tariff_payment_method_unsupported"),
        ("overlap", "tariff_overlapping_rates"),
        ("pagination", "tariff_pagination_limit"),
    ],
)
async def test_invalid_rate_feeds_fail_explicitly(problem: str, code: str) -> None:
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if problem == "invalid_json":
            return httpx.Response(200, text="not JSON")
        if problem == "large":
            return httpx.Response(200, content=b"x" * 1_048_577)
        if problem == "redirect":
            return httpx.Response(302, headers={"Location": "https://untrusted.invalid/"})
        if problem == "rate_limit":
            return httpx.Response(429, headers={"Retry-After": "120"})
        row = public_rate()
        if problem == "nan":
            row["value_inc_vat"] = "NaN"
        elif problem == "bool":
            row["value_inc_vat"] = True
        elif problem == "timestamp":
            row["valid_from"] = "not-a-time"
        elif problem == "payment":
            row["payment_method"] = "DIRECT_DEBIT"
        rows = [row, public_rate(31)] if problem == "overlap" else [row]
        if problem == "pagination":
            rows = []
        return httpx.Response(
            200, json={"results": rows, "next": "more" if problem == "pagination" else None}
        )

    client = FixtureKraken(account([agreement()]), upstream)
    try:
        with pytest.raises(KrakenError, match=code):
            await OctopusTariffs(client).rates(PRODUCT, CODE, "STANDARD", START, START + DAY_US)
        assert calls == (4 if problem == "pagination" else 1)
    finally:
        await client.close()


async def test_agreements_match_selected_import_meter_and_reject_ambiguity() -> None:
    data = account([agreement()])
    other = deepcopy(data["account"]["electricityAgreements"][0])
    other["meterPoint"]["id"] = "different-point"
    data["account"]["electricityAgreements"].append(other)
    client = FixtureKraken(
        data, lambda _: httpx.Response(200, json={"results": [public_rate()], "next": None})
    )
    config = {"octopus_api_key": "test-key", "account_number": "A-TEST", "active_device_id": DEVICE}
    try:
        with pytest.raises(KrakenError, match="ambiguous"):
            await OctopusTariffs(client).fetch(config, START, START + DAY_US)
        other["meterPoint"]["direction"] = "EXPORT"
        rows, _ = await OctopusTariffs(client).fetch(config, START, START + DAY_US)
        assert len(rows) == 1
    finally:
        await client.close()


async def test_historical_agreements_do_not_use_current_prices_backwards() -> None:
    old, new = agreement(), agreement()
    old["validTo"] = iso(START + 30_000_000)
    new["validFrom"] = old["validTo"]
    new["tariff"].update(productCode="NEW-26-01-01", tariffCode="E-1R-NEW-26-01-01-A")

    def upstream(request: httpx.Request) -> httpx.Response:
        value = 40 if "NEW-26-01-01" in request.url.path else 10
        return httpx.Response(200, json={"results": [public_rate(value)], "next": None})

    future = agreement()
    future["validFrom"] = iso(START + DAY_US)
    client = FixtureKraken(account([new, old, future]), upstream)
    try:
        rows, _ = await OctopusTariffs(client).fetch(
            {"octopus_api_key": "test-key", "account_number": "A-TEST", "active_device_id": DEVICE},
            START,
            START + 60_000_000,
        )
        assert [(row.start, row.end, row.pence_per_kwh) for row in rows] == [
            (START, START + 30_000_000, Decimal(10)),
            (START + 30_000_000, START + 60_000_000, Decimal(40)),
        ]
    finally:
        await client.close()


async def test_both_costs_split_rate_boundaries_without_extrapolation(store: Store) -> None:
    config = await seed(store)
    rates = [
        TariffRate(START, START + 20_000_000, Decimal(30), CODE, "STANDARD"),
        TariffRate(START + 20_000_000, START + 60_000_000, Decimal(10), CODE, "STANDARD"),
    ]
    await store.save_tariff_rates(
        config, START, START + 60_000_000, rates, cache_details(START, START + 60_000_000)
    )
    result = await Analytics(store).summary(START, START + 60_000_000, "UTC")
    assert result["pricing"]["energy_when_above_gbp"] == pytest.approx(0.022222222222)
    assert result["pricing"]["excess_energy_gbp"] == pytest.approx(0.005555555555)
    assert result["pricing"]["price_coverage_pct"] == 100
    assert result["thresholds"][0]["seconds_above"] == 60
    await store.save_tariff_rates(
        config, START, START + 60_000_000, rates[:1], cache_details(START, START + 60_000_000)
    )
    partial = (await Analytics(store).summary(START, START + 60_000_000, "UTC"))["pricing"]
    assert not partial["available"]
    assert partial["energy_when_above_gbp"] is None and partial["excess_energy_gbp"] is None
    assert partial["price_coverage_pct"] == pytest.approx(100 / 3)


async def test_daily_costs_split_local_midnight_and_clip_the_partial_day(store: Store) -> None:
    start = timestamp_us("2026-10-24T22:59:30Z")
    midnight = start + 30_000_000
    end = start + 50_000_000
    config = await seed(store, start=start)
    await store.save_tariff_rates(
        config,
        start,
        start + 60_000_000,
        [
            TariffRate(start, midnight, Decimal(10), CODE, "STANDARD"),
            TariffRate(midnight, start + 60_000_000, Decimal(30), CODE, "STANDARD"),
        ],
        cache_details(start, start + 60_000_000),
    )
    result = await Analytics(store).daily_peaks(end, "Europe/London")
    previous, latest = result["daily_peaks"][-2:]
    assert previous["date"] == "2026-10-24"
    assert latest["date"] == "2026-10-25"
    assert previous["observed_seconds"] == 30
    assert latest["requested_seconds"] == latest["observed_seconds"] == 20
    assert latest["to"] == iso(end)
    assert previous["pricing"]["energy_when_above_gbp"] == pytest.approx(0.006666666667)
    assert previous["pricing"]["excess_energy_gbp"] == pytest.approx(0.001666666667)
    assert latest["pricing"]["energy_when_above_gbp"] == pytest.approx(0.013333333333)
    assert latest["pricing"]["excess_energy_gbp"] == pytest.approx(0.003333333333)
    assert previous["pricing"]["price_coverage_pct"] == 100
    assert latest["pricing"]["price_coverage_pct"] == 100
    summary = await Analytics(store).summary(start, end, "Europe/London")
    for field in ("energy_when_above_gbp", "excess_energy_gbp"):
        assert previous["pricing"][field] + latest["pricing"][field] == pytest.approx(
            summary["pricing"][field]
        )


@pytest.mark.parametrize(
    "end,hours,first,second",
    [
        (
            "2026-10-26T00:00:00Z",
            25,
            "2026-10-25T00:30:00Z",
            "2026-10-25T01:30:00Z",
        ),
        (
            "2026-03-29T23:00:00Z",
            23,
            "2026-03-29T00:30:00Z",
            "2026-03-29T01:30:00Z",
        ),
    ],
)
async def test_daily_costs_preserve_dst_duration_and_dated_rates(
    store: Store, end: str, hours: int, first: str, second: str
) -> None:
    start, change = timestamp_us(first), timestamp_us(second)
    await seed(store, start=start)
    config = await seed(store, start=change)
    await store.save_tariff_rates(
        config,
        start,
        change + 60_000_000,
        [
            TariffRate(start, start + 60_000_000, Decimal(10), CODE, "STANDARD"),
            TariffRate(change, change + 60_000_000, Decimal(30), CODE, "STANDARD"),
        ],
        cache_details(start, change + 60_000_000),
    )
    latest = (await Analytics(store).daily_peaks(timestamp_us(end), "Europe/London"))[
        "daily_peaks"
    ][-1]
    assert latest["requested_seconds"] == hours * 3600
    assert latest["observed_seconds"] == 120
    assert latest["pricing"]["priced_seconds_above"] == 120
    assert latest["pricing"]["price_coverage_pct"] == 100
    assert latest["pricing"]["energy_when_above_gbp"] == pytest.approx(0.053333333333)
    assert latest["pricing"]["excess_energy_gbp"] == pytest.approx(0.013333333333)


async def test_daily_costs_keep_missing_prices_separate_from_zero_and_other_days(
    store: Store,
) -> None:
    await seed(store, start=START - 3 * DAY_US)
    await seed(store, start=START - 2 * DAY_US)
    await seed(store, start=START - DAY_US, demand=6000)
    config = await seed(store)
    await store.save_tariff_rates(
        config,
        START - 4 * DAY_US,
        START + 60_000_000,
        [
            TariffRate(
                START - 2 * DAY_US,
                START - 2 * DAY_US + 30_000_000,
                Decimal(30),
                CODE,
                "STANDARD",
            ),
            TariffRate(START, START + 60_000_000, Decimal(30), CODE, "STANDARD"),
        ],
        cache_details(START - 4 * DAY_US, START + 60_000_000),
    )
    days = (await Analytics(store).daily_peaks(START + 60_000_000, "UTC"))["daily_peaks"]
    assert [day["date"] for day in days] == [
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
        "2026-10-24",
        "2026-10-25",
    ]
    for day in days[:3]:
        assert day["pricing"]["available"] is False
        assert day["pricing"]["energy_when_above_gbp"] is None
        assert day["pricing"]["excess_energy_gbp"] is None
    assert days[0]["pricing"]["price_coverage_pct"] is None
    assert "No observed energy" in days[0]["pricing"]["warning"]
    assert days[1]["pricing"]["price_coverage_pct"] == 0
    assert days[2]["pricing"]["price_coverage_pct"] == 50
    assert days[3]["pricing"]["available"] is True
    assert days[3]["pricing"]["energy_when_above_gbp"] == 0
    assert days[3]["pricing"]["excess_energy_gbp"] == 0
    assert days[3]["pricing"]["price_coverage_pct"] == 100
    assert days[4]["pricing"]["available"] is True
    assert days[4]["pricing"]["energy_when_above_gbp"] == pytest.approx(0.04)
    assert days[4]["pricing"]["excess_energy_gbp"] == pytest.approx(0.01)


async def test_archived_costs_require_rates_at_the_retained_resolution(store: Store) -> None:
    config = await seed(store, seconds=300)
    await store.rebuild()
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET raw_before_us=excluded.raw_before_us",
            (DEVICE, START + 300_000_000),
        )
        await conn.execute("DELETE FROM readings WHERE device_id=?", (DEVICE,))
    rates = [
        TariffRate(START, START + 150_000_000, Decimal(10), CODE, "STANDARD"),
        TariffRate(START + 150_000_000, START + 300_000_000, Decimal(30), CODE, "STANDARD"),
    ]
    await store.save_tariff_rates(
        config, START, START + 300_000_000, rates, cache_details(START, START + 300_000_000)
    )
    result = (await Analytics(store).summary(START, START + 300_000_000, "UTC"))["pricing"]
    assert not result["available"] and result["energy_when_above_gbp"] is None
    daily = (await Analytics(store).daily_peaks(START + 300_000_000, "UTC"))["daily_peaks"]
    assert daily[-1]["pricing"] == result
    await store.save_tariff_rates(
        config,
        START,
        START + 300_000_000,
        [TariffRate(START, START + 300_000_000, Decimal(30), CODE, "STANDARD")],
        cache_details(START, START + 300_000_000),
    )
    result = (await Analytics(store).summary(START, START + 300_000_000, "UTC"))["pricing"]
    assert result["available"]
    assert result["energy_when_above_gbp"] == pytest.approx(0.2)
    assert result["excess_energy_gbp"] == pytest.approx(0.05)
    daily = (await Analytics(store).daily_peaks(START + 300_000_000, "UTC"))["daily_peaks"]
    assert daily[-1]["pricing"] == result


@pytest.mark.parametrize("demand,price,expected", [(6000, 30, 0), (8000, -30, -0.04)])
async def test_exact_threshold_zero_and_negative_prices(
    store: Store, demand: int, price: int, expected: float
) -> None:
    config = await seed(store, demand=demand)
    await store.save_tariff_rates(
        config,
        START,
        START + 60_000_000,
        [TariffRate(START, START + 60_000_000, Decimal(price), CODE, "STANDARD")],
        cache_details(START, START + 60_000_000),
    )
    result = (await Analytics(store).summary(START, START + 60_000_000, "UTC"))["pricing"]
    assert result["energy_when_above_gbp"] == pytest.approx(expected)
    assert result["excess_energy_gbp"] == pytest.approx(expected / 4)
    daily = (await Analytics(store).daily_peaks(START + 60_000_000, "UTC"))["daily_peaks"]
    assert daily[-1]["pricing"] == result


@pytest.mark.parametrize("seconds,observed,above", [(60, 0, 0), (600, 280, 140)])
async def test_ev_adjusted_costs_use_joint_coverage_and_ignore_standing_charge(
    store: Store, seconds: int, observed: int, above: int
) -> None:
    config = await seed(store, seconds=seconds)
    await store.save_config(
        ConfigUpdate(
            expected_revision=config["revision"],
            home_assistant_url="http://ha",
            home_assistant_entity="sensor.power",
            home_assistant_token=SecretStr("fixture-token"),
            home_assistant_enabled=True,
        )
    )
    config = await store.config()
    end = START + seconds * 1_000_000
    third = seconds // 3 * 1_000_000
    await store.save_ev_history(
        config,
        START,
        end,
        [
            EVInterval(START, START + third, 7_000_000),
            EVInterval(START + third, START + 2 * third, 0),
            EVInterval(START + 2 * third, end, None),
        ],
    )
    await store.save_tariff_rates(
        config,
        START,
        end,
        [TariffRate(START, end, Decimal(30), CODE, "STANDARD")],
        cache_details(START, end),
    )
    result = await Analytics(store).summary(START, end, "UTC", exclude_ev=True)
    assert result["observed_seconds"] == observed
    assert result["pricing"]["energy_when_above_gbp"] == (
        pytest.approx(8 * above / 3600 * 0.3) if observed else None
    )
    assert result["pricing"]["excess_energy_gbp"] == (
        pytest.approx(2 * above / 3600 * 0.3) if observed else None
    )
    assert result["pricing"]["priced_seconds_above"] == above
    daily = await Analytics(store).daily_peaks(end, "UTC", exclude_ev=True)
    assert daily["daily_peaks"][-1]["pricing"] == result["pricing"]


async def test_daily_ev_costs_only_price_joint_history_within_raw_retention(store: Store) -> None:
    config = await seed(store)
    await store.save_config(
        ConfigUpdate(
            expected_revision=config["revision"],
            home_assistant_url="http://ha",
            home_assistant_entity="sensor.power",
            home_assistant_token=SecretStr("fixture-token"),
            home_assistant_enabled=True,
        )
    )
    config = await store.config()
    end = START + 60_000_000
    await store.save_ev_history(config, START, end, [EVInterval(START, end, 0)])
    await store.save_tariff_rates(
        config,
        START,
        end,
        [TariffRate(START, end, Decimal(30), CODE, "STANDARD")],
        cache_details(START, end),
    )
    async with store.write() as conn:
        await conn.execute(
            "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?)",
            (DEVICE, START + 30_000_000),
        )
    days = (await Analytics(store).daily_peaks(end, "UTC", exclude_ev=True))["daily_peaks"]
    assert all(day["pricing"]["energy_when_above_gbp"] is None for day in days[:-1])
    assert all(day["pricing"]["excess_energy_gbp"] is None for day in days[:-1])
    assert days[-1]["observed_seconds"] == 30
    assert days[-1]["coverage_pct"] == 50
    assert days[-1]["pricing"]["priced_seconds_above"] == 30
    assert days[-1]["pricing"]["price_coverage_pct"] == 100
    assert days[-1]["pricing"]["energy_when_above_gbp"] == pytest.approx(0.02)
    assert days[-1]["pricing"]["excess_energy_gbp"] == pytest.approx(0.005)


async def test_cache_is_revision_fenced_and_discarded_on_meter_change(store: Store) -> None:
    config = await seed(store)
    rates = [TariffRate(START, START + 60_000_000, Decimal(30), CODE, "STANDARD")]
    await store.save_tariff_rates(
        config, START, START + 60_000_000, rates, cache_details(START, START + 60_000_000)
    )
    await store.save_config(
        ConfigUpdate(expected_revision=config["revision"], display_timezone="UTC")
    )
    with pytest.raises(Conflict):
        await store.save_tariff_rates(config, START, START + 60_000_000, [], {})
    config = await store.config()
    async with store.read() as conn:
        assert await store.tariff_snapshot(conn, config) is not None
    await store.save_config(
        ConfigUpdate(
            expected_revision=config["revision"], active_device_id="AA-BB-CC-DD-EE-FF-00-12"
        )
    )
    async with store.read() as conn:
        assert await store.tariff_snapshot(conn, await store.config()) is None


async def test_refresh_and_worker_keep_meter_polling_independent(store: Store) -> None:
    config = await enable(store)
    await store.save_config(
        ConfigUpdate(expected_revision=config["revision"], polling_enabled=False)
    )
    client = FixtureKraken(
        account([agreement()]),
        lambda _: httpx.Response(200, json={"results": [public_rate()], "next": None}),
    )
    published = asyncio.Event()

    async def publish() -> None:
        published.set()

    pricing = TariffPricing(store, client, publish)
    worker = asyncio.create_task(pricing.run())
    try:
        await asyncio.wait_for(published.wait(), 2)
        status = await pricing.status()
        assert status["enabled"] and status["fetched_at"] and status["rates"]
        assert not (await store.config())["polling_enabled"]
        assert client.discoveries == 1
        with pytest.raises(KrakenError, match="throttled"):
            await pricing.refresh()
        assert client.discoveries == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_failed_refresh_retains_dated_cache(store: Store) -> None:
    await enable(store)
    missing = False

    def upstream(_: httpx.Request) -> httpx.Response:
        rows = [] if missing else [public_rate()]
        return httpx.Response(200, json={"results": rows, "next": None})

    client = FixtureKraken(account([agreement()]), upstream)

    async def publish() -> None:
        pass

    pricing = TariffPricing(store, client, publish)
    try:
        await pricing.refresh()
        original = await pricing.status()
        missing, pricing.not_before = True, 0
        with pytest.raises(KrakenError, match="tariff_rates_missing"):
            await pricing.refresh()
        failed = await pricing.status()
        assert failed["error_code"] == "tariff_rates_missing"
        assert failed["rates"] == original["rates"]
        assert failed["fetched_at"] == original["fetched_at"]
    finally:
        await client.close()


async def test_manual_recovery_resumes_automatic_refresh(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enable(store)
    monkeypatch.setattr("app.tariffs.REFRESH_SECONDS", 0.05)
    failed, published = asyncio.Event(), asyncio.Event()
    healthy = False

    def upstream(_: httpx.Request) -> httpx.Response:
        if not healthy:
            failed.set()
            return httpx.Response(400)
        return httpx.Response(200, json={"results": [public_rate()], "next": None})

    async def publish() -> None:
        published.set()

    client = FixtureKraken(account([agreement()]), upstream)
    pricing = TariffPricing(store, client, publish)
    worker = asyncio.create_task(pricing.run())
    try:
        await asyncio.wait_for(failed.wait(), 2)
        async with pricing.lock:
            assert pricing.error_code == "tariff_prices_http_error"
        healthy, pricing.not_before = True, 0
        await pricing.refresh()
        published.clear()
        pricing.not_before = 0
        await asyncio.wait_for(published.wait(), 2)
        assert client.discoveries == 3
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_tariff_api_uses_saved_settings_and_keeps_credentials_private(tmp_path: Path) -> None:
    provider = FixtureKraken(
        account([agreement()]),
        lambda _: httpx.Response(
            200,
            json={
                "results": [public_rate(start=timestamp_us("2020-01-01T00:00:00Z"))],
                "next": None,
            },
        ),
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
        client=provider,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        assert (
            await client.post("/api/tariff/refresh", json={"expected_revision": 1})
        ).status_code == 403
        session = (await client.get("/api/auth/session")).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        missing = await client.post(
            "/api/config", json={"expected_revision": 1, "tariff_pricing_enabled": True}
        )
        assert missing.status_code == 422
        saved = await client.post(
            "/api/config",
            json={
                "expected_revision": 1,
                "account_number": "A-TEST",
                "active_device_id": DEVICE,
                "api_key": "fixture-private-tariff-key",
                "tariff_pricing_enabled": True,
            },
        )
        assert saved.status_code == 200 and saved.json()["tariff_pricing_enabled"]
        response = await client.post("/api/tariff/refresh", json={"expected_revision": 2})
        assert response.status_code == 200, response.text
        assert response.json()["tariff_name"] == "Fixture tariff"
        assert "fixture-private-tariff-key" not in response.text
        assert (await client.get("/api/config")).json()["revision"] == 2
        again = await client.post("/api/tariff/refresh", json={"expected_revision": 2})
        assert again.status_code == 429
        assert again.headers["Retry-After"]
        assert provider.discoveries == 1
        partial = await client.get("/api/tariff", params={"from": iso(START)})
        assert partial.status_code == 422
        historic = await client.get(
            "/api/tariff",
            params={
                "from": iso(START),
                "to": iso(START + DAY_US),
                "device_id": DEVICE,
            },
        )
        assert historic.status_code == 200
        assert historic.json()["rates"]
        other = await client.get(
            "/api/tariff",
            params={
                "from": iso(START),
                "to": iso(START + DAY_US),
                "device_id": "AA-BB-CC-DD-EE-FF-00-12",
            },
        )
        assert other.status_code == 200 and not other.json()["rates"]


async def test_historical_overlay_uses_only_cached_applicable_bands(store: Store) -> None:
    config = await enable(store)
    rates = [
        TariffRate(START, START + 3600_000_000, Decimal("6.6"), CODE, "ECO7_NIGHT"),
        TariffRate(START + 3600_000_000, START + 7200_000_000, Decimal("36.294"), CODE, "ECO7_DAY"),
    ]
    await store.save_tariff_rates(
        config, START, START + 7200_000_000, rates, cache_details(START, START + 7200_000_000)
    )
    client = Kraken()

    async def publish() -> None:
        pass

    pricing = TariffPricing(store, client, publish)
    try:
        historic = await pricing.status((START, START + 60_000_000), DEVICE)
        assert len(historic["rates"]) == 1
        assert historic["rates"][0]["band"] == "ECO7_NIGHT"
        day = await pricing.status((START + 3600_000_000, START + 7200_000_000), DEVICE)
        assert [row["band"] for row in day["rates"]] == ["ECO7_DAY"]
        missing = await pricing.status((START - DAY_US, START), DEVICE)
        assert not missing["rates"]
        assert missing["state"] == "rates_unavailable"
        assert await store.config() == config
    finally:
        await client.close()


async def test_intelligent_daytime_fetch_preserves_matching_dated_night_prices() -> None:
    start = timestamp_us("2026-10-26T18:00:00Z")
    candidate = agreement(True)
    product = "INTELLI-VAR-26-01-01"
    code = f"E-1R-{product}-A"
    candidate["tariff"].update(productCode=product, tariffCode=code)

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "next": None,
                "results": [public_rate(8 if "/night-unit-rates/" in request.url.path else 30)],
            },
        )

    client = FixtureKraken(account([candidate]), upstream)
    try:
        rows, details = await OctopusTariffs(client).fetch(
            {"octopus_api_key": "test-key", "account_number": "A-TEST", "active_device_id": DEVICE},
            start,
            start + 1800_000_000,
        )
        assert [row.band for row in rows] == ["ECO7_DAY"]
        assert details["agreements"][0]["smart_ev"] is True
        assert details["off_peak_rates"] == [
            {
                "from": iso(start),
                "to": iso(start + 1800_000_000),
                "pence_per_kwh": "8",
                "tariff_code": code,
            }
        ]
    finally:
        await client.close()
