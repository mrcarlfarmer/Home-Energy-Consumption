# API

The application exposes its current generated schema at `GET /api/openapi.json`, requiring authentication by default. JSON request/response models are implemented with Pydantic; units appear in field names.

## Authentication

Both supplied Portainer stacks enable login for the whole dashboard/API. `APP_ADMIN_USERNAME` defaults to `admin` and accepts 1-64 characters without outer whitespace. Login requires both username and password. Incorrect credentials share the same response and password verification work; invalid usernames do not create sessions. The username is a deployment setting, not a second user account system.

`APP_AUTH_REQUIRED` defaults to `true` and accepts only `true` or `false`. It is a deployment-only setting. Explicit `false` makes configuration, analytics, OpenAPI and SSE accessible without login; **anyone who can reach the app can read data and change settings**. It creates no password hash or authentication cookie, skips password bootstrap and the password watcher, and revokes existing persisted sessions on startup without deleting the saved password hash or application data.

In password-free mode, `GET /api/auth/session` returns `authenticated:false`, `authentication_required:false` and a process-local CSRF token. The frontend opens the dashboard directly and hides Sign out. Send the token as `X-CSRF-Token` with mutations; trusted Origin/Host checks, request limits, redaction and upstream authentication still apply. The CSRF token is not an authentication credential and is available to anyone who can reach this endpoint. It rotates on restart; reload the page before retrying a stale-token rejection. Login and logout return 409 `authentication_disabled`.

The remaining authentication/session behavior below applies when login is enabled. Successful login/session responses include `authentication_required:true`.

`POST /api/auth/login` accepts `{"username":"admin","password":"..."}` and returns a CSRF token while setting an HttpOnly, SameSite=Strict session cookie. HTTPS is the default and adds the Secure flag and HSTS. Explicit `APP_TRANSPORT=http` omits those HTTPS-only protections; the browser connection is then unencrypted. The request must have a configured `Origin` matching the selected transport. Default ports 80/443 are normalized to the browser's port-free Origin form.

`GET /api/auth/session` returns the current CSRF token. Send it as `X-CSRF-Token` with all subsequent mutations. `POST /api/auth/logout` revokes the session; associated SSE streams close at their next session check (within 15 seconds).

Dashboard sessions survive routine application/container restarts and expire 12 hours after sign-in, without sliding renewal. Logout and password reset revoke persisted sessions. Upgrading from the original memory-only session store requires one new sign-in.

## Configuration

| Endpoint | Contract |
|---|---|
| `GET /api/config` | Redacted configuration, revision, `has_api_key`, device labels and fixed thresholds |
| `POST /api/config` | Partial update with mandatory `expected_revision`; returns new redacted configuration |
| `POST /api/config/test` | Optional draft key/account/device; absent fields use saved settings, no configuration/readings are saved |
| `POST /api/home-assistant/test` | Optional draft `home_assistant_url`, `home_assistant_token`, `home_assistant_entity`; checks current power and recent history without saving |

Writable settings: `api_key`, `account_number`, `active_device_id`, `polling_enabled`, `poll_interval_seconds` (30-3,600), `display_timezone` (IANA), `tariff_pricing_enabled` (default false). Omitted key preserves it, nonempty string replaces it, null clears it only with polling and tariff pricing disabled. Optimistic revision conflicts return 409. Saving syntactically valid settings does not silently initiate a connectivity test.

Optional EV settings are `home_assistant_url` (HTTP(S), no URL credentials/query/
fragment), `home_assistant_entity` (`sensor.*`, power in W or kW),
`home_assistant_token`, and `home_assistant_enabled`. Enabled EV collection
requires URL, token, entity and an active meter. Omitted token preserves it;
nonempty string replaces it; null clears it only with EV collection disabled.
`GET /api/config` returns URL/entity/enabled and `has_home_assistant_token`, never
the token itself. Changing URL, entity or meter selects a separate EV history
identity; token rotation does not discard that history.

Optional `ev_savings_from` is a validated `YYYY-MM-DD` date or null (default).
Setting it confirms all Ohme charging from that Europe/London date uses
Intelligent Octopus Go Prioritise Savings, never Max/Boost or charging beyond
the allowance. It requires a configured power URL/entity and meter. Null disables
bonus inference. Account/meter or EV URL/entity changes clear it unless it is
explicitly supplied again in the update. It does not enable collection or pricing.

