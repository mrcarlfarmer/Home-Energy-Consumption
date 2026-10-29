import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from app.kraken import Kraken, KrakenError, retry_after


async def test_single_flight_refresh_and_epoch_expiry() -> None:
    inputs = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs.append(json.loads(request.content)["variables"]["input"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "obtainKrakenToken": {
                        "token": f"token-{len(inputs)}",
                        "payload": {"exp": time.time() + 3600},
                        "refreshToken": "refresh",
                        "refreshExpiresIn": int(time.time() + 86400),
                    }
                }
            },
        )

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        results = await asyncio.gather(*(client.token("secret") for _ in range(5)))
        assert results == ["token-1"] * 5
        client.expires = time.time() + 30
        await client.token("secret")
        assert inputs == [{"APIKey": "secret"}, {"refreshToken": "refresh"}]
        assert client.refresh_expires > time.time()
    finally:
        await client.close()


async def test_graphql_throttle_and_cooldown() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "extensions": {"errorCode": "KT-GB-4042"},
                    }
                ],
                "data": {"smartMeterTelemetry": []},
            },
        )

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        for _ in range(2):
            with pytest.raises(KrakenError) as exc:
                await client.execute("query{}", {})
            assert exc.value.retryable and exc.value.delay > 0
        assert count == 1
    finally:
        await client.close()


@pytest.mark.parametrize("graphql", [False, True])
async def test_one_authentication_retry(graphql: bool) -> None:
    tokens, queries = 0, 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tokens, queries
        body = json.loads(request.content)
        if "ObtainToken" in body["query"]:
            tokens += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "obtainKrakenToken": {
                            "token": str(tokens),
                            "payload": {"exp": time.time() + 3600},
                        }
                    }
                },
            )
        queries += 1
        if graphql:
            return httpx.Response(
                200,
                json={"errors": [{"extensions": {"errorCode": "KT-CT-1143"}}]},
            )
        return httpx.Response(401)

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(KrakenError) as exc:
            await client.authenticated("secret", "query Test{}", {})
        assert exc.value.code == "token_rejected" and exc.value.auth
        assert (tokens, queries) == (2, 2)
    finally:
        await client.close()


async def test_graphql_invalid_credential_renews_token_and_recovers_telemetry() -> None:
    tokens = 0
    credentials: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tokens
        body = json.loads(request.content)
        if "ObtainToken" in body["query"]:
            tokens += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "obtainKrakenToken": {
                            "token": f"token-{tokens}",
                            "payload": {"exp": time.time() + 3600},
                        }
                    }
                },
            )
        credentials.append(request.headers["Authorization"])
        if len(credentials) == 1:
            return httpx.Response(
                200,
                json={
                    "errors": [
                        {
                            "extensions": {
                                "errorType": "AUTHORIZATION",
                                "errorCode": "KT-CT-1143",
                            }
                        }
                    ],
                    "data": {"smartMeterTelemetry": None},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "smartMeterTelemetry": [{"readAt": "2026-10-29T22:00:00Z", "demand": 4000}]
                }
            },
        )

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await client.token("secret")
        rows = await client.telemetry(
            "secret",
            "test-device",
            "2026-10-29T21:59:00Z",
            "2026-10-29T22:01:00Z",
        )
        assert rows == [{"readAt": "2026-10-29T22:00:00Z", "demand": 4000}]
        assert tokens == 2
        assert credentials == ["JWT token-1", "JWT token-2"]
    finally:
        await client.close()


def test_retry_after_numeric_and_http_date() -> None:
    assert retry_after("900000") == 900000
    assert 55 <= retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=60))) <= 60
    with pytest.raises(KrakenError):
        retry_after("inf")


async def test_short_tokens_are_reused_and_partial_errors_rejected() -> None:
    tokens = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tokens
        if "ObtainToken" in json.loads(request.content)["query"]:
            tokens += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "obtainKrakenToken": {
                            "token": "short",
                            "payload": {"exp": time.time() + 50},
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {"smartMeterTelemetry": [{"demand": 4000}]},
                "errors": [{"extensions": {"errorCode": ["malformed"]}}],
            },
        )

    client = Kraken(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await client.token("secret")
        await client.token("secret")
        assert tokens == 1
        with pytest.raises(KrakenError, match="graphql_error"):
            await client.authenticated("secret", "query Test{}", {})
    finally:
        await client.close()
