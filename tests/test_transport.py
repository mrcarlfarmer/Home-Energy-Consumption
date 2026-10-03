from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from app import healthcheck
from app.main import create_app
from app.settings import Settings


@pytest.fixture
def transport_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("APP_TRANSPORT", "APP_TLS_CERT_FILE", "APP_TLS_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    password = tmp_path / "password"
    password.write_text("transport-fixture-password")
    monkeypatch.setenv("APP_DATABASE_PATH", str(tmp_path / "data.sqlite3"))
    monkeypatch.setenv("APP_ADMIN_PASSWORD_FILE", str(password))
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", "https://testserver")


def test_https_remains_required_by_default(
    transport_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="APP_TLS_CERT_FILE"):
        Settings.from_env()
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", "http://testserver")
    with pytest.raises(ValueError, match="must use HTTPS"):
        Settings.from_env()
    monkeypatch.setenv("APP_TRANSPORT", "ftp")
    with pytest.raises(ValueError, match="APP_TRANSPORT must"):
        Settings.from_env()


def test_http_requires_explicit_transport_and_matching_origins(
    transport_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_TRANSPORT", "http")
    with pytest.raises(ValueError, match="must use HTTP"):
        Settings.from_env()
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", "http://testserver:80/")
    settings = Settings.from_env()
    assert not settings.secure
    assert settings.certificate is None
    assert settings.private_key is None
    assert settings.allowed_origins == ("http://testserver",)
    for invalid in (
        "http://testserver/admin",
        "http://user:password@testserver",
        "http://testserver?query=1",
        "http://testserver#fragment",
    ):
        monkeypatch.setenv("APP_ALLOWED_ORIGINS", invalid)
        with pytest.raises(ValueError, match="Allowed origins"):
            Settings.from_env()


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("status", [200, 503])
def test_healthcheck_uses_selected_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secure: bool, status: int
) -> None:
    settings = Settings(
        database=tmp_path / "data.sqlite3",
        secure=secure,
        certificate=tmp_path / "certificate.pem" if secure else None,
    )
    monkeypatch.setattr(Settings, "from_env", lambda: settings)
    raw, tls, context = MagicMock(), MagicMock(), MagicMock()
    raw.__enter__.return_value = raw
    tls.__enter__.return_value = tls
    selected = tls if secure else raw
    selected.makefile.return_value = BytesIO(
        f"HTTP/1.1 {status} Result\r\nContent-Length: 0\r\n\r\n".encode()
    )
    context.verify_flags = 0
    context.wrap_socket.return_value = tls
    tls_factory = MagicMock(return_value=context)
    socket_factory = MagicMock(return_value=raw)
    monkeypatch.setattr(healthcheck.socket, "create_connection", socket_factory)
    monkeypatch.setattr(healthcheck.ssl, "create_default_context", tls_factory)
    if status == 200:
        healthcheck.main()
    else:
        with pytest.raises(SystemExit) as failure:
            healthcheck.main()
        assert failure.value.code == 1
    socket_factory.assert_called_once_with(("127.0.0.1", 8443), timeout=5)
    selected.sendall.assert_called_once()
    assert selected.makefile.return_value.closed
    assert b"Host: localhost\r\n" in selected.sendall.call_args.args[0]
    if secure:
        tls_factory.assert_called_once_with(cafile=str(settings.certificate))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="localhost")
        assert context.verify_flags & healthcheck.ssl.VERIFY_X509_PARTIAL_CHAIN
        raw.sendall.assert_not_called()
    else:
        tls_factory.assert_not_called()
        tls.sendall.assert_not_called()


@pytest.mark.parametrize("transport,port", [("http", 80), ("https", 443)])
async def test_transport_preserves_authentication_csrf_and_cookie_policy(
    transport_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: str,
    port: int,
) -> None:
    monkeypatch.setenv("APP_TRANSPORT", transport)
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", f"{transport}://testserver:{port}")
    if transport == "https":
        for name in ("APP_TLS_CERT_FILE", "APP_TLS_KEY_FILE"):
            path = tmp_path / name
            path.touch()
            monkeypatch.setenv(name, str(path))
    settings = replace(Settings.from_env(), start_workers=False)
    assert settings.secure == (transport == "https")
    app = create_app(settings)
    origin = f"{transport}://testserver"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=origin,
            headers={"Origin": origin},
        ) as client,
    ):
        assert (await client.get("/api/config")).status_code == 401
        assert (
            await client.post("/api/auth/login", json={"password": "incorrect"})
        ).status_code == 401
        response = await client.post(
            "/api/auth/login", json={"password": "transport-fixture-password"}
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert ("Secure" in cookie) == settings.secure
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert ("strict-transport-security" in response.headers) == settings.secure
        assert (await client.get("/api/config")).status_code == 200
        change = {"expected_revision": 1, "poll_interval_seconds": 60}
        assert (await client.post("/api/config", json=change)).status_code == 403
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert (
            await client.post(
                "/api/config", json=change, headers={"Origin": "http://untrusted.test"}
            )
        ).status_code == 403
        assert (await client.post("/api/config", json=change)).status_code == 200
        assert (await client.post("/api/auth/logout", json={})).status_code == 200
        assert (await client.get("/api/config")).status_code == 401
