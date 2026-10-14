# Bounded storage, retention and recovery

## Upgrade requirement

The shipped Compose and Portainer stacks now require a **dedicated filesystem no larger
than the configured storage budget** for `/data`. They refuse an ordinary directory
on a larger host filesystem. A Docker named volume or bind mount alone is not a quota.
This is an intentional deployment change: prepare storage and migrate the database
before deploying the new image. Never delete the old database to make startup succeed.

The default budget is 2 GiB. It covers the whole dedicated data filesystem, including
SQLite's main database, WAL, SHM and journals. Filesystem capacity is the hard boundary;
application checks cannot enforce a hard cap on WAL growth when readers pin WAL pages.
The filesystem also prevents a forgotten backup inside `/data` from growing outside
the allocation. Keep backups on separate protected storage with their own quota and
finite retention. Container logs remain independently capped by the supplied stacks.

## Prepare a new bounded filesystem on the Pi

Take a consistent backup first, then stop the old container. These commands run on
the **Linux host**, not in Portainer's application console. Choose a new storage root.
The helper refuses an existing image or data directory and allocates the full 2 GiB
up front, so there is no sparse-file growth into the host's remaining free space:

```sh
sudo sh deploy/prepare-storage.sh /srv/home-energy-bounded
findmnt /srv/home-energy-bounded/data
df -B1 /srv/home-energy-bounded/data
```

The helper needs the host's `fallocate`, `mkfs.ext4` and `mount` utilities. It creates
`energy-data.ext4`, mounts it at `data`, and sets ownership to UID/GID 10001.
It requires enough host space to leave at least 1 GiB free after allocation.
Do not run it against the existing storage root. The new ext4 filesystem contains
its normal `lost+found` directory; otherwise leave it free of application data.

Configure a persistent host mount before rebooting. For a path without spaces, an
example `/etc/fstab` entry is:

```text
/srv/home-energy-bounded/energy-data.ext4 /srv/home-energy-bounded/data ext4 loop,nodev,nosuid,noexec 0 0
```

Verify the entry on your host and arrange Docker startup after this mount. If the
mount is absent, the app's bounded-filesystem startup check refuses to use the
ordinary host directory instead. Never disable this check merely to bypass a failed
mount. Monitor host free space separately: other services can still fill the host.

Restore a validated SQLite backup as
`/srv/home-energy-bounded/data/energy.sqlite3`, without unrelated WAL/SHM files, and
set its ownership to 10001:10001 and permissions to 600. Backups contain secrets.
If restoring sessions from a backup, follow the password-reset/revocation procedure
in [operations](operations.md).

For Compose, set `ENERGY_DATA_DIRECTORY=/srv/home-energy-bounded/data`. For either
Portainer stack, set `ENERGY_ROOT=/srv/home-energy-bounded` and copy the existing
private secrets into that root's `secrets` directory (outside the bounded data
filesystem), preserving restrictive permissions.
Start **one** container, confirm history and collection, and retain the old storage
and compatible image until the migration is accepted. No running Pi is changed by
editing this repository.

For an existing database larger than the allocation, do not copy it blindly or
reduce the limit below its size. Use a larger dedicated filesystem and matching
budget for the initial upgrade, let retention finish, then schedule an offline
backup/space-reclamation migration. Deletion normally reuses pages rather than
shrinking the database file.

## Policy settings

Set these environment variables on the container; restarting applies them:

| Variable | Default | Meaning |
|---|---:|---|
| `APP_STORAGE_BUDGET_BYTES` | 2147483648 | Total data allocation, 2 GiB |
| `APP_STORAGE_RESERVE_BYTES` | 268435456 | Keep at least 256 MiB free on the data filesystem |
| `APP_REQUIRE_BOUNDED_FILESYSTEM` | `true` in shipped stacks | Refuse a filesystem larger than the budget |
| `APP_RAW_RETENTION_DAYS` | 30 | Native readings |
| `APP_5M_RETENTION_DAYS` | 396 | Durable five-minute integrals and thresholds |
| `APP_DAILY_RETENTION_DAYS` | 3653 | Daily history, approximately ten years |
| `APP_RECOVERY_LOOKBACK_DAYS` | 1 | Best-effort historical recovery horizon |

Retention must satisfy `1 <= recovery < raw < five-minute < daily` days. The
library/development default for the bounded-filesystem check is false to permit
temporary test databases; the deployment stacks explicitly enable it.
Daily retention expires whole days, so a boundary day may remain slightly longer.
Increasing retention later cannot recover data already deleted.

