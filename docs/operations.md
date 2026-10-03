# Operations

## Collection and troubleshooting

Polls use a three-minute overlap and at most one hour of recent recovery after an outage. All rows are deduplicated by device/native timestamp. Older missing intervals remain visible and are not replaced by zero or fabricated historical data.

Token refresh is single-flight and proactive. A rejected token gets one renewal/operation retry. Persistent auth/configuration failures stop automatic retries until settings change or a successful test of the saved credentials resumes already-enabled polling. HTTP and GraphQL throttling share cooldowns with connectivity tests. Transient failures use bounded exponential backoff/jitter and respect vendor retry timing.

Interpret health indicators separately:

- **Dashboard connected:** the browser has an SSE connection to the Pi.
- **Collector running/backoff/stopped:** collection scheduling state.
- **Fresh/stale/missing:** age/validity of the native meter reading, not merely HTTP success.
- **Storage error:** a database write/read could not complete.

A Home Mini can lose meter/Wi-Fi connectivity while Kraken HTTP remains reachable. A Pi Wi-Fi loss can interrupt collection while the local database remains healthy. Restore networking at the host/router; the container does not need privileged network controls.

Inspect bounded logs with `docker compose logs --tail 100 energy`. Logs contain counts, timings and safe codes, not API keys/passwords or upstream response bodies. An unexpected worker failure stops the process; Compose restarts it. Credentials/configuration failures remain visible without an authentication storm.

## Password reset

```sh
docker compose exec energy energy-admin password-reset
```

Enter the new password interactively. The hash and persisted-session revocation are updated together in SQLite; running sessions are revoked within 15 seconds. Ordinary restarts preserve dashboard sessions, but clear the upstream token cache.

Dashboard sessions expire 12 hours after sign-in; activity and restarts do not extend that deadline. Only token digests, CSRF metadata and absolute expiry times are stored, with at most 32 sessions in SQLite and memory. Signing out removes the persisted session. The first upgrade from memory-only sessions requires one new sign-in.

## Backup

Backups contain the Octopus API key, administrator hash and session metadata. Store them with private permissions on protected storage.

Use SQLite's backup API, not a plain copy of an actively WAL-backed database. For a resource-bounded, quiesced backup:

```sh
docker compose stop energy
docker compose run --rm --no-deps energy energy-admin backup --output /data/backups/energy-2026-10-03.sqlite3
docker compose start energy
```

The command refuses to overwrite an existing file and runs a `quick_check` on the resulting snapshot. Choose a new filename for each backup. Move/copy the completed backup to a separate protected disk; a backup on the same physical device does not protect against device failure. The collection pause is a real gap and will only recover within Kraken's available lookback.

An online `docker compose exec ... backup` is supported by SQLite, but it adds another Python process to the container memory budget. Measure headroom first; the quiesced procedure is the safe default.

## Restore and upgrade

Stop collection and the container. Preserve the current database and any associated WAL/SHM files together before a recovery attempt; do not discard an active WAL. Validate the chosen backup with SQLite `PRAGMA integrity_check`.

Restore into a stopped, correctly owned data directory/volume with no stale WAL/SHM from a different database. Mount the target volume in a one-off administrative container if necessary; never run two collectors against it. A backup may contain sessions that were subsequently signed out, so run `energy-admin password-reset` against the restored database before starting the service to revoke those sessions. Then confirm configuration, latest readings, history and health.

Schema migrations are transactional. An image refuses an unknown newer database schema. Before upgrading, take a consistent backup and preserve the current image tag/digest. Roll back a schema-changing upgrade only with the compatible backup/image pair.

Do not use `docker compose down -v` for ordinary troubleshooting: it deletes the persistent volume.

## Rollups and disk space

Raw history is never automatically deleted. A 365-day stream can contain 3.15 million raw rows plus 105,120 five-minute base rollups and three threshold rows per bucket. Measure actual bytes on your data and leave capacity for growth, backups and WAL/checkpoint activity.

The periodic maintenance worker rebuilds dirty buckets and checkpoints passively. Long analytical reads have deadlines so they cannot pin WAL indefinitely. The journal-size limit is not a strict cap on a currently active WAL.

To regenerate caches explicitly, prefer an offline maintenance window:

```sh
docker compose stop energy
docker compose run --rm --no-deps energy energy-admin rebuild
docker compose start energy
```

If storage fills or becomes read-only/corrupt, the application reports an error; it does not delete raw data or recreate the database. Check free disk and volume permissions before restarting. Repair/restore using a protected copy, not an in-place destructive experiment.

## Memory and performance checks

`tests/performance/seed_history.py` creates a **new synthetic database only**, refusing to overwrite an existing one. Run it with the application stopped and a disposable volume; never substitute a household database path.

`tests/performance/workload.py` logs into an HTTPS deployment, opens four real SSE streams and requests 1-, 30- and 365-day summary/history responses while checking response bounds and arithmetic invariants. Run this client outside the container and collect:

- Container `memory.current`, `memory.peak`, swap/limit settings and OOM events.
- Database/index/WAL sizes.
- Cold/warm response durations and image/OS/Pi/storage identification.

Do not substitute Python heap size or Docker's cache-subtracted summary for total cgroup memory. Browser/host/Docker-daemon memory is outside the app container's target. Native Pi and real-meter acceptance gates are recorded separately in the validation document.

Use `--require-ingestion` to require an observed data-version change while the workload is active. For a network-free test, `serve_mock.py` runs the actual background tasks against a synthetic Kraken transport, guarded to accept only the disposable `A-SYNTHETIC` database. It is test tooling, not a production entry point.
