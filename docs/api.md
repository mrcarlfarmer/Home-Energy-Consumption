# API

The application exposes its current generated schema to authenticated users at `GET /api/openapi.json`. JSON request/response models are implemented with Pydantic; units appear in field names.

## Authentication

`POST /api/auth/login` accepts `{"password":"..."}` and returns a CSRF token while setting an HttpOnly, Secure, SameSite=Strict session cookie. The request must have a configured HTTPS `Origin`.

`GET /api/auth/session` returns the current CSRF token. Send it as `X-CSRF-Token` with all subsequent mutations. `POST /api/auth/logout` revokes the session; associated SSE streams close at their next session check (within 15 seconds).

## Configuration

| Endpoint | Contract |
|---|---|
| `GET /api/config` | Redacted configuration, revision, `has_api_key`, device labels and fixed thresholds |
| `POST /api/config` | Partial update with mandatory `expected_revision`; returns new redacted configuration |
| `POST /api/config/test` | Optional draft key/account/device; absent fields use saved settings, no configuration/readings are saved |

Writable settings: `api_key`, `account_number`, `active_device_id`, `polling_enabled`, `poll_interval_seconds` (30-3,600), `display_timezone` (IANA). Omitted key preserves it, nonempty string replaces it, null clears it only with polling disabled. Optimistic revision conflicts return 409. Saving syntactically valid settings does not silently initiate a connectivity test.

The test discovers all accessible electricity meter candidates. With no selected device it returns `selection_required:true`; with an accessible but empty meter it returns `telemetry_available:false`. It shares the upstream rate governor and can return 429 with a retry time.

The test endpoint also enforces a 45-second local cooldown before any upstream authentication/discovery calls, preventing repeated clicks from exhausting those fields' quotas.

A successful test of the already saved account/key/device resumes an error-paused collector only if polling was already enabled. Draft tests never replace saved settings or enable disabled collection. Malformed telemetry is rejected; stale source readings are reported as warnings.

## Live stream

`GET /api/telemetry/live` serves native SSE with `snapshot` events, a process-local `id`, five-second retry instruction and heartbeat comments.

Each snapshot contains:

- `device_id`, `data_version`.
- Nullable `reading`: native `read_at`, signed `demand_w`, nonnegative `import_demand_w`, cumulative `consumption_kwh`, quality.
- `health`: collector state, native telemetry freshness, storage state, latest successful API contact, latest reading time, next retry, consecutive failures and sanitized error code.

Reconnect always gets the latest complete snapshot, even when a `Last-Event-ID` is supplied. Missed event replay is deliberately not promised. There are at most four clients, one pending snapshot per client and bounded network-send time. Use history for catch-up.

## Analytics

Both endpoints accept `from` and `to` as timezone-aware RFC3339 strings, forming `[from,to)`, at most 366 days. Optional `device_id` selects one stored meter; omission selects the active meter. No cross-meter aggregation occurs.

`GET /api/analytics/summary` additionally accepts `timezone` (default `Europe/London`). It returns observed/requested duration, coverage, estimated import kWh, sampled peak/time, local daily peaks and exactly three thresholds:

```json
{
  "threshold_w": 3680,
  "seconds_above": 120,
  "time_above_pct": 100,
  "energy_when_above_kwh": 0.133333333333,
  "excess_energy_kwh": 0.010666666667
}
```

This example represents two fully observed minutes at 4 kW. Null estimates mean insufficient observed duration; a supported zero remains zero.

`GET /api/analytics/history` additionally accepts `interval=auto|1m|5m|15m|30m|1h|6h|1d` and `max_points` (default 1,500, maximum 2,000). An explicit interval that cannot fit the output bound returns 422, rather than silently changing it.

History returns equal-length columnar arrays: bucket start/end Unix seconds, observed-time mean W, sampled peak W/time, trailing 15-minute W, observed duration, bucket coverage and rolling-window coverage. Missing values are null. Buckets are UTC aligned; fixed `1d` chart buckets are distinct from local-calendar daily peaks.

The detailed [methodology](analytics-methodology.md) defines integrations, clipping, gaps and rolling evaluation times.

## Errors and limits

Errors use `application/problem+json`, a safe code/detail, status and request ID. Validation errors never echo raw credential inputs.

401 means local session failure; upstream invalid credentials in connectivity tests use 422 so they do not log the dashboard user out. Other relevant statuses are 403 (CSRF/Origin), 409 (revision), 413 (16 KiB request body limit), 422 (invalid settings/range), 429 (rate/stream/login limits), 503 (storage, upstream, analytics busy/rebuilding) and 504 (query deadline).

`Retry-After` accompanies retryable throttling/busy responses. The analytical lane permits one expensive query at a time with a one-second bounded wait and ten-second deadline. Ordinary API data is non-cacheable; hashed static assets are immutable. `GET /healthz` checks worker/process liveness; `GET /readyz` checks storage readiness independently of Kraken connectivity.
