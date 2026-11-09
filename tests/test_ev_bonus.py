import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.analytics.effective_rates import effective_rates
from app.analytics.service import Analytics
from app.db.store import Store, one
from app.main import create_app
from app.models import ConfigUpdate, EVInterval, Reading, TariffRate, iso, timestamp_us
from app.settings import Settings
from conftest import DEVICE
from pydantic import SecretStr, ValidationError

START = timestamp_us("2026-10-26T18:00:00Z")
MINUTE = 60_000_000
HALF_HOUR = 30 * MINUTE
CODE = "E-1R-INTELLI-VAR-26-01-01-A"


async def configure(store: Store) -> dict[str, Any]:
    config = await store.config()
    await store.save_config(
        ConfigUpdate(
            expected_revision=config["revision"],
            tariff_pricing_enabled=True,
            home_assistant_url="http://ha.fixture",
            home_assistant_entity="sensor.ohme_power",
            home_assistant_token=SecretStr("fixture-token"),
            home_assistant_enabled=True,
            ev_savings_from="2026-10-01",
        )
    )
    return await store.config()


async def cache(
    store: Store,
    config: dict[str, Any],
    start: int = START,
    end: int = START + HALF_HOUR,
    *,
    night_price: str | None = "8",
    band: str = "ECO7_DAY",
    smart: bool = True,
) -> None:
    await store.save_tariff_rates(
        config,
        start,
        end,
        [TariffRate(start, end, Decimal("8" if band == "ECO7_NIGHT" else "30"), CODE, band)],
        {
            "agreements": [
                {
                    "from": iso(start),
                    "to": iso(end),
                    "name": "Intelligent fixture",
                    "tariff_code": CODE,
                    "schedule": "Europe/London",
                    "smart_ev": smart,
                }
            ],
            "off_peak_rates": [
                {
                    "from": iso(start),
                    "to": iso(end),
                    "pence_per_kwh": night_price,
                    "tariff_code": CODE,
                }
            ]
            if night_price is not None
            else [],
            "warnings": [],
        },
    )


async def history(store: Store, config: dict[str, Any], rows: list[EVInterval]) -> None:
    for left in range(rows[0].start, rows[-1].end, 15 * MINUTE):
        right = min(left + 15 * MINUTE, rows[-1].end)
        await store.save_ev_history(
            config,
            left,
            right,
            [
                EVInterval(max(left, row.start), min(right, row.end), row.power_mw)
                for row in rows
                if row.start < right and row.end > left
            ],
        )


async def demand(store: Store, config: dict[str, Any], start: int, end: int) -> None:
    await store.ingest(
        DEVICE,
        [
            Reading.from_kraken({"readAt": iso(at), "demand": 8000}, end)
            for at in range(start, end + 1, 10_000_000)
        ],
        config["revision"],
    )


async def resolve(store: Store, start: int = START, end: int = START + HALF_HOUR) -> Any:
    config = await store.config()
    async with store.read() as conn:
        snapshot = await store.tariff_snapshot(conn, config)
        return await effective_rates(store, conn, config, DEVICE, start, end, snapshot)


async def test_ten_minutes_award_whole_half_hour_and_price_exact_excess(store: Store) -> None:
    config = await configure(store)
    await cache(store, config)
    await history(
        store,
        config,
        [
            EVInterval(START, START + 7 * MINUTE, 0),
            EVInterval(START + 7 * MINUTE, START + 17 * MINUTE, 1_000_000),
            EVInterval(START + 17 * MINUTE, START + HALF_HOUR, 0),
        ],
    )
    await demand(store, config, START, START + HALF_HOUR)
    resolved = await resolve(store)
    assert resolved.charging == [(START + 7 * MINUTE, START + 17 * MINUTE)]
    assert resolved.ev_coverage_pct == 100
    assert not resolved.unresolved
    assert resolved.rates == [TariffRate(START, START + HALF_HOUR, Decimal(8), CODE, "EV_BONUS")]
    analytics = Analytics(store)
    summary = await analytics.summary(START, START + HALF_HOUR, "UTC")
    assert summary["pricing"]["energy_when_above_gbp"] == pytest.approx(0.32)
    assert summary["pricing"]["excess_energy_gbp"] == pytest.approx(0.08)
    assert summary["thresholds"][0]["seconds_above"] == 1800
    # A zoom before the charging event must still receive the same half-hour rate.
    zoom = await analytics.summary(START, START + MINUTE, "UTC")
    assert zoom["pricing"]["excess_energy_gbp"] == pytest.approx(2 / 60 * 0.08)
    adjusted = await analytics.summary(START, START + HALF_HOUR, "UTC", exclude_ev=True)
    assert adjusted["pricing"]["excess_energy_gbp"] == pytest.approx((2 * 20 + 1 * 10) / 60 * 0.08)
    assert adjusted["pricing"]["pricing_revision"] == summary["pricing"]["pricing_revision"]
    daily = await analytics.daily_peaks(START + HALF_HOUR, "UTC")
    assert daily["daily_peaks"][-1]["pricing"]["excess_energy_gbp"] == pytest.approx(0.08)


