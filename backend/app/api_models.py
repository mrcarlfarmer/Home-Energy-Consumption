from typing import Literal

from pydantic import BaseModel, Field


class Device(BaseModel):
    device_id: str
    label: str


class PublicConfig(BaseModel):
    revision: int
    account_number: str | None
    active_device_id: str | None
    has_api_key: bool
    polling_enabled: bool
    poll_interval_seconds: int
    display_timezone: str
    updated_at_us: int
    thresholds_w: list[int]
    devices: list[Device]


class AuthState(BaseModel):
    authenticated: bool
    csrf_token: str


class Connectivity(BaseModel):
    ok: bool
    authenticated: bool
    device_accessible: bool
    selection_required: bool
    telemetry_available: bool
    latest_read_at: str | None
    latency_ms: int
    devices: list[Device]
    warnings: list[str]


class Health(BaseModel):
    polling_state: Literal["stopped", "running", "backoff", "auth_error", "configuration_error"]
    telemetry_state: Literal["missing", "fresh", "stale"]
    storage_state: Literal["ok", "error"]
    last_success_at: str | None
    last_read_at: str | None
    next_attempt_at: str | None
    consecutive_failures: int
    error_code: str | None
    recovery_incomplete: bool
    disk_free_bytes: int | None
    database_bytes: int | None
    low_disk: bool


class LiveReading(BaseModel):
    read_at: str
    demand_w: float | None
    import_demand_w: float | None
    consumption_kwh: float | None
    quality: str


class Snapshot(BaseModel):
    device_id: str | None
    data_version: int
    reading: LiveReading | None
    health: Health


class Method(BaseModel):
    power_basis: str
    source_grouping: str
    integration: str
    max_gap_seconds: int
    algorithm_version: int


class Metrics(BaseModel):
    requested_seconds: float
    observed_seconds: float
    coverage_pct: float
    import_energy_kwh: float | None
    sample_count: int
    invalid_sample_count: int
    sampled_peak_w: float | None
    sampled_peak_at: str | None


class TimeRange(BaseModel):
    from_: str = Field(alias="from")
    to: str


class DailyMetrics(Metrics, TimeRange):
    date: str


class Threshold(BaseModel):
    threshold_w: int
    seconds_above: float
    time_above_pct: float | None
    energy_when_above_kwh: float | None
    excess_energy_kwh: float | None


class Summary(Metrics, TimeRange):
    device_id: str
    data_version: int
    timezone: str
    thresholds: list[Threshold]
    daily_peaks: list[DailyMetrics]
    method: Method
    warnings: list[str]


class HistorySeries(BaseModel):
    bucket_start_epoch_s: list[float]
    bucket_end_epoch_s: list[float]
    mean_import_w: list[float | None]
    sampled_peak_w: list[float | None]
    peak_at_epoch_s: list[float | None]
    rolling_15m_w: list[float | None]
    observed_seconds: list[float]
    coverage_pct: list[float]
    rolling_15m_coverage_pct: list[float]


class History(TimeRange):
    device_id: str
    data_version: int
    interval_seconds: int
    bucket_alignment: str
    rolling_evaluation: str
    series: HistorySeries
    method: Method
    warnings: list[str]
