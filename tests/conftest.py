from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.db.store import Store
from app.models import ConfigUpdate
from pydantic import SecretStr

DEVICE = "AA-BB-CC-DD-EE-FF-00-11"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = Store(tmp_path / "energy.sqlite3")
    await database.open()
    await database.save_config(
        ConfigUpdate(
            expected_revision=1,
            account_number="A-TEST",
            active_device_id=DEVICE,
            api_key=SecretStr("test-key"),
            polling_enabled=True,
        )
    )
    try:
        yield database
    finally:
        await database.close()