The Home Assistant test returns `power_w`, `reported_at`, `fresh`,
`history_available` and `warnings`. It shares the local 45-second connectivity
test cooldown. Only read-only requests are sent to the configured host, redirects
are refused and TLS verification is enabled. Upstream authorization failure is
422, not a local-session 401. The current sensor must report within two minutes
for background collection to extend recent coverage. When Home Assistant's
cached REST timestamp is stale, a read-only `POST /api/template` retrieves the
power and timestamp together from the current state; Home Assistant requires an
administrator token for this fallback. A denied freshness check is reported as
`home_assistant_freshness_access_denied`, and no coverage is inferred. To avoid
repeated failed logins, the app does not retry the denied template request until
settings are saved; it continues checking the normal state and can recover when
that timestamp becomes fresh. Older Home Assistant versions without
`last_reported` continue to use `last_updated`.

The test discovers all accessible electricity meter candidates. With no selected device it returns `selection_required:true`; with an accessible but empty meter it returns `telemetry_available:false`. It shares the upstream rate governor and can return 429 with a retry time.

The test endpoint also enforces a 45-second local cooldown before any upstream authentication/discovery calls, preventing repeated clicks from exhausting those fields' quotas.

A successful test of the already saved account/key/device resumes an error-paused collector only if polling was already enabled. Draft tests never replace saved settings or enable disabled collection. Malformed telemetry is rejected; stale source readings are reported as warnings.

## Historical gap recovery

`POST /api/recovery` accepts `expected_revision`, `device_id`, RFC3339 `from`
and `to`, and returns 202 with `device_id`, actual queued `from`/`to`, and
`state:"pending"`. It requires the authenticated session/CSRF/Origin protections
and saved, enabled polling for the active meter. It does not change configuration,
start polling or use credential drafts.

Requests are limited to retained raw history and at most 31 days back. A boundary
within one minute of the retention cutoff is clipped to it to accommodate request
latency. The newest three minutes are excluded to allow for telemetry publication.
Pending/paused work is merged with the requested range rather than lost.
Revision conflicts return 409, disabled polling/unsupported ranges/meters return
422, storage pressure returns 507, and concurrent/repeated requests return 429
with `Retry-After` (60-second local cooldown).

The existing collector processes the persistent cursor, skipping fully supported
fifteen-minute windows without upstream calls. Missing windows use the existing
shared telemetry governor and alternate with live collection. SSE health and
`GET /api/storage` expose progress, pauses and unavailable windows. Successful
repairs reconcile old gap records; incomplete/empty/aged-out responses remain
explicitly unresolved. This retries meter history only, not Home Assistant history.

## Tariff pricing

`GET /api/tariff` returns enabled/state/error status, fetch time, cached range,
current tariff name/code/schedule, household rate intervals covering the next
24 hours, and estimate warnings. Each rate has RFC3339 `from`/`to`, `band` and
`pence_per_kwh` including VAT. Credentials are never returned.

Optional paired `from`/`to` parameters request cached intervals overlapping a
historical graph range instead (at most 3653 days). Optional `device_id` prevents
the active tariff from being applied to another meter; a mismatch has no rates.
These reads do not fetch upstream data or enable pricing. The graph shades
returned household `ECO7_NIGHT` and separately styled estimated `EV_BONUS`
intervals, never inferring uncached prices.

When `ev_savings_from` is set, the shared effective-rate resolver overlays whole
settlement half-hours earned by recorded positive charger power, under that
explicit assumption. Complete known-zero EV coverage retains an ordinary daytime
rate. Unknown daytime eligibility is omitted from `rates`, never silently
represented by the published day price. Other tariffs and dates before activation
keep their published schedules.

Additional response fields are `adjustment_enabled`, `pricing_revision`,
`ev_coverage_pct` (nullable valid-EV-duration percentage over the relevant tariff
span), `charging` and `unresolved`. The last two are arrays of RFC3339 `from`/`to`
intervals clipped to the query. `charging` contains actual observed events;
`EV_BONUS` rate intervals contain their awarded household periods. A charge
outside the query can establish its rate because the full settlement half-hour
is examined. `unresolved` includes missing EV evidence and inferred bonuses
without dated night prices. Neither future plans nor a known-zero partial slot
guarantee the eventual peak price. Warnings explain assumptions and missing data.

