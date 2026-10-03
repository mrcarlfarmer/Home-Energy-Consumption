import asyncio
import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
from app.auth import COOKIE, SESSION_SECONDS, Auth, hash_password
from app.db.store import MAX_SESSIONS, Store, one
from app.main import create_app
from app.models import now_us
from app.settings import Settings


async def test_session_and_csrf_survive_restart_but_logout_does_not(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("restart-fixture-password")
    settings = Settings(
        database=tmp_path / "energy.sqlite3",
        password_file=password,
        allowed_hosts=("testserver",),
        allowed_origins=("http://testserver",),
        secure=False,
        start_workers=False,
    )
    first = create_app(settings)
    async with (
        first.router.lifespan_context(first),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        response = await client.post(
            "/api/auth/login", json={"password": "restart-fixture-password"}
        )
        assert response.status_code == 200
        assert f"Max-Age={SESSION_SECONDS}" in response.headers["set-cookie"]
        token = response.cookies[COOKIE]
        csrf = response.json()["csrf_token"]
        original = first.state.auth.session(token)
        assert original is not None
        assert SESSION_SECONDS - 2 < original.expires - time.time() <= SESSION_SECONDS
        async with first.state.store.read() as conn:
            row = await one(conn, "SELECT token_digest,csrf_token FROM admin_sessions")
            assert row[0] == hashlib.sha256(token.encode()).hexdigest()
            assert token not in tuple(row)

    second = create_app(settings)
    async with (
        second.router.lifespan_context(second),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second),
            base_url="http://testserver",
            cookies={COOKIE: token},
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        ) as client,
    ):
        response = await client.get("/api/auth/session")
        assert response.status_code == 200
        assert response.json()["csrf_token"] == csrf
        assert second.state.auth.session(token).expires == original.expires
        changed = await client.post(
            "/api/config", json={"expected_revision": 1, "display_timezone": "UTC"}
        )
        assert changed.status_code == 200
        assert (await client.post("/api/auth/logout", json={})).status_code == 200
        assert second.state.auth.session(token) is None

    third = create_app(settings)
    async with (
        third.router.lifespan_context(third),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=third),
            base_url="http://testserver",
            cookies={COOKIE: token},
        ) as client,
    ):
        assert (await client.get("/api/auth/session")).status_code == 401


async def test_absolute_expiry_and_password_reset(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = hash_password("initial-fixture-password")
    await store.set_password(encoded)
    auth = Auth(store, encoded)
    result = await auth.login("initial-fixture-password")
    assert result is not None
    token, session = result
    with monkeypatch.context() as clock:
        clock.setattr("app.auth.time.time", lambda: session.expires - 0.001)
        assert auth.session(token) is session
        clock.setattr("app.auth.time.time", lambda: session.expires)
        assert auth.session(token) is None

    async with store.write() as conn:
        await conn.execute("UPDATE admin_sessions SET expires_at_us=?", (now_us() - 1,))
    restored = Auth(store, encoded)
    await restored.restore_sessions()
    assert restored.session(token) is None
    assert await store.load_sessions() == []
    result = await restored.login("initial-fixture-password")
    assert result is not None
    active_token, _ = result
    changed = hash_password("replacement-fixture-password")
    await store.set_password(changed)
    assert await store.load_sessions() == []
    await restored.refresh_password()
    assert restored.session(active_token) is None
    restarted = Auth(store, changed)
    await restarted.restore_sessions()
    assert restarted.session(active_token) is None
    assert await restarted.login("initial-fixture-password") is None
    assert await restarted.login("replacement-fixture-password") is not None


async def test_bounded_session_eviction_survives_restart(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = hash_password("bounded-fixture-password")
    await store.set_password(encoded)
    monkeypatch.setattr("app.auth.verify_password", lambda password, stored: True)
    auth = Auth(store, encoded)
    tokens = []
    for _ in range(MAX_SESSIONS + 2):
        result = await auth.login("bounded-fixture-password")
        assert result is not None
        tokens.append(result[0])
    assert len(auth.sessions) == MAX_SESSIONS
    assert len(await store.load_sessions()) == MAX_SESSIONS
    restored = Auth(store, encoded)
    await restored.restore_sessions()
    for instance in (auth, restored):
        assert all(instance.session(token) is None for token in tokens[:2])
        assert all(instance.session(token) is not None for token in tokens[2:])


async def test_password_reset_during_login_cannot_create_session(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = hash_password("old-fixture-password")
    replacement = hash_password("new-fixture-password")
    await store.set_password(encoded)
    auth = Auth(store, encoded)
    started, release = threading.Event(), threading.Event()

    def verify(password: str, stored: str) -> bool:
        started.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr("app.auth.verify_password", verify)
    login = asyncio.create_task(auth.login("old-fixture-password"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        await store.set_password(replacement)
    finally:
        release.set()
    assert await login is None
    assert not auth.sessions
    assert await store.load_sessions() == []


async def test_session_migration_preserves_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "version-one.sqlite3"
    migration = (
        Path(__file__).parents[1] / "backend" / "app" / "db" / "migrations" / "001_initial.sql"
    ).read_text()
    encoded = hash_password("migration-fixture-password")
    with sqlite3.connect(path) as conn:
        conn.executescript(migration)
        conn.execute("INSERT INTO admin_auth VALUES(1,?,?)", (encoded, now_us()))
        conn.execute("UPDATE app_config SET revision=7,octopus_api_key='migration-fixture-key'")
    store = Store(path)
    await store.open()
    try:
        assert (await store.config())["revision"] == 7
        assert (await store.config())["octopus_api_key"] == "migration-fixture-key"
        assert await store.password_hash() == encoded
        assert await store.load_sessions() == []
        async with store.read() as conn:
            assert (await one(conn, "PRAGMA user_version"))[0] == 2
            assert (await one(conn, "PRAGMA integrity_check"))[0] == "ok"
    finally:
        await store.close()