SQLite's main database is limited to 75% of the budget using `max_page_count`.
Before ingestion, the app checks total database/sidecar size, filesystem free space,
and available/reusable SQLite pages. It pauses collection at 90% total usage, below
the free-space reserve, or with fewer than 128 available/reusable pages. It resumes
only below 80%, above the reserve plus safety headroom, and with at least 256 pages.
This reserves room for bounded transactions and maintenance. These checks supplement,
not replace, the dedicated filesystem's hard limit.

An existing oversized database is not silently truncated. SQLite cannot lower its
page limit below allocated pages; capacity checks pause ingestion until an operator
reclaims space or migrates to a suitable allocation.

## Maintenance and query semantics

A single supervised worker continuously drains small maintenance batches, sleeping
five seconds when caught up. It finalizes at most 24 raw five-minute buckets, one
archived local day and 24 expired daily records per pass. Maintenance continues when
collection is disabled or storage-paused. It never shortens retention under pressure
or deletes unsummarized data. Summary writes, source deletion and per-meter watermarks
are transactional; failure rolls back that batch and is logged/exposed.

Five-minute and daily summaries preserve observed duration, import integral, sample
and invalid counts, sampled peak/time, and all three inverter-threshold integrals.
They are not averages of averages. Unsupported time remains unknown.
Small raw boundary context and the latest reading per meter are retained for
integration and stale-reading status. They are not a substitute for expired history.

The first daily archive freezes the meter's archive timezone. Its 23-/25-hour DST
days remain intact even if display settings change. Archived history must be queried
in that timezone; the dashboard selects it automatically.
Historical range endpoints expand to complete retained buckets/days, with actual
`from`/`to` and explicit warnings in responses. An explicitly requested resolution
finer than retained data returns 422. Trailing 15-minute values cannot be reconstructed
from daily archives and are null with an explanatory warning. Summary queries support
up to 3653 days; chart requests remain bounded to 4000 points.

After raw expiry, archived summaries cannot be rebuilt with a different algorithm
or arbitrary new thresholds. They retain their version. Incompatible archives are
not silently recalculated from an empty source.

Checkpoints attempt WAL truncation every minute; readers may prevent truncation.
Ingestion still pauses under pressure. There is no automatic full `VACUUM`, which
could require a second database-sized allocation. Inspect maintenance errors rather
than relying on restarts to cure full/read-only/corrupt storage.

## Recovery after downtime

Collection is started at service startup, not by login or an SSE connection.
Browser closure, logout and session expiry do not stop it.

Before live collection, the collector persists a recovery cursor/target per meter.
On first setup it attempts the configured horizon; following downtime it starts
from the prior latest sample. Successful live polls and fifteen-minute backfill
chunks alternate through the same rate governor, one telemetry request per configured
poll interval. With pending recovery, live requests are therefore normally two poll
intervals apart. Small overlaps account for ten-second bucket rounding; source
timestamps deduplicate retries. Cursor advancement occurs only after ingestion, so
a crash can repeat a chunk safely. Revision/device changes fence ingestion and
progress updates.

The default one-day horizon reflects this household's observed October 2026
successful ten-second queries at 30 minutes, one hour, six hours and 24 hours old.
It is **not** a vendor retention guarantee or a measured maximum. Seven-day access
and maximum request size/rate have not been established. Raising the horizon is
best effort. The fifteen-minute chunk size is conservative, not a claimed upstream
limit; the client also enforces a 1000-row/response-size safety bound.

Transient network/throttle failures retain the cursor and use existing backoff.
An unretryable historical-only contract error pauses backfill without stopping live
collection. The cursor and safe error code survive restarts; saving settings or a
successful test of saved connectivity resumes it. Authentication failures still
pause the collector as before.
Old-range rejection, empty responses, partial coverage and aged-out work are recorded
explicitly. A chunk's presence does not promise complete coverage. These windows are
reported as unresolved by this recovery attempt, not proof that upstream data can
never become available. Rejected/empty/partial chunks are not retried forever.
No half-hour consumption fallback is fabricated or mixed into demand/peak metrics.
Storage-paused collection makes no recovery requests until capacity is restored.

`GET /api/storage` (authenticated) exposes policy, capacity, retention watermarks,
recovery progress/counts and the most recent 100 unresolved windows, per optional
`device_id`. SSE/dashboard health includes budget/usage, free space, pause reason,
maintenance status and recovery progress. Recovery-window records expire with the
daily retention horizon.