async def test_missing_ev_history_is_not_a_peak_price_but_known_zero_is(store: Store) -> None:
    config = await configure(store)
    await cache(store, config)
    await demand(store, config, START, START + HALF_HOUR)
    await history(
        store,
        config,
        [
            EVInterval(START, START + 15 * MINUTE, None),
            EVInterval(START + 15 * MINUTE, START + HALF_HOUR, 0),
        ],
    )
    resolved = await resolve(store)
    assert resolved.rates == []
    assert resolved.unresolved == [(START, START + HALF_HOUR)]
    assert resolved.ev_coverage_pct == 50
    price = (await Analytics(store).summary(START, START + HALF_HOUR, "UTC"))["pricing"]
    assert price["available"] is False
    assert price["excess_energy_gbp"] is None
    assert price["price_coverage_pct"] == 0
    await history(store, config, [EVInterval(START, START + HALF_HOUR, 0)])
    price = (await Analytics(store).summary(START, START + HALF_HOUR, "UTC"))["pricing"]
    assert price["excess_energy_gbp"] == pytest.approx(0.30)
    assert price["price_coverage_pct"] == 100


async def test_positive_evidence_earns_slot_despite_other_missing_minutes(store: Store) -> None:
    config = await configure(store)
    await cache(store, config)
    await history(
        store,
        config,
        [
            EVInterval(START, START + MINUTE, 250_000),
            EVInterval(START + MINUTE, START + HALF_HOUR, None),
        ],
    )
    resolved = await resolve(store)
    assert resolved.rates[0].band == "EV_BONUS"
    assert not resolved.unresolved
    assert resolved.ev_coverage_pct == pytest.approx(100 / 30)


@pytest.mark.parametrize("price", ["-5", "0", "8"])
async def test_missing_and_negative_dated_night_prices(store: Store, price: str) -> None:
    config = await configure(store)
    await cache(store, config, night_price=None)
    await history(store, config, [EVInterval(START, START + HALF_HOUR, 1_000_000)])
    await demand(store, config, START, START + HALF_HOUR)
    assert (await resolve(store)).unresolved == [(START, START + HALF_HOUR)]
    await cache(store, config, night_price=price)
    summary = await Analytics(store).summary(START, START + HALF_HOUR, "UTC")
    assert summary["pricing"]["excess_energy_gbp"] == pytest.approx(float(price) / 100)


async def test_corrections_and_rate_refresh_preserve_evidence(store: Store) -> None:
    config = await configure(store)
    await cache(store, config)
    await history(store, config, [EVInterval(START, START + HALF_HOUR, 1_000_000)])
    old = await resolve(store)
    await cache(store, config, night_price="9")
    refreshed = await resolve(store)
    assert refreshed.rates[0].pence_per_kwh == 9
    assert refreshed.charging == old.charging
    assert refreshed.revision != old.revision
    await history(store, config, [EVInterval(START, START + HALF_HOUR, 0)])
    corrected = await resolve(store)
    assert not corrected.charging
    assert corrected.rates[0].band == "ECO7_DAY"
    assert corrected.revision != refreshed.revision


