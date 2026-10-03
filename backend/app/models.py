import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
BUCKET_US = 300_000_000
GAP_US = 30_000_000
THRESHOLDS = (3680, 5000, 6000)
ALGORITHM_VERSION = 1


def timestamp_us(value: str) -> int:
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ValueError(
            "Expected a timezone-aware RFC3339 timestamp (up to six fractional digits)"
        )
    dt = datetime.fromisoformat(value).astimezone(UTC)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def iso(value: int) -> str:
    return (
        (EPOCH + timedelta(microseconds=value))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def now_us() -> int:
    delta = datetime.now(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def milli(value: object, *, counter: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a measurement")
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite() or abs(decimal) > Decimal("1000000000000"):
            raise ValueError("Nonfinite or out-of-range measurement")
        if counter and decimal < 0:
            raise ValueError("Cumulative counters cannot be negative")
        return int((decimal * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal measurement") from exc


class Reading(BaseModel):
    read_at: str
    source_read_at: str
    read_at_us: int
    demand_mw: int | None
    consumption_mwh: int | None = None
    export_mwh: int | None = None
    quality: Literal["valid", "missing_demand", "invalid_demand"] = "valid"

    @classmethod
    def from_kraken(cls, row: dict[str, object], at: int) -> "Reading":
        source = str(row.get("readAt", ""))
        time = timestamp_us(source)
        if time > at + 60_000_000 or time < 0:
            raise ValueError("Telemetry timestamp is outside the supported time range")
        quality: Literal["valid", "missing_demand", "invalid_demand"] = "valid"
        try:
            demand = milli(row.get("demand"))
        except ValueError:
            demand, quality = None, "invalid_demand"
        if demand is None and quality == "valid":
            quality = "missing_demand"
        return cls(
            read_at=iso(time),
            source_read_at=source,
            read_at_us=time,
            demand_mw=demand,
            consumption_mwh=milli(row.get("consumption"), counter=True),
            export_mwh=milli(row.get("export"), counter=True),
            quality=quality,
        )


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    account_number: str | None = Field(default=None, max_length=64)
    active_device_id: str | None = None
    api_key: SecretStr | None = None
    polling_enabled: bool | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=30, le=3600)
    display_timezone: str | None = None

    @field_validator("active_device_id")
    @classmethod
    def device(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"(?:[0-9A-Fa-f]{2}-){7}[0-9A-Fa-f]{2}", value):
            raise ValueError("Enter the electricity meter EUI-64 device ID")
        return value.upper() if value else value

    @field_validator("api_key")
    @classmethod
    def key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not 1 <= len(value.get_secret_value()) <= 512:
            raise ValueError("API key must be nonempty and at most 512 characters")
        return value

    @field_validator("display_timezone")
    @classmethod
    def timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Unknown IANA timezone") from exc
        return value


class TestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_number: str | None = Field(default=None, min_length=1, max_length=64)
    active_device_id: str | None = None
    api_key: SecretStr | None = None

    @field_validator("active_device_id")
    @classmethod
    def device(cls, value: str | None) -> str | None:
        return ConfigUpdate.device(value)

    @field_validator("api_key")
    @classmethod
    def key(cls, value: SecretStr | None) -> SecretStr | None:
        return ConfigUpdate.key(value)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=1024)
