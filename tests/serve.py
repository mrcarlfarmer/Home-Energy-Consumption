"""Deterministic browser-test server; never used by the production image."""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
from app.db.store import Store
from app.home_assistant import HomeAssistant
from app.kraken import Kraken
from app.main import create_app
from app.models import ConfigUpdate, EVInterval, Reading, iso, now_us, timestamp_us
from app.settings import Settings
from pydantic import SecretStr

DEVICE = "AA-BB-CC-DD-EE-FF-00-11"


async def seed(path: Path, with_ev: bool = False) -> int:
    store = Store(path)
    await store.open()
    try:
        await store.save_config(
            ConfigUpdate(
                expected_revision=1,
                account_number="A-FIXTURE",
                active_device_id=DEVICE,
                api_key=SecretStr("fixture-only-key"),
                polling_enabled=True,
            )
        )
        at = now_us() // 10_000_000 * 10_000_000
        await store.ingest(
            DEVICE,
            [
                Reading.from_kraken({"readAt": iso(t), "demand": 8000 if with_ev else 4000}, at)
                for t in range(at - 7200_000_000, at + 1, 10_000_000)
            ],
            2,
        )
        await store.rebuild(100)
        await store.save_config(ConfigUpdate(expected_revision=2, polling_enabled=False))
        if with_ev:
            await store.save_config(
                ConfigUpdate(
                    expected_revision=3,
                    home_assistant_url="http://homeassistant.fixture:8123",
                    home_assistant_token=SecretStr("fixture-home-assistant-token"),
                    home_assistant_entity="sensor.ohme_home_pro_power",
                    home_assistant_enabled=True,
                )
            )
            config = await store.config()
            for start in range(at - 7200_000_000, at, 900_000_000):
                await store.save_ev_history(
                    config,
                    start,
                    start + 900_000_000,
                    [
                        EVInterval(
                            start,
                            start + 900_000_000,
                            7_000_000 if start < at - 1800_000_000 else 0,
                        )
                    ],
                )
        return at
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-login", action="store_true")
    parser.add_argument("--with-ev", action="store_true")
    parser.add_argument("--with-tariff", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="energy-browser-test-") as temporary:
        directory = Path(temporary)
        password = None
        if not args.no_login:
            password = directory / "password"
            password.write_text("fixture-only-password")
        database = directory / "energy.sqlite3"
        at = asyncio.run(seed(database, args.with_ev))

        def home_upstream(request: httpx.Request) -> httpx.Response:
            start = (
                now_us()
                if "/states/" in request.url.path
                else timestamp_us(request.url.path.rsplit("/", 1)[1])
            )
            row = {
                "entity_id": "sensor.ohme_home_pro_power",
                "state": "7" if start < at - 1800_000_000 else "0",
                "last_updated": iso(start),
                "last_reported": iso(start),
                "attributes": {"unit_of_measurement": "kW"},
            }
            return httpx.Response(200, json=row if "/states/" in request.url.path else [[row]])

        def tariff_upstream(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "next": None,
                        "results": [
                            {
                                "value_inc_vat": 30,
                                "valid_from": "2020-01-01T00:00:00Z",
                                "valid_to": None,
                                "payment_method": None,
                            }
                        ],
                    },
                )
            query = json.loads(request.content)["query"]
            if "obtainKrakenToken" in query:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "obtainKrakenToken": {
                                "token": "fixture-jwt",
                                "payload": {"exp": time.time() + 3600},
                            }
                        }
                    },
                )
            if "TariffAgreements" in query:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "account": {
                                "electricityAgreements": [
                                    {
                                        "meterPoint": {
                                            "id": "fixture-point",
                                            "direction": "IMPORT",
                                            "meters": [{"smartDevices": [{"deviceId": DEVICE}]}],
                                            "agreements": [
                                                {
                                                    "validFrom": "2020-01-01T00:00:00Z",
                                                    "validTo": None,
                                                    "isRevoked": False,
                                                    "tariff": {
                                                        "productCode": "FIXTURE-26-01-01",
                                                        "tariffCode": "E-1R-FIXTURE-26-01-01-A",
                                                        "displayName": "Fixture Octopus tariff",
                                                        "isExport": False,
                                                    },
                                                    "timeOfUseScheme": None,
                                                }
                                            ],
                                        },
                                    }
                                ]
                            }
                        }
                    },
                )
            raise AssertionError("Unexpected fixture Octopus request")

        app = create_app(
            Settings(
                database=database,
                password_file=password,
                auth_required=not args.no_login,
                secure=False,
                start_workers=False,
                allowed_hosts=("127.0.0.1",),
                allowed_origins=(f"http://127.0.0.1:{args.port}",),
            ),
            client=Kraken(httpx.AsyncClient(transport=httpx.MockTransport(tariff_upstream)))
            if args.with_tariff
            else None,
            home_client=HomeAssistant(
                httpx.AsyncClient(transport=httpx.MockTransport(home_upstream))
            ),
        )
        if args.with_ev:
            app.state.ev_collector.health.update(state="running", power_w=0, reported_at=iso(at))
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
        )