`POST /api/tariff/refresh` accepts `{"expected_revision": N}` and uses only saved
Octopus credentials, account and meter. Fetching does not enable pricing or meter
polling. It returns the same status shape. Authentication, CSRF and trusted-Origin
checks apply. Refreshes are serialized and have a local 60-second cooldown.
Conflicting configuration returns 409; invalid/unsupported tariffs return 422;
busy/throttled requests return 429; retryable upstream failures return 503;
exhausted storage returns 507. Safe error codes identify unsupported schedules,
payment-method-specific feeds, missing rates and upstream errors.

Enabled pricing refreshes every six hours independently of meter collection.
It caches dated household rates over `min(raw_retention_days,31)` days back and
two days ahead, including underlying dated night prices for bonus overlays.
Fetch again after upgrading an older cache to obtain these underlying prices.
Historical agreements are respected. Cache replacement is atomic
and revision-fenced; failed refreshes preserve the previous cache for intervals
it actually covers. Account/meter changes invalidate it; key rotation does not.

## Live stream

`GET /api/telemetry/live` serves native SSE with `snapshot` events, a process-local `id`, five-second retry instruction and heartbeat comments.

Each snapshot contains:

- `device_id`, `data_version`.
- Nullable `reading`: native `read_at`, signed `demand_w`, nonnegative `import_demand_w`, cumulative `consumption_kwh`, quality.
- `health`: collector state, native telemetry freshness, storage state, latest successful API contact, latest reading time, next retry, consecutive failures and sanitized error code.
- Recovery health additionally exposes `recovery_state` (`idle`, `pending`,
  `paused` or `complete`), `recovery_active` (a historical worker request is
  currently in flight), cursor/target, `recovery_last_progress_at` (the latest
  completed or locally skipped chunk), unresolved-window count and recovery
  error. Completion/progress persist across restarts; active work is process-local.
  A complete pass can still have unresolved windows. Storage recovery health
  contains the persistent fields; live snapshots also include `recovery_active`.
- `home_assistant`: independent EV collector state, `power_w`, `reported_at`,
  latest success/retry, sanitized `error_code` and optional backfill cursor.

Reconnect always gets the latest complete snapshot, even when a `Last-Event-ID` is supplied. Missed event replay is deliberately not promised. There are at most four clients, one pending snapshot per client and bounded network-send time. Use history for catch-up.

## Analytics

Both endpoints accept `from` and `to` as timezone-aware RFC3339 strings, forming
`[from,to)`, at most 3653 days. Optional `device_id` selects one stored meter;
omission selects the active meter. No cross-meter aggregation occurs.

Both endpoints accept `exclude_ev=true` (default false). This subtracts the
configured Ohme power history before energy/threshold calculations and uses
joint meter/EV coverage. Missing EV data does not fall back to grid-only values.
It is available only for the configured active meter, ranges no longer than raw
retention, and boundaries not older than the raw retention watermark. Unsupported
ranges return 422 with an explicit message. The unadjusted endpoint retains all
existing archive behavior.

Adjusted responses use `method.power_basis=nonnegative_grid_import_excluding_ev`
and `integration=meter_previous_sample_hold_minus_recorded_ev_state_hold`.
Daily metrics, chart means, derived peaks and trailing demand all use this basis.
The unchanged live reading remains grid import. Responses warn about recorded-
state estimation and unreported Home Assistant outages.

`GET /api/analytics/summary` additionally accepts `timezone` (default `Europe/London`). It returns observed/requested duration, coverage, estimated import kWh, sampled peak/time, local daily peaks and a single 6 kW threshold:

```json
{
  "threshold_w": 6000,
  "seconds_above": 120,
  "time_above_pct": 100,
  "energy_when_above_kwh": 0.266666666667,
  "excess_energy_kwh": 0.066666666667
}
```

This example represents two fully observed minutes at 8 kW. Null estimates mean insufficient observed duration; a supported zero remains zero. `thresholds` and the configuration's `thresholds_w` remain arrays, each containing only the 6 kW entry.

