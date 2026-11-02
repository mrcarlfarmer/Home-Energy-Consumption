import asyncio
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.models import EVInterval, iso, milli, now_us, timestamp_us


class HomeAssistantError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code, self.retryable = code, retryable


def ev_source(config: dict[str, Any], device: str | None = None) -> str:
    identity = [
        config["home_assistant_url"],
        config["home_assistant_entity"],
        device or config["active_device_id"],
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def power(row: dict[str, Any]) -> int | None:
    state = row.get("state")
    if state in ("unknown", "unavailable"):
        return None
    attributes = row.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("unit_of_measurement") not in (
        "W",
        "kW",
    ):
        raise HomeAssistantError("home_assistant_power_unit")
    if not isinstance(state, str):
        raise HomeAssistantError("home_assistant_invalid_power")
    try:
        watts = Decimal(state)
        if attributes["unit_of_measurement"] == "kW":
            watts *= 1000
        if not watts.is_finite() or not 0 <= watts <= 200_000:
            raise ValueError("Invalid power")
        return milli(watts)
    except (ValueError, InvalidOperation) as exc:
        raise HomeAssistantError("home_assistant_invalid_power") from exc


class HomeAssistant:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
        )
        self._template_lock = asyncio.Lock()
        self._template_denied: set[tuple[str, bytes]] = set()

    async def close(self) -> None:
        await self.http.aclose()

    def reset_freshness_access(self) -> None:
        self._template_denied.clear()

    async def get(
        self, config: dict[str, Any], path: str, params: dict[str, str] | None = None
    ) -> Any:
        return await self._request(config, "GET", path, params=params)

    async def _request(
        self,
        config: dict[str, Any],
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
    ) -> Any:
        try:
            async with (
                asyncio.timeout(20),
                self.http.stream(
                    method,
                    f"{config['home_assistant_url']}/api/{path}",
                    headers={"Authorization": f"Bearer {config['home_assistant_token']}"},
                    params=params,
                    json=body,
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise HomeAssistantError("home_assistant_access_denied")
                if response.status_code == 404:
                    raise HomeAssistantError("home_assistant_entity_or_history_missing")
                if response.status_code == 429 or response.status_code >= 500:
                    raise HomeAssistantError("home_assistant_unavailable", retryable=True)
                if response.status_code != 200:
                    raise HomeAssistantError("home_assistant_http_error")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 1_048_576:
                        raise HomeAssistantError("home_assistant_response_too_large")
                    content.extend(chunk)
                try:
                    return json.loads(content)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise HomeAssistantError("home_assistant_invalid_json") from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            raise HomeAssistantError("home_assistant_network_error", retryable=True) from exc

    def _parse_latest(self, row: Any, entity: str) -> dict[str, Any]:
        if not isinstance(row, dict) or row.get("entity_id") != entity:
            raise HomeAssistantError("home_assistant_invalid_state")
        value = power(row)
        try:
            reported = timestamp_us(str(row.get("last_reported", row.get("last_updated", ""))))
        except ValueError as exc:
            raise HomeAssistantError("home_assistant_invalid_timestamp") from exc
        if reported < 0 or reported > now_us() + 60_000_000:
            raise HomeAssistantError("home_assistant_invalid_timestamp")
        return {
            "power_w": value / 1000 if value is not None else None,
            "reported_at": iso(reported),
            "fresh": value is not None and now_us() - reported <= 120_000_000,
        }

    async def latest(self, config: dict[str, Any]) -> dict[str, Any]:
        entity = config["home_assistant_entity"]
        row = await self.get(config, f"states/{entity}")
        latest = self._parse_latest(row, entity)
        if latest["fresh"] or latest["power_w"] is None or "last_reported" not in row:
            return latest

        # HA can cache REST state JSON even as an unchanged state's last_reported advances.
        template = (
            "{% set s = states[" + json.dumps(entity) + "] %}{% if s is none %}{}{% else %}"
            "{{ {'entity_id': s.entity_id, 'state': s.state, "
            "'attributes': {'unit_of_measurement': s.attributes.get('unit_of_measurement')}, "
            "'last_reported': (s.last_reported if s.last_reported is defined "
            "else s.last_updated).isoformat()} | to_json }}{% endif %}"
        )
        identity = (
            config["home_assistant_url"],
            hashlib.sha256(config["home_assistant_token"].encode()).digest(),
        )
        async with self._template_lock:
            if identity in self._template_denied:
                raise HomeAssistantError("home_assistant_freshness_access_denied", retryable=True)
            try:
                row = await self._request(config, "POST", "template", body={"template": template})
            except HomeAssistantError as exc:
                if exc.code != "home_assistant_access_denied":
                    raise
                self._template_denied.add(identity)
                raise HomeAssistantError(
                    "home_assistant_freshness_access_denied", retryable=True
                ) from exc
        return self._parse_latest(row, entity)

    async def history(self, config: dict[str, Any], start: int, end: int) -> list[EVInterval]:
        if not 0 < end - start <= 900_000_000:
            raise ValueError("Home Assistant history requests must be at most 15 minutes")
        data = await self.get(
            config,
            f"history/period/{iso(start)}",
            {
                "filter_entity_id": config["home_assistant_entity"],
                "end_time": iso(end),
                "significant_changes_only": "0",
            },
        )
        if not isinstance(data, list) or len(data) > 1:
            raise HomeAssistantError("home_assistant_invalid_history")
        rows = data[0] if data else []
        if not isinstance(rows, list) or len(rows) > 10_000:
            raise HomeAssistantError("home_assistant_invalid_history")
        events: dict[int, int | None] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("entity_id") != config["home_assistant_entity"]:
                raise HomeAssistantError("home_assistant_invalid_history")
            try:
                at = timestamp_us(str(row.get("last_updated", "")))
            except ValueError as exc:
                raise HomeAssistantError("home_assistant_invalid_timestamp") from exc
            if at < 0 or at > end:
                raise HomeAssistantError("home_assistant_invalid_timestamp")
            events[at] = power(row)
        intervals: list[EVInterval] = []
        left, value = start, None
        for at, next_value in sorted(events.items()):
            if at > left:
                intervals.append(EVInterval(left, at, value))
                left = at
            value = next_value
        if left < end:
            intervals.append(EVInterval(left, end, value))
        return intervals