async def test_policy_date_non_intelligent_and_night_rates_are_not_overridden(store: Store) -> None:
    config = await configure(store)
    for start, band, smart in [
        (timestamp_us("2026-09-30T18:00:00Z"), "ECO7_DAY", True),
        (START, "ECO7_DAY", False),
        (START, "ECO7_NIGHT", True),
        (START, "STANDARD", False),
    ]:
        await cache(store, config, start, start + HALF_HOUR, band=band, smart=smart)
        resolved = await resolve(store, start, start + HALF_HOUR)
        assert [row.band for row in resolved.rates] == [band]
        assert not resolved.unresolved


async def test_cross_half_hour_and_query_end_are_half_open(store: Store) -> None:
    config = await configure(store)
    await cache(store, config, START, START + 2 * HALF_HOUR)
    await history(
        store,
        config,
        [
            EVInterval(START, START + 29 * MINUTE, 0),
            EVInterval(START + 29 * MINUTE, START + 31 * MINUTE, 2_000_000),
            EVInterval(START + 31 * MINUTE, START + 2 * HALF_HOUR, 0),
        ],
    )
    resolved = await resolve(store, START, START + 2 * HALF_HOUR)
    assert (
        sum(row.end - row.start for row in resolved.rates if row.band == "EV_BONUS")
        == 2 * HALF_HOUR
    )
    assert sum(b - a for a, b in resolved.charging) == 2 * MINUTE
    before = await resolve(store, START, START + MINUTE)
    assert before.rates[0].band == "EV_BONUS"
    assert before.charging == []


async def test_rate_change_inside_awarded_slot_keeps_exact_prices(store: Store) -> None:
    config = await configure(store)
    await cache(store, config)
    async with store.write() as conn:
        row = await one(conn, "SELECT details_json FROM tariff_cache")
        details = json.loads(row[0])
        details["off_peak_rates"] = [
            {"from": iso(a), "to": iso(b), "pence_per_kwh": p, "tariff_code": CODE}
            for a, b, p in [
                (START, START + 10 * MINUTE, "8"),
                (START + 10 * MINUTE, START + HALF_HOUR, "10"),
            ]
        ]
        await conn.execute("UPDATE tariff_cache SET details_json=?", (json.dumps(details),))
    await history(store, config, [EVInterval(START, START + HALF_HOUR, 1_000_000)])
    await demand(store, config, START, START + HALF_HOUR)
    price = (await Analytics(store).summary(START, START + HALF_HOUR, "UTC"))["pricing"]
    assert price["excess_energy_gbp"] == pytest.approx(2 * (8 * 10 + 10 * 20) / 6000)


@pytest.mark.parametrize("value", ["", "01/10/2026", "2026-02-30", "1969-12-31"])
def test_savings_date_validation(value: str) -> None:
    with pytest.raises(ValidationError):
        ConfigUpdate(expected_revision=1, ev_savings_from=value)


async def test_assumption_is_opt_in_and_clears_on_meter_change(store: Store) -> None:
    assert (await store.config())["ev_savings_from"] is None
    config = await configure(store)
    await store.save_config(
        ConfigUpdate(
            expected_revision=config["revision"], active_device_id="AA-BB-CC-DD-EE-FF-00-12"
        )
    )
    assert (await store.config())["ev_savings_from"] is None


async def test_activation_uses_uk_date_and_late_charge_outside_view(store: Store) -> None:
    config = await configure(store)
    midnight = timestamp_us("2026-09-30T23:00:00Z")
    await cache(store, config, midnight - HALF_HOUR, midnight + HALF_HOUR)
    await history(
        store,
        config,
        [
            EVInterval(midnight - HALF_HOUR, midnight + HALF_HOUR, 1_000_000),
        ],
    )
    before = await resolve(store, midnight - HALF_HOUR, midnight)
    after = await resolve(store, midnight, midnight + MINUTE)
    assert before.rates[0].band == "ECO7_DAY"
    assert after.rates[0].band == "EV_BONUS"


async def test_repeated_dst_hour_uses_distinct_settlement_slots(store: Store) -> None:
    config = await configure(store)
    start = timestamp_us("2026-10-25T00:00:00Z")
    await cache(store, config, start, start + 120 * MINUTE)
    await history(
        store,
        config,
        [
            EVInterval(start, start + MINUTE, 1_000_000),
            EVInterval(start + MINUTE, start + 60 * MINUTE, 0),
            EVInterval(start + 60 * MINUTE, start + 61 * MINUTE, 1_000_000),
            EVInterval(start + 61 * MINUTE, start + 120 * MINUTE, 0),
        ],
    )
    resolved = await resolve(store, start, start + 120 * MINUTE)
    assert [(row.start, row.end) for row in resolved.rates if row.band == "EV_BONUS"] == [
        (start, start + HALF_HOUR),
        (start + 60 * MINUTE, start + 90 * MINUTE),
    ]


