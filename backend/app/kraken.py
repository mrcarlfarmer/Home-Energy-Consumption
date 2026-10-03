import asyncio
import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

URL = "https://api.octopus.energy/v1/graphql/"
TOKEN = """mutation ObtainToken($input: ObtainJSONWebTokenInput!) {
 obtainKrakenToken(input: $input) { token payload refreshToken refreshExpiresIn }
}"""
TELEMETRY = """query HouseholdTelemetry($deviceId:String!,$start:DateTime!,$end:DateTime!,$grouping:TelemetryGrouping!) {
 smartMeterTelemetry(deviceId:$deviceId,start:$start,end:$end,grouping:$grouping) {
  readAt demand consumption export
 }
}"""
DEVICES = """query ElectricityDevices($accountNumber:String!) {
 account(accountNumber:$accountNumber) {
  electricityAgreements(active:true) {
   meterPoint { meters(includeInactive:false) { smartDevices { deviceId } } }
  }
 }
}"""
AUTH_CODES = {"KT-CT-1111", "KT-CT-1112", "KT-CT-1124", "KT-CT-1130"}
RATE_CODES = {"KT-CT-1199", "KT-GB-4042"}


class KrakenError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        auth: bool = False,
        delay: float = 0,
    ) -> None:
        super().__init__(code)
        self.code, self.retryable, self.auth, self.delay = code, retryable, auth, delay


def retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        if math.isfinite(seconds) and 0 <= seconds <= 365 * 86400:
            return seconds
        raise KrakenError("invalid_retry_deadline")
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0, (parsed - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class Kraken:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5, pool=5, write=5),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            follow_redirects=False,
        )
        self.token_lock = asyncio.Lock()
        self.telemetry_lock = asyncio.Lock()
        self.access = ""
        self.refresh: str | None = None
        self.expires = 0.0
        self.refresh_margin = 60.0
        self.refresh_expires = 0.0
        self.owner = ""
        self.next_telemetry = 0.0
        self.cooldown = 0.0
        self.verified: tuple[str, str, str] | None = None

    async def close(self) -> None:
        await self.http.aclose()

    async def execute(
        self,
        query: str,
        variables: dict[str, Any],
        token: str | None = None,
    ) -> dict[str, Any]:
        remaining = self.cooldown - time.monotonic()
        if remaining > 0:
            raise KrakenError("rate_limited", retryable=True, delay=remaining)
        try:
            async with (
                asyncio.timeout(20),
                self.http.stream(
                    "POST",
                    URL,
                    json={"query": query, "variables": variables},
                    headers={"Authorization": f"JWT {token}"} if token else {},
                ) as response,
            ):
                status = response.status_code
                delay = retry_after(response.headers.get("Retry-After"))
                if status == 429 or status >= 500:
                    self.cooldown = time.monotonic() + max(delay, 5)
                    raise KrakenError(
                        "rate_limited" if status == 429 else "upstream_unavailable",
                        retryable=True,
                        delay=max(delay, 5),
                    )
                if status == 401:
                    raise KrakenError("token_rejected", auth=True)
                if status != 200:
                    raise KrakenError(
                        "upstream_access_denied" if status == 403 else "upstream_http_error"
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 1_048_576:
                        raise KrakenError("upstream_response_too_large")
                    content.extend(chunk)
                try:
                    payload = json.loads(content)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise KrakenError("invalid_upstream_json") from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            raise KrakenError("network_unavailable", retryable=True) from exc
        if not isinstance(payload, dict):
            raise KrakenError("invalid_upstream_shape")
        errors = payload.get("errors")
        if errors:
            if not isinstance(errors, list) or not isinstance(errors[0], dict):
                raise KrakenError("invalid_upstream_error")
            extensions = errors[0].get("extensions") or {}
            code = extensions.get("errorCode") if isinstance(extensions, dict) else None
            if not isinstance(code, str):
                code = None
            if code in RATE_CODES:
                self.cooldown = time.monotonic() + max(45, delay)
                raise KrakenError("rate_limited", retryable=True, delay=max(45, delay))
            if code in AUTH_CODES:
                raise KrakenError("token_rejected", auth=True)
            if code == "KT-GB-4043":
                raise KrakenError("upstream_network_error", retryable=True)
            if code == "KT-GB-4051":
                raise KrakenError("range_too_old")
            raise KrakenError(
                f"graphql_{code}"
                if isinstance(code, str) and re.fullmatch(r"KT-[A-Z]{2}-[0-9]{4,5}", code)
                else "graphql_error"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise KrakenError("invalid_upstream_data")
        return data

    async def token(self, key: str, *, invalidate: str | None = None) -> str:
        async with self.token_lock:
            owner = hashlib.sha256(key.encode()).hexdigest()
            if self.owner != owner:
                self.owner, self.access, self.refresh = owner, "", None
                self.expires, self.refresh_expires, self.verified = 0, 0, None
            if invalidate and self.access == invalidate:
                self.access, self.expires = "", 0
            remaining = self.expires - time.time()
            if self.access and remaining > self.refresh_margin:
                return self.access
            use_refresh = bool(self.refresh and self.refresh_expires > time.time() + 20)
            try:
                data = await self.execute(
                    TOKEN,
                    {"input": ({"refreshToken": self.refresh} if use_refresh else {"APIKey": key})},
                )
            except KrakenError as exc:
                if exc.retryable and self.access and remaining > 20:
                    return self.access
                if use_refresh and not exc.retryable:
                    data = await self.execute(TOKEN, {"input": {"APIKey": key}})
                else:
                    raise
            result = data.get("obtainKrakenToken")
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("token"), str)
                or not 1 <= len(result["token"]) <= 16384
            ):
                raise KrakenError("invalid_token_response")
            payload = result.get("payload")
            expiry = payload.get("exp") if isinstance(payload, dict) else None
            if (
                not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
                or expiry <= time.time() + 20
            ):
                raise KrakenError("invalid_token_expiry", auth=True)
            self.access, self.expires = result["token"], float(expiry)
            self.refresh_margin = min(60, max(20, (self.expires - time.time()) * 0.2))
            refresh = result.get("refreshToken")
            if refresh is not None and (not isinstance(refresh, str) or len(refresh) > 16384):
                raise KrakenError("invalid_refresh_token")
            self.refresh = refresh
            refresh_expiry = result.get("refreshExpiresIn")
            self.refresh_expires = (
                float(refresh_expiry) if isinstance(refresh_expiry, (int, float)) else 0
            )
            return self.access

    async def authenticated(
        self,
        key: str,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        token = await self.token(key)
        try:
            return await self.execute(query, variables, token)
        except KrakenError as exc:
            if not exc.auth:
                raise
        token = await self.token(key, invalidate=token)
        return await self.execute(query, variables, token)

    async def devices(self, key: str, account: str) -> list[dict[str, str]]:
        data = await self.authenticated(key, DEVICES, {"accountNumber": account})
        result: set[str] = set()
        try:
            for agreement in data["account"]["electricityAgreements"]:
                for meter in agreement["meterPoint"]["meters"]:
                    for device in meter["smartDevices"]:
                        result.add(str(device["deviceId"]).upper())
        except (TypeError, KeyError) as exc:
            raise KrakenError("device_discovery_contract_error") from exc
        return [{"device_id": value, "label": value} for value in sorted(result)]

    async def verify(self, key: str, account: str, device: str) -> None:
        identity = (hashlib.sha256(key.encode()).hexdigest(), account, device)
        if self.verified != identity:
            candidates = await self.devices(key, account)
            if device not in [x["device_id"] for x in candidates]:
                raise KrakenError("device_not_in_account")
            self.verified = identity

    async def telemetry(
        self,
        key: str,
        device: str,
        start: str,
        end: str,
        interval: int = 45,
    ) -> list[dict[str, object]]:
        async with self.telemetry_lock:
            wait = max(self.next_telemetry, self.cooldown) - time.monotonic()
            if wait > 0:
                raise KrakenError("rate_limited", retryable=True, delay=wait)
            self.next_telemetry = time.monotonic() + max(30, interval)
            data = await self.authenticated(
                key,
                TELEMETRY,
                {
                    "deviceId": device,
                    "start": start,
                    "end": end,
                    "grouping": "TEN_SECONDS",
                },
            )
            rows = data.get("smartMeterTelemetry")
            if rows is None:
                return []
            if (
                not isinstance(rows, list)
                or len(rows) > 1000
                or any(not isinstance(r, dict) for r in rows)
            ):
                raise KrakenError("invalid_telemetry_shape")
            return rows
