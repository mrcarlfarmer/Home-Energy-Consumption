import asyncio
import logging
import math
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from zoneinfo import ZoneInfoNotFoundError

import aiosqlite
import uvicorn
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.analytics.service import Analytics, Rebuilding, bounds
from app.api_models import AuthState, Connectivity, History, PublicConfig, Snapshot, Summary
from app.auth import COOKIE, Auth, Session, hash_password
from app.db.store import Conflict, Store, one
from app.kraken import Kraken, KrakenError
from app.models import ConfigUpdate, Login, Reading, TestConfig, iso, now_us
from app.poller import Poller
from app.settings import Settings
from app.streaming import Hub

log = logging.getLogger(__name__)


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str, retry: int | None = None) -> None:
        self.status, self.code, self.detail, self.retry = status, code, detail, retry


def problem_response(status: int, code: str, detail: str, retry: int | None = None) -> JSONResponse:
    return JSONResponse(
        {
            "type": f"urn:energy-monitor:{code}",
            "title": code.replace("_", " "),
            "status": status,
            "code": code,
            "detail": detail,
            "request_id": uuid.uuid4().hex,
        },
        status_code=status,
        media_type="application/problem+json",
        headers={"Retry-After": str(retry)} if retry is not None else None,
    )


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            origin = headers.get(b"origin", b"").decode("latin1")
            if origin not in self.settings.allowed_origins:
                await problem_response(403, "origin_denied", "Request origin is not trusted")(
                    scope, receive, send
                )
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 16384:
                    await problem_response(413, "body_too_large", "Request body exceeds 16 KiB")(
                        scope, receive, send
                    )
                    return
                if not message.get("more_body", False):
                    break
            original_receive = receive
            consumed = False

            async def buffered() -> Message:
                nonlocal consumed
                if consumed:
                    return await original_receive()
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            receive = buffered

        async def secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                additions = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                    ),
                ]
                if self.settings.secure:
                    additions.append((b"strict-transport-security", b"max-age=31536000"))
                if scope["path"].startswith("/api/"):
                    additions.append((b"cache-control", b"no-store"))
                elif scope["path"].startswith("/assets/"):
                    additions.append((b"cache-control", b"public, max-age=31536000, immutable"))
                message["headers"] = list(message.get("headers", [])) + additions
            if scope["path"] == "/api/telemetry/live" and message["type"] == "http.response.body":
                async with asyncio.timeout(10):
                    await send(message)
            else:
                await send(message)

        await self.app(scope, receive, secured)