async def test_source_change_and_disabling_assumption(store: Store) -> None:
    config = await configure(store)
    await cache(store, config)
    await history(store, config, [EVInterval(START, START + HALF_HOUR, 1_000_000)])
    await store.save_config(
        ConfigUpdate(
            expected_revision=config["revision"], home_assistant_entity="sensor.other_power"
        )
    )
    changed = await store.config()
    assert changed["ev_savings_from"] is None
    assert (await resolve(store)).rates[0].band == "ECO7_DAY"
    await store.save_config(
        ConfigUpdate(expected_revision=changed["revision"], ev_savings_from="2026-10-01")
    )
    assert (await resolve(store)).rates == []


async def test_overnight_charging_is_visible_without_requiring_it_for_home_rate(store: Store) -> None:
    config = await configure(store)
    start = timestamp_us("2026-10-27T02:00:00Z")
    await cache(store, config, start, start + HALF_HOUR, band="ECO7_NIGHT")
    unknown = await resolve(store, start, start + HALF_HOUR)
    assert unknown.rates[0].band == "ECO7_NIGHT"
    assert unknown.ev_coverage_pct == 0
    await history(store, config, [EVInterval(start, start + HALF_HOUR, 1_000_000)])
    known = await resolve(store, start, start + HALF_HOUR)
    assert known.rates == unknown.rates
    assert known.charging == [(start, start + HALF_HOUR)]
    assert known.ev_coverage_pct == 100


async def test_legacy_cache_never_substitutes_day_price_for_bonus(store: Store) -> None:
    config = await configure(store)
    await cache(store, config)
    async with store.write() as conn:
        details = json.loads((await one(conn, "SELECT details_json FROM tariff_cache"))[0])
        del details["agreements"][0]["smart_ev"]
        del details["off_peak_rates"]
        await conn.execute("UPDATE tariff_cache SET details_json=?", (json.dumps(details),))
    await history(store, config, [EVInterval(START, START + HALF_HOUR, 1_000_000)])
    assert (await resolve(store)).rates == []
    assert (await resolve(store)).unresolved == [(START, START + HALF_HOUR)]


async def test_bonus_api_contract_and_revision_consistency(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            database=tmp_path / "api.sqlite3",
            auth_required=False,
            secure=False,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            start_workers=False,
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
        session = (await client.get("/api/auth/session")).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        saved = await client.post(
            "/api/config",
            json={
                "expected_revision": 1,
                "account_number": "A-TEST",
                "active_device_id": DEVICE,
                "api_key": "test-key",
                "polling_enabled": True,
            },
        )
        assert saved.status_code == 200
        store = app.state.store
        config = await configure(store)
        await cache(store, config)
        await history(store, config, [EVInterval(START, START + HALF_HOUR, 1_000_000)])
        await demand(store, config, START, START + HALF_HOUR)
        query = {"from": iso(START), "to": iso(START + HALF_HOUR)}
        tariff = await client.get("/api/tariff", params=query)
        summary = await client.get("/api/analytics/summary", params=query)
        assert tariff.status_code == summary.status_code == 200
        assert tariff.json()["rates"][0]["band"] == "EV_BONUS"
        assert tariff.json()["charging"] == [{"from": iso(START), "to": iso(START + HALF_HOUR)}]
        assert tariff.json()["unresolved"] == []
        assert tariff.json()["pricing_revision"] == summary.json()["pricing"]["pricing_revision"]
        assert summary.json()["pricing"]["excess_energy_gbp"] == pytest.approx(0.08)
        other = await client.get(
            "/api/tariff", params={**query, "device_id": "AA-BB-CC-DD-EE-FF-00-12"}
        )
        assert other.json()["rates"] == []
        assert other.json()["charging"] == []
        assert other.json()["adjustment_enabled"] is False
        assert (await client.get("/api/config")).json()["ev_savings_from"] == "2026-10-01"
