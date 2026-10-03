import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from app.auth import COOKIE
from app.kraken import Kraken
from app.main import create_app
from app.settings import Settings


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("true", True), ("false", False), ("", None), ("False", None), ("0", None)],
)
def test_authentication_requires_explicit_opt_out(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool | None
) -> None:
    monkeypatch.setenv("APP_TRANSPORT", "http")
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", "http://testserver")
    if value is None:
        monkeypatch.delenv("APP_AUTH_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("APP_AUTH_REQUIRED", value)
    if expected is None:
        with pytest.raises(ValueError, match="APP_AUTH_REQUIRED must be true or false"):
            Settings.from_env()
    else:
        assert Settings.from_env().auth_required is expected


def settings_at(path: Path) -> Settings:
    return Settings(
        database=path,
        auth_required=False,
        secure=False,
        allowed_hosts=("testserver",),
        allowed_origins=("http://testserver",),
        start_workers=False,
    )


async def test_password_free_api_keeps_csrf_origin_redaction_and_workers(tmp_path: Path) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
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

    app = create_app(
        replace(settings_at(tmp_path / "data.sqlite3"), start_workers=True),
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
        assert app.state.auth is None
        assert await app.state.store.password_hash() is None
        assert await app.state.store.load_sessions() == []
        config = await client.get("/api/config")
        assert config.status_code == 200
        state = await client.get("/api/auth/session")
        assert state.status_code == 200
        assert not state.json()["authentication_required"]
        assert not state.json()["authenticated"]
        assert "set-cookie" not in state.headers
        change = {
            "expected_revision": config.json()["revision"],
            "account_number": "A-FIXTURE",
            "api_key": "never-echo-this-key",
            "active_device_id": "AA-BB-CC-DD-EE-FF-00-11",
        }
        assert (await client.post("/api/config", json=change)).status_code == 403
        client.headers["X-CSRF-Token"] = state.json()["csrf_token"]
        assert (
            await client.post("/api/config", json=change, headers={"Origin": "http://evil.test"})
        ).status_code == 403
        assert (await client.get("/api/config", headers={"Host": "evil.test"})).status_code == 400
        saved = await client.post("/api/config", json=change)
        assert saved.status_code == 200
        assert saved.json()["has_api_key"]
        assert "never-echo-this-key" not in saved.text
        assert "never-echo-this-key" not in (await client.get("/api/config")).text
        assert (
            await client.post(
                "/api/config",
                json={"expected_revision": saved.json()["revision"], "auth_required": True},
            )
        ).status_code == 422
        tested = await client.post("/api/config/test", json={"active_device_id": None})
        assert tested.status_code == 200, tested.text
        assert tested.json()["authenticated"]
        params = {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"}
        for route in ("summary", "history"):
            result = await client.get(f"/api/analytics/{route}", params=params)
            assert result.status_code == 200, result.text
        assert (await client.get("/api/openapi.json")).status_code == 200
        assert (
            await client.post("/api/auth/login", json={"password": "unused-password"})
        ).status_code == 409
        assert (await client.post("/api/auth/logout", json={})).status_code == 409
        await asyncio.sleep(0.05)
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
        assert not client.cookies
        assert await app.state.store.password_hash() is None
        assert await app.state.store.load_sessions() == []


async def test_password_free_restart_rotates_csrf_without_requiring_login(tmp_path: Path) -> None:
    settings = settings_at(tmp_path / "data.sqlite3")
    previous = ""
    for _ in range(2):
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Origin": "http://testserver"},
            ) as client,
        ):
            response = await client.get("/api/auth/session")
            assert response.status_code == 200
            token = response.json()["csrf_token"]
            assert token and token != previous
            revision = (await client.get("/api/config")).json()["revision"]
            change = {"expected_revision": revision, "poll_interval_seconds": 70}
            assert (
                await client.post("/api/config", json=change, headers={"X-CSRF-Token": previous})
            ).status_code == 403
            assert (
                await client.post("/api/config", json=change, headers={"X-CSRF-Token": token})
            ).status_code == 200
            previous = token


async def test_reenabling_login_requires_bootstrap_for_a_password_free_database(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path / "data.sqlite3")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert await app.state.store.password_hash() is None
    protected = create_app(replace(settings, auth_required=True))
    with pytest.raises(RuntimeError, match="First startup requires APP_ADMIN_PASSWORD_FILE"):
        async with protected.router.lifespan_context(protected):
            pytest.fail("Authentication must not silently fall back to password-free mode")
    password = tmp_path / "password"
    password.write_text("new-bootstrap-password")
    protected = create_app(replace(settings, auth_required=True, password_file=password))
    async with (
        protected.router.lifespan_context(protected),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=protected),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        assert (await client.get("/api/config")).status_code == 401
        assert (
            await client.post("/api/auth/login", json={"password": password.read_text()})
        ).status_code == 200


async def test_disabling_login_revokes_sessions_but_preserves_password_and_data(
    tmp_path: Path,
) -> None:
    password = tmp_path / "password"
    password.write_text("existing-fixture-password")
    protected = replace(
        settings_at(tmp_path / "data.sqlite3"), auth_required=True, password_file=password
    )
    first = create_app(protected)
    async with (
        first.router.lifespan_context(first),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        login = await client.post("/api/auth/login", json={"password": password.read_text()})
        assert login.json()["authentication_required"]
        cookie = login.cookies[COOKIE]
        encoded = await first.state.store.password_hash()
        saved = await client.post(
            "/api/config",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={
                "expected_revision": 1,
                "api_key": "retained-fixture-key",
                "poll_interval_seconds": 90,
            },
        )
        assert saved.status_code == 200

    for required in (False, True):
        app = create_app(replace(protected, auth_required=required, password_file=None))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                cookies={COOKIE: cookie},
                headers={"Origin": "http://testserver"},
            ) as client,
        ):
            assert await app.state.store.password_hash() == encoded
            assert await app.state.store.load_sessions() == []
            stored = await app.state.store.config()
            assert stored["octopus_api_key"] == "retained-fixture-key"
            assert stored["poll_interval_seconds"] == 90
            assert (await client.get("/api/config")).status_code == (401 if required else 200)
            if required:
                result = await client.post(
                    "/api/auth/login", json={"password": "existing-fixture-password"}
                )
                assert result.status_code == 200
                assert result.json()["authentication_required"]
