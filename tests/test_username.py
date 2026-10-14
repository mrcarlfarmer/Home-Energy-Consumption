from pathlib import Path

import httpx
import pytest
from app.main import create_app
from app.settings import Settings


async def test_username_required_and_wrong_username_does_not_issue_session(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("username-fixture-password")
    app = create_app(
        Settings(
            database=tmp_path / "db.sqlite3",
            password_file=password,
            admin_username="owner",
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
        for path in (
            "/api/config",
            "/api/auth/session",
            "/api/openapi.json",
            "/api/telemetry/live",
            "/api/analytics/history?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z",
            "/api/analytics/summary?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z",
        ):
            assert (await client.get(path)).status_code == 401
        assert (
            await client.post("/api/auth/login", json={"password": password.read_text()})
        ).status_code == 422
        for username, secret in (("admin", password.read_text()), ("owner", "incorrect")):
            response = await client.post(
                "/api/auth/login", json={"username": username, "password": secret}
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid username or password"
            assert "set-cookie" not in response.headers
            assert await app.state.store.load_sessions() == []
        response = await client.post(
            "/api/auth/login", json={"username": "owner", "password": password.read_text()}
        )
        assert response.status_code == 200
        assert (await client.get("/api/config")).status_code == 200
        assert (await client.post("/api/auth/logout", json={})).status_code == 403
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert (await client.post("/api/auth/logout", json={})).status_code == 200
        assert (await client.get("/api/config")).status_code == 401


@pytest.mark.parametrize("username", ["", " admin", "admin ", "a" * 65])
def test_invalid_username_configuration(monkeypatch: pytest.MonkeyPatch, username: str) -> None:
    monkeypatch.setenv("APP_ADMIN_USERNAME", username)
    with pytest.raises(ValueError, match="APP_ADMIN_USERNAME"):
        Settings.from_env()