def create_app(settings: Settings, client: Kraken | None = None) -> FastAPI:
    store, hub = Store(settings.database), Hub()
    kraken = client or Kraken()
    poller, analytics = Poller(store, kraken, hub), Analytics(store)
    analysis_lock, test_lock = asyncio.Lock(), asyncio.Lock()
    next_test = 0.0

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await store.open()
        tasks: list[asyncio.Task[None]] = []
        closing = False
        try:
            encoded = await store.password_hash()
            if encoded is None:
                if settings.password_file is None:
                    raise RuntimeError("First startup requires APP_ADMIN_PASSWORD_FILE")
                password = settings.password_file.read_text(encoding="utf-8").rstrip("\r\n")
                encoded = await asyncio.to_thread(hash_password, password)
                await store.set_password(encoded)
            app.state.auth = Auth(store, encoded)
            app.state.fatal = False
            await poller.publish()

            def finished(task: asyncio.Task[None]) -> None:
                if closing or task.cancelled():
                    return
                error = task.exception()
                log.critical("Background worker stopped unexpectedly: %s", type(error).__name__)
                app.state.fatal = True
                callback = getattr(app.state, "stop_server", None)
                if callback:
                    callback()

            if settings.start_workers:
                tasks = [
                    asyncio.create_task(poller.run()),
                    asyncio.create_task(store.maintenance()),
                    asyncio.create_task(poller.health_timer()),
                    asyncio.create_task(app.state.auth.watch_password()),
                ]
                for task in tasks:
                    task.add_done_callback(finished)
            yield
        finally:
            closing = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await kraken.close()
            await store.close()

    app = FastAPI(
        title="Home Energy Monitor",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.poller, app.state.hub = store, poller, hub
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.exception_handler(Problem)
    async def problem_handler(request: Request, exc: Problem) -> JSONResponse:
        return problem_response(exc.status, exc.code, exc.detail, exc.retry)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic errors include raw inputs; never echo credential/password values.
        return problem_response(
            422, "validation_error", "; ".join(str(e["msg"]) for e in exc.errors())
        )

    @app.exception_handler(aiosqlite.Error)
    async def storage_handler(request: Request, exc: aiosqlite.Error) -> JSONResponse:
        log.error("Database request failed: %s", type(exc).__name__)
        return problem_response(
            503, "storage_unavailable", "Database operation failed; inspect server logs"
        )

    async def require(request: Request) -> Session:
        auth: Auth = app.state.auth
        session = auth.session(request.cookies.get(COOKIE))
        if session is None:
            raise Problem(401, "session_required", "Sign in to continue")
        if request.method not in ("GET", "HEAD"):
            import secrets

            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf):
                raise Problem(403, "csrf_denied", "Refresh the page and retry")
        return session

    SessionDep = Annotated[Session, Depends(require)]

    @app.post("/api/auth/login", response_model=AuthState)
    async def login(data: Login, request: Request, response: Response) -> dict[str, Any]:
        address = request.client.host if request.client else "unknown"
        if not app.state.auth.allowed(address):
            raise Problem(429, "login_throttled", "Too many login attempts", 60)
        result = await app.state.auth.login(data.password.get_secret_value())
        if result is None:
            raise Problem(401, "invalid_credentials", "Invalid password")
        token, session = result
        response.set_cookie(
            COOKIE,
            token,
            max_age=12 * 3600,
            httponly=True,
            secure=settings.secure,
            samesite="strict",
        )
        return {"authenticated": True, "csrf_token": session.csrf}

    @app.get("/api/auth/session", response_model=AuthState)
    async def session_info(session: SessionDep) -> dict[str, Any]:
        return {"authenticated": True, "csrf_token": session.csrf}

    @app.post("/api/auth/logout")
    async def logout(response: Response, session: SessionDep) -> dict[str, bool]:
        app.state.auth.sessions.pop(session.digest, None)
        response.delete_cookie(COOKIE, secure=settings.secure, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/config", response_model=PublicConfig)
    async def configuration(session: SessionDep) -> dict[str, Any]:
        return await store.public_config()

    @app.post("/api/config", response_model=PublicConfig)
    async def configure(data: ConfigUpdate, session: SessionDep) -> dict[str, Any]:
        try:
            await store.save_config(data)
        except Conflict as exc:
            raise Problem(409, "config_conflict", str(exc)) from exc
        except ValueError as exc:
            raise Problem(422, "invalid_config", str(exc)) from exc
        await poller.reconfigure()
        return await store.public_config()

    @app.post("/api/config/test", response_model=Connectivity)
    async def test_config(data: TestConfig, session: SessionDep) -> dict[str, Any]:
        nonlocal next_test
        if test_lock.locked():
            raise Problem(429, "test_busy", "A connectivity test is already running", 30)
        async with test_lock:
            remaining = next_test - asyncio.get_running_loop().time()
            if remaining > 0:
                raise Problem(
                    429,
                    "test_throttled",
                    "Please wait before another connectivity test",
                    math.ceil(remaining),
                )
            saved = await store.config()
            key = data.api_key.get_secret_value() if data.api_key else saved["octopus_api_key"]
            account = (
                data.account_number
                if "account_number" in data.model_fields_set
                else saved["account_number"]
            )
            device = (
                data.active_device_id
                if "active_device_id" in data.model_fields_set
                else saved["active_device_id"]
            )
            if not key or not account:
                raise Problem(422, "missing_credentials", "An API key and account are required")
            start = asyncio.get_running_loop().time()
            next_test = start + 45
            try:
                candidates = await kraken.devices(key, account)
                if device and device not in [x["device_id"] for x in candidates]:
                    raise Problem(
                        422, "wrong_device", "Selected device is not in this electricity account"
                    )
                rows = (
                    await kraken.telemetry(key, device, iso(now_us() - 180_000_000), iso(now_us()))
                    if device
                    else []
                )
                try:
                    samples = [Reading.from_kraken(row, now_us()) for row in rows]
                except ValueError as exc:
                    raise Problem(
                        502, "invalid_telemetry", "Octopus returned malformed telemetry"
                    ) from exc
                latest = max(samples, key=lambda row: row.read_at_us) if samples else None
                available = latest is not None and latest.demand_mw is not None
                warnings = [] if available else ["Select a meter or check recent usable telemetry"]
                if latest and now_us() - latest.read_at_us > 135_000_000:
                    warnings.append("The latest source reading is stale")
                if (
                    saved["polling_enabled"]
                    and key == saved["octopus_api_key"]
                    and account == saved["account_number"]
                    and device == saved["active_device_id"]
                ):
                    await poller.reconfigure()
                result = {
                    "ok": bool(device),
                    "authenticated": True,
                    "device_accessible": bool(device),
                    "selection_required": not bool(device),
                    "telemetry_available": available,
                    "latest_read_at": latest.read_at if latest else None,
                    "latency_ms": round((asyncio.get_running_loop().time() - start) * 1000),
                    "devices": candidates,
                    "warnings": warnings,
                }
                return result
            except KrakenError as exc:
                status = 429 if exc.code == "rate_limited" else 503 if exc.retryable else 422
                raise Problem(
                    status,
                    exc.code,
                    "Octopus connectivity test failed",
                    math.ceil(exc.delay) if exc.delay else None,
                ) from exc

    async def subscription(
        request: Request, session: SessionDep
    ) -> AsyncIterator[asyncio.Queue[tuple[str, dict[str, Any]]]]:
        try:
            queue = hub.subscribe()
        except OverflowError as exc:
            raise Problem(429, "stream_limit", str(exc), 30) from exc
        try:
            yield queue
        finally:
            hub.clients.discard(queue)

    @app.get("/api/telemetry/live", response_class=EventSourceResponse)
    async def live(
        request: Request,
        queue: Annotated[asyncio.Queue[tuple[str, dict[str, Any]]], Depends(subscription)],
    ) -> AsyncIterator[ServerSentEvent]:
        while app.state.auth.session(request.cookies.get(COOKIE)) is not None:
            try:
                event_id, snapshot = await asyncio.wait_for(queue.get(), 15)
                if app.state.auth.session(request.cookies.get(COOKIE)) is None:
                    return
                yield ServerSentEvent(
                    event="snapshot",
                    id=event_id,
                    retry=5000,
                    data=Snapshot.model_validate(snapshot),
                )
            except TimeoutError:
                yield ServerSentEvent(comment="session heartbeat")

    async def analytical(operation: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(1):
                await analysis_lock.acquire()
        except TimeoutError as exc:
            raise Problem(503, "analytics_busy", "Another analytical query is running", 2) from exc
        try:
            async with asyncio.timeout(10):
                return await operation()
        except (ValueError, LookupError, ZoneInfoNotFoundError) as exc:
            raise Problem(422, "invalid_range", str(exc)) from exc
        except Rebuilding as exc:
            raise Problem(503, "analytics_rebuilding", str(exc), 5) from exc
        except TimeoutError as exc:
            raise Problem(504, "analytics_timeout", "Try a shorter historical range") from exc
        finally:
            analysis_lock.release()

    @app.get("/api/analytics/summary", response_model=Summary)
    async def summary(
        session: SessionDep,
        start: Annotated[str, Query(alias="from")],
        to: str,
        timezone: str = "Europe/London",
        device_id: str | None = None,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            a, b = bounds(start, to)
            return await analytics.summary(a, b, timezone, device_id)

        return await analytical(operation)

    @app.get("/api/analytics/history", response_model=History)
    async def history(
        session: SessionDep,
        start: Annotated[str, Query(alias="from")],
        to: str,
        interval: str = "auto",
        max_points: int = 1500,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            a, b = bounds(start, to)
            return await analytics.history(a, b, interval, max_points, device_id)

        return await analytical(operation)

    @app.get("/api/openapi.json")
    async def openapi(session: SessionDep) -> dict[str, Any]:
        return app.openapi()

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        if app.state.fatal:
            raise Problem(503, "worker_failed", "A background worker stopped")
        return {"ok": True}

    @app.get("/readyz")
    async def ready() -> dict[str, bool]:
        if app.state.fatal:
            raise Problem(503, "worker_failed", "A background worker stopped")
        async with store.read() as conn:
            await one(conn, "SELECT 1")
        return {"ok": True}

    if (settings.static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.static / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        if not (settings.static / "index.html").is_file():
            raise Problem(
                503, "frontend_not_built", "Build the frontend before starting the server"
            )
        return FileResponse(settings.static / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def run() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    app = create_app(settings)
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
            timeout_keep_alive=5,
            timeout_graceful_shutdown=25,
            access_log=False,
        )
    )
    app.state.stop_server = lambda: setattr(server, "should_exit", True)
    server.run()
    if not server.started or getattr(app.state, "fatal", False):
        raise SystemExit(1)


if __name__ == "__main__":
    run()
