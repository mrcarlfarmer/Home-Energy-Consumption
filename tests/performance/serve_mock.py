"""Run production tasks with a synthetic Kraken transport on a disposable benchmark DB."""

import asyncio
import json
import time

import httpx
import uvicorn
from app.db.store import Store
from app.kraken import Kraken
from app.main import create_app
from app.models import ConfigUpdate, iso, timestamp_us
from app.settings import Settings
from pydantic import SecretStr

DEVICE = "AA-BB-CC-DD-EE-FF-00-11"


async def configure(settings: Settings, enabled: bool) -> None:
    store = Store(settings.database)
    await store.open()
    try:
        config = await store.config()
        if config["account_number"] != "A-SYNTHETIC":
            raise ValueError(
                "This harness only accepts a disposable A-SYNTHETIC benchmark database"
            )
        await store.save_config(
            ConfigUpdate(
                expected_revision=config["revision"],
                polling_enabled=enabled,
                api_key=SecretStr("synthetic-not-an-octopus-key") if enabled else None,
            )
        )
    finally:
        await store.close()


async def upstream(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(0.05)
    body = json.loads(request.content)
    if "ObtainToken" in body["query"]:
        data = {
            "obtainKrakenToken": {
                "token": "synthetic-token",
                "payload": {"exp": time.time() + 3600},
                "refreshToken": "synthetic-refresh",
                "refreshExpiresIn": int(time.time() + 86400),
            }
        }
    elif "ElectricityDevices" in body["query"]:
        data = {
            "account": {
                "electricityAgreements": [
                    {
                        "meterPoint": {"meters": [{"smartDevices": [{"deviceId": DEVICE}]}]},
                    }
                ]
            }
        }
    else:
        end = timestamp_us(body["variables"]["end"])
        start = max(timestamp_us(body["variables"]["start"]), end - 180_000_000)
        data = {
            "smartMeterTelemetry": [
                {"readAt": iso(at), "demand": 4000}
                for at in range((start // 10_000_000 + 1) * 10_000_000, end, 10_000_000)
            ]
        }
    return httpx.Response(200, json={"data": data})


if __name__ == "__main__":
    settings = Settings.from_env()
    asyncio.run(configure(settings, True))
    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    app = create_app(settings, client)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=8443,
            workers=1,
            http="h11",
            proxy_headers=False,
            ssl_certfile=str(settings.certificate),
            ssl_keyfile=str(settings.private_key),
            limit_concurrency=16,
            backlog=64,
            access_log=False,
            timeout_graceful_shutdown=25,
        )
    )
    app.state.stop_server = lambda: setattr(server, "should_exit", True)
    try:
        server.run()
    finally:
        asyncio.run(configure(settings, False))
