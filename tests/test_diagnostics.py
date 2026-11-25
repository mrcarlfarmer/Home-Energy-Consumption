import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from app.db.store import Store
from app.main import create_app
from app.settings import Settings


async def test_diagnostics_persist_safe_messages_and_require_session(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("a-long-test-password")
    app = create_app(
        Settings(
            database=tmp_path / "diagnostics.sqlite3",
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
        assert (await client.get("/api/diagnostics")).status_code == 401
        assert (await client.post(
            "/api/auth/login", json={"username": "admin", "password": "a-long-test-password"}
        )).status_code == 200
        logger = logging.getLogger("app.poller")
        logger.info("Collection completed: rows=%s changed=%s rejected=%s", 12, 4, 0)
        logger.warning("Unexpected secret: %s", "sensitive-value")
        await asyncio.sleep(0)
        await app.state.diagnostics.flush_events()
        response = await client.get("/api/diagnostics?limit=1")
        assert response.status_code == 200
        event = response.json()["events"][0]
        assert event["message"] == "Application error (details withheld)"
        assert "sensitive-value" not in response.text
        previous = await client.get(f"/api/diagnostics?before={event['id']}")
        assert previous.json()["events"][0]["message"] == (
            "Collection completed: rows=12 changed=4 rejected=0"
        )
        assert (await client.get("/api/diagnostics?limit=101")).status_code == 422
    async with app.router.lifespan_context(app):
        events = await app.state.store.diagnostics(None, 100)
        assert any(event["message"] == "Request validation failed" for event in events)
        assert any(event["message"] == "Request failed: code=session_required status=401" for event in events)


async def test_diagnostics_reports_unavailable_storage(store: Store) -> None:
    from app.diagnostics import Diagnostics

    store.capacity = AsyncMock(return_value={"storage_paused": True})  # type: ignore[method-assign]
    diagnostics = Diagnostics(store)
    diagnostics.enqueue((1, "WARNING", "app.poller", "Collection failed"))
    await diagnostics.flush_events()
    assert diagnostics.dropped == 1