`GET /api/analytics/daily-peaks` accepts `to`, `timezone`, optional `device_id`
and `exclude_ev`, using the same authentication and analytical query lane.
It returns `device_id`, actual `from`/`to`, `timezone`, five `daily_peaks` rows
with the existing daily-metrics shape plus a per-row `pricing` estimate, and
`warnings`. These are the five calendar
dates ending at the exclusive `to`; a midnight end includes the preceding day,
not an empty next day. The latest date can be partial. Archived grid resolution
may expand it to a whole day. Missing/expired EV history yields unavailable days
without falling back to whole-meter peaks. The graph displays these rows newest
first independently of its selected duration.

The summary also contains `pricing`: `enabled`, `available`,
`energy_when_above_gbp`, `excess_energy_gbp`, `price_coverage_pct`,
`priced_seconds_above`, `fetched_at`, `warning` and nullable `pricing_revision`. Monetary values are GBP,
using household import rates including VAT and the selected grid/EV-excluded
basis. Both are null unless every observed above-threshold interval is priced.
No observed duration also yields null. Observed periods without exceedance can
legitimately cost zero. Price coverage is above-threshold duration coverage, not
overall meter/EV coverage. Unknown bonus eligibility counts as missing applicable
pricing, not an assumed peak rate. `pricing_revision` combines the configuration
revision, meter/EV data version and tariff fetch timestamp; where present it
matches `/api/tariff` for the same snapshot. The browser retries mismatched
cost/history/indicator revisions. Standing charges and account-specific discounts
are excluded; supplier-specific EV billing adjustments are not independently
verified. Exceeding 6 kW is not itself a penalty.

Each `/api/analytics/daily-peaks` row's `pricing` has the same fields and
availability rules as the summary estimate, calculated independently over that
row's actual calendar-day bounds. It prices both all energy while demand exceeds
6 kW and only the excess beyond 6 kW, not a daily 6 kWh allowance. A missing rate
on one day does not suppress another day's fully priced estimate. Disabled
pricing, inactive meters, missing observations and insufficient retained detail
retain explicit per-row explanations.

`GET /api/analytics/history` additionally accepts `interval=auto|1m|5m|15m|30m|1h|6h|1d` and `max_points` (default 1,500, maximum 4,000). An explicit interval that cannot fit the output bound returns 422, rather than silently changing it.

History returns equal-length columnar arrays: bucket start/end Unix seconds, observed-time mean W, sampled peak W/time, trailing 15-minute W, observed duration, bucket coverage and rolling-window coverage. Missing values are null. Recent buckets are UTC aligned; fixed `1d` chart buckets are distinct from local-calendar daily peaks.

Expired raw data requires at least five-minute resolution. Daily archives require
`auto`/`1d` and the frozen archive calendar timezone for summaries. Responses expand
endpoints to retained boundaries, report actual `from`/`to`, and include warnings.
Daily chart buckets follow the archive timezone and may be 23/25 hours; inspect
`bucket_alignment` and actual bucket endpoints. Trailing 15-minute demand in daily
archives is null (rolling coverage is zero) and carries an unavailability warning.

`GET /api/storage` accepts optional `device_id` and returns deployment policy,
capacity including WAL/SHM/journals, retention watermarks, recovery progress and up
to 100 unresolved historical windows (timestamps in microseconds). SSE health also
exposes `storage_budget_bytes`, `storage_paused`, maintenance status, recovery
cursor/target/counts, and separate last-attempt/success/store times. Collection
can report `polling_state=storage_paused` and `storage_state=paused`.

The detailed [methodology](analytics-methodology.md) defines integrations, clipping, gaps and rolling evaluation times.

## Errors and limits

Errors use `application/problem+json`, a safe code/detail, status and request ID. Validation errors never echo raw credential inputs.

401 means local session failure; upstream invalid credentials in connectivity tests use 422 so they do not log the dashboard user out. Other relevant statuses are 403 (CSRF/Origin), 409 (revision), 413 (16 KiB request body limit), 422 (invalid settings/range), 429 (rate/stream/login limits), 503 (storage, upstream, analytics busy/rebuilding) and 504 (query deadline).

`Retry-After` accompanies retryable throttling/busy responses. The analytical lane permits one expensive query at a time with a one-second bounded wait and ten-second deadline. Ordinary API data is non-cacheable; hashed static assets are immutable. `GET /healthz` checks worker/process liveness; `GET /readyz` checks storage readiness independently of Kraken connectivity.
