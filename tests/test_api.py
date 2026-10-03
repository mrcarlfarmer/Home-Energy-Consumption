import json
import time
from pathlib import Path

import httpx
from app.kraken import Kraken
from app.main import create_app
from app.settings import Settings
from app.streaming import Hub


async def test_login_config_redaction_csrf_and_bounds(tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("a-long-test-password")
    app = create_app(
        Settings(
            database=tmp_path / "data.sqlite3",
            password_file=secret,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            secure=False,
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
        assert (await client.get("/api/config")).status_code == 401
        response = await client.post("/api/auth/login", json={"password": "a-long-test-password"})
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        data = {
            "expected_revision": 1,
            "api_key": "private-key",
            "account_number": "A-TEST",
            "active_device_id": "AA-BB-CC-DD-EE-FF-00-11",
            "polling_enabled": True,
        }
        saved = await client.post("/api/config", json=data)
        assert saved.status_code == 200, saved.text
        assert "private-key" not in saved.text
        assert saved.json()["has_api_key"]
        assert (await client.post("/api/config", json=data)).status_code == 409
        client.headers["X-CSRF-Token"] = "wrong"
        assert (await client.post("/api/config", json={"expected_revision": 2})).status_code == 403
        bad = await client.get(
            "/api/analytics/history",
            params={
                "from": "2026-01-01T00:00:00Z",
                "to": "2027-03-01T00:00:00Z",
            },
        )
        assert bad.status_code == 422
        assert (await client.get("/healthz")).status_code == 200
        schema = (await client.get("/api/openapi.json")).json()
        assert "from" in schema["components"]["schemas"]["Summary"]["properties"]
        body = await client.post("/api/config", content=b"x" * 16385)
        assert body.status_code == 413
        assert (
            await client.post("/api/config", json={"api_key": "never-echo-this"})
        ).status_code == 403
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        rejected = await client.post("/api/config", json={"api_key": "never-echo-this"})
        assert rejected.status_code == 422
        assert "never-echo-this" not in rejected.text


def test_hub_coalesces_and_limits() -> None:
    hub = Hub()
    queues = [hub.subscribe() for _ in range(4)]
    import pytest

    with pytest.raises(OverflowError):
        hub.subscribe()
    for i in range(1000):
        hub.publish({"value": i})
    assert all(q.qsize() == 1 and q.get_nowait()[1] == {"value": 999} for q in queues)


async def test_draft_connectivity_is_rate_limited_without_saving(tmp_path: Path) -> None:
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if "ObtainToken" in json.loads(request.content)["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "obtainKrakenToken": {
                            "token": "fixture",
                            "payload": {"exp": time.time() + 3600},
                        }
                    }
                },
            )
        return httpx.Response(200, json={"data": {"account": {"electricityAgreements": []}}})

    secret = tmp_path / "password"
    secret.write_text("fixture-password-only")
    app = create_app(
        Settings(
            database=tmp_path / "db.sqlite3",
            password_file=secret,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            secure=False,
            start_workers=False,
        ),
        Kraken(httpx.AsyncClient(transport=httpx.MockTransport(upstream))),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        login = await client.post("/api/auth/login", json={"password": "fixture-password-only"})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        draft = {"account_number": "A-FIXTURE", "api_key": "never-saved"}
        first = await client.post("/api/config/test", json=draft)
        assert first.status_code == 200
        assert first.json()["selection_required"]
        assert (await client.post("/api/config/test", json=draft)).status_code == 429
        assert calls == 2
        assert not (await client.get("/api/config")).json()["has_api_key"]
