# Operations

## Collection and troubleshooting

Live polls request a recent three-minute window. Persistent, rate-governed backfill
attempts fifteen-minute historical chunks within the configured lookback (one day
by default), alternating with live requests. All rows are deduplicated by
device/native timestamp. Unresolved/partial intervals remain visible and are not
replaced by zero or fabricated historical data.
See [storage management](storage-management.md) for pause/recovery semantics.

Token refresh is single-flight and proactive. A rejected token gets one renewal/operation retry. Persistent auth/configuration failures stop automatic retries until settings change or a successful test of the saved credentials resumes already-enabled polling. HTTP and GraphQL throttling share cooldowns with connectivity tests. Transient failures use bounded exponential backoff/jitter and respect vendor retry timing.

Kraken's GraphQL `KT-CT-1143` (invalid Authorization credential) is treated as a
rejected access token, including when returned with HTTP 200: renew once and retry
the operation, rather than stopping immediately as a configuration error. On older
images showing `graphql_KT-CT-1143`, test the saved credentials in Settings to resume
already-enabled collection. Confirm that the last measurement timestamp advances;
a displayed demand value alone can be a stale stored reading. Select a wider history
range to distinguish missing recent collection from missing stored history.

Interpret health indicators separately:

- **Dashboard connected:** the browser has an SSE connection to the Pi.
- **Collector running/backoff/stopped:** collection scheduling state.
- **Fresh/stale/missing:** age/validity of the native meter reading, not merely HTTP success.
- **Storage error:** a database write/read could not complete.

A Home Mini can lose meter/Wi-Fi connectivity while Kraken HTTP remains reachable. A Pi Wi-Fi loss can interrupt collection while the local database remains healthy. Restore networking at the host/router; the container does not need privileged network controls.

Inspect bounded logs with `docker compose logs --tail 100 energy`. Logs contain counts, timings and safe codes, not API keys/passwords or upstream response bodies. An unexpected worker failure stops the process; Compose restarts it. Credentials/configuration failures remain visible without an authentication storm.

## Password reset

Both shipped Portainer stacks require username/password login. The username defaults to `admin`; set `ENERGY_ADMIN_USERNAME` in Portainer to override it. Changing the bootstrap file does not replace a stored password hash. Reset the stored password without deleting the database or Octopus configuration:

```sh
docker compose exec energy energy-admin password-reset
```

For the Portainer container (without needing its Compose checkout), use:

```sh
docker exec -it home-energy energy-admin password-reset
```

Keep the private bootstrap file consistent with the new password for future fresh database initialization. Never put it in Git, image layers or environment variables. Changing the username alone does not revoke existing sessions.

If upgrading an explicitly password-free installation, prepare the password file before pulling the new stack; see the [HTTP upgrade guide](portainer-http-installation.md#backups-and-later-changes). The optional `APP_AUTH_REQUIRED=false` mode still exposes data/settings to anyone who can reach the service. Resetting a password alone does not enable login: recreate the container with authentication enabled.

Enter the new password interactively. The hash and persisted-session revocation are updated together in SQLite; running sessions are revoked within 15 seconds. Ordinary restarts preserve dashboard sessions, but clear the upstream token cache.

Dashboard sessions expire 12 hours after sign-in; activity and restarts do not extend that deadline. Only token digests, CSRF metadata and absolute expiry times are stored, with at most 32 sessions in SQLite and memory. Signing out removes the persisted session. The first upgrade from memory-only sessions requires one new sign-in.

## Backup

Backups contain the Octopus API key, administrator hash and session metadata. Store them with private permissions on protected storage.

Use SQLite's backup API, not a plain copy of an actively WAL-backed database. For a resource-bounded, quiesced backup:

```sh
# Provision this on separate protected, capacity-limited backup storage.
sudo install -d -m 700 -o 10001 -g 10001 /srv/home-energy-backups
docker compose stop energy
docker compose run --rm --no-deps -v /srv/home-energy-backups:/backup energy energy-admin backup --output /backup/energy-2026-10-03.sqlite3
docker compose start energy
```

The command refuses to overwrite an existing file and runs a `quick_check` on the resulting snapshot. Choose a new filename for each backup. Enforce a separate backup quota/finite rotation; do not put full-size backups inside the bounded data filesystem. Move/copy completed backups to a separate protected disk; a backup on the same physical device does not protect against device failure. The collection pause is a real gap and will only recover within Kraken's available lookback.

An online `docker compose exec ... backup` is supported by SQLite, but it adds another Python process to the container memory budget. Measure headroom first; the quiesced procedure is the safe default.

## Restore and upgrade

Stop collection and the container. Preserve the current database and any associated WAL/SHM files together before a recovery attempt; do not discard an active WAL. Validate the chosen backup with SQLite `PRAGMA integrity_check`.

Restore into a stopped, correctly owned data directory/volume with no stale WAL/SHM from a different database. Mount the target volume in a one-off administrative container if necessary; never run two collectors against it. A backup may contain sessions that were subsequently signed out, so run `energy-admin password-reset` against the restored database before starting a password-protected service to revoke those sessions. Password-free startup revokes restored sessions automatically and does not need a password reset. Then confirm configuration, latest readings, history and health.

Schema migrations are transactional. An image refuses an unknown newer database schema. Before upgrading, take a consistent backup and preserve the current image tag/digest. Roll back a schema-changing upgrade only with the compatible backup/image pair.

Do not use `docker compose down -v` for ordinary troubleshooting: it deletes the persistent volume.

## Rollups and disk space

Raw history is automatically summarized before expiry: defaults are 30 days raw,
396 days five-minute and 3653 days daily. The shipped stacks require a dedicated
bounded filesystem. Follow [storage preparation and migration](storage-management.md)
before upgrading an existing deployment.

The periodic maintenance worker rebuilds dirty buckets, performs transactional
retention and attempts WAL truncation. Long analytical reads have deadlines.
The journal-size limit is not a strict cap on a currently active WAL; the dedicated
filesystem is the hard storage boundary. Capacity checks pause ingestion before it
is exhausted, without deleting unsummarized data.

To regenerate caches explicitly, prefer an offline maintenance window:

```sh
docker compose stop energy
docker compose run --rm --no-deps energy energy-admin rebuild
docker compose start energy
```

If storage fills or becomes read-only/corrupt, the application reports an error;
it does not delete unsummarized data or recreate the database. Check free disk,
maintenance health and volume permissions before restarting. Repair/restore using
a protected copy, not an in-place destructive experiment.

## Memory and performance checks

`tests/performance/seed_history.py` creates a **new synthetic database only**, refusing to overwrite an existing one. Run it with the application stopped and a disposable volume; never substitute a household database path.

`tests/performance/workload.py` logs into an HTTPS deployment, opens four real SSE streams and requests 1-, 30- and 365-day summary/history responses while checking response bounds and arithmetic invariants. Run this client outside the container and collect:

- Container `memory.current`, `memory.peak`, swap/limit settings and OOM events.
- Database/index/WAL sizes.
- Cold/warm response durations and image/OS/Pi/storage identification.

Do not substitute Python heap size or Docker's cache-subtracted summary for total cgroup memory. Browser/host/Docker-daemon memory is outside the app container's target. Native Pi and real-meter acceptance gates are recorded separately in the validation document.

Use `--require-ingestion` to require an observed data-version change while the workload is active. For a network-free test, `serve_mock.py` runs the actual background tasks against a synthetic Kraken transport, guarded to accept only the disposable `A-SYNTHETIC` database. It is test tooling, not a production entry point.
