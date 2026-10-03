"""Deterministic browser-test server; never used by the production image."""

import argparse
import asyncio
import tempfile
from pathlib import Path

import uvicorn
from app.db.store import Store
from app.main import create_app
from app.models import ConfigUpdate, Reading, iso, now_us
from app.settings import Settings
from pydantic import SecretStr

DEVICE = "AA-BB-CC-DD-EE-FF-00-11"


async def seed(path: Path) -> None:
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
                Reading.from_kraken({"readAt": iso(t), "demand": 4000}, at)
                for t in range(at - 7200_000_000, at + 1, 10_000_000)
            ],
            2,
        )
        await store.rebuild(100)
        await store.save_config(ConfigUpdate(expected_revision=2, polling_enabled=False))
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-login", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="energy-browser-test-") as temporary:
        directory = Path(temporary)
        password = None
        if not args.no_login:
            password = directory / "password"
            password.write_text("fixture-only-password")
        database = directory / "energy.sqlite3"
        asyncio.run(seed(database))
        uvicorn.run(
            create_app(
                Settings(
                    database=database,
                    password_file=password,
                    auth_required=not args.no_login,
                    secure=False,
                    start_workers=False,
                    allowed_hosts=("127.0.0.1",),
                    allowed_origins=(f"http://127.0.0.1:{args.port}",),
                )
            ),
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
        )
