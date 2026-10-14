import asyncio
import json
import logging
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.analytics.intervals import Metrics, integrate
from app.models import (
    ALGORITHM_VERSION,
    BUCKET_US,
    GAP_US,
    ConfigUpdate,
    Reading,
    iso,
    now_us,
    timestamp_us,
)
from app.storage import DAY_US, StoragePolicy, StoragePressure

log = logging.getLogger(__name__)
MAX_SESSIONS = 32


class Conflict(Exception):
    pass


async def one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()) -> aiosqlite.Row:
    async with conn.execute(sql, args) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise LookupError("Database record not found")
    return row


class Store:
    def __init__(self, path: Path, policy: StoragePolicy | None = None) -> None:
        self.path = path
        self.policy = policy or StoragePolicy.from_env()
        self.storage_paused = False
        self.maintenance_error: str | None = None
        self.last_maintenance_at: str | None = None
        self.lock = asyncio.Lock()
        self.readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=2)
        self.connections: list[aiosqlite.Connection] = []
        self.writer: aiosqlite.Connection

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (
            self.policy.require_bounded_filesystem
            and shutil.disk_usage(self.path.parent).total > self.policy.budget_bytes
        ):
            raise RuntimeError(
                "Database must be on a dedicated filesystem no larger than APP_STORAGE_BUDGET_BYTES; "
                "see docs/storage-management.md. Refusing unbounded storage."
            )
        try:
            for index in range(3):
                conn = await aiosqlite.connect(self.path, isolation_level=None)
                self.connections.append(conn)
                conn.row_factory = aiosqlite.Row
                mode = await one(conn, "PRAGMA journal_mode=WAL")
                if mode[0] != "wal":
                    raise RuntimeError("SQLite WAL is required on a local writable filesystem")
                await conn.executescript(
                    "PRAGMA foreign_keys=ON; PRAGMA busy_timeout=2500;"
                    "PRAGMA synchronous=FULL; PRAGMA temp_store=FILE;"
                    "PRAGMA mmap_size=0; PRAGMA wal_autocheckpoint=1000;"
                    "PRAGMA journal_size_limit=16777216;"
                    f"PRAGMA cache_size={-4096 if index == 0 else -2048};"
                )
                if index == 0:
                    self.writer = conn
                    page_size = int((await one(conn, "PRAGMA page_size"))[0])
                    await conn.execute(
                        f"PRAGMA max_page_count={self.policy.budget_bytes * 3 // 4 // page_size}"
                    )
                    self.page_limit = self.policy.budget_bytes * 3 // 4 // page_size
                    version = (await one(conn, "PRAGMA user_version"))[0]
                    migrations = ("001_initial.sql", "002_sessions.sql", "003_retention.sql")
                    if version > len(migrations):
                        raise RuntimeError("Database schema is newer than this application")
                    for filename in migrations[version:]:
                        migration = (Path(__file__).parent / "migrations" / filename).read_text()
                        await conn.executescript("BEGIN IMMEDIATE;\n" + migration + "\nCOMMIT;")
                else:
                    await conn.execute("PRAGMA query_only=ON")
                    self.readers.put_nowait(conn)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        for conn in self.connections:
            await conn.close()
        self.connections.clear()
        self.readers = asyncio.Queue(maxsize=2)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with asyncio.timeout(12):
            conn = await self.readers.get()
        deadline = time.monotonic() + 10
        try:
            await conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            await conn.execute("BEGIN")
            yield conn
        finally:
            await conn.interrupt()
            await conn.set_progress_handler(lambda: 0, 0)
            await conn.rollback()
            self.readers.put_nowait(conn)

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.lock:
            await self.writer.execute("BEGIN IMMEDIATE")
            try:
                yield self.writer
                await self.writer.commit()
            except BaseException:
                await self.writer.rollback()
                raise

    async def config(self) -> dict[str, Any]:
        async with self.read() as conn:
            return dict(await one(conn, "SELECT * FROM app_config WHERE singleton=1"))

    async def public_config(self) -> dict[str, Any]:
        async with self.read() as conn:
            result = dict(await one(conn, "SELECT * FROM app_config WHERE singleton=1"))
            result["has_api_key"] = bool(result.pop("octopus_api_key"))
            result.pop("singleton")
            result["polling_enabled"] = bool(result["polling_enabled"])
            result["thresholds_w"] = [3680, 5000, 6000]
            async with conn.execute(
                "SELECT device_id,label FROM devices ORDER BY created_at_us"
            ) as cur:
                result["devices"] = [dict(row) for row in await cur.fetchall()]
            return result

    async def save_config(self, update: ConfigUpdate) -> None:
        async with self.write() as conn:
            config = dict(await one(conn, "SELECT * FROM app_config WHERE singleton=1"))
            if config["revision"] != update.expected_revision:
                raise Conflict("Settings changed; reload before saving")
            for field in update.model_fields_set - {"expected_revision"}:
                value = getattr(update, field)
                if field == "api_key":
                    config["octopus_api_key"] = value.get_secret_value() if value else None
                else:
                    if value is None and field in {
                        "polling_enabled",
                        "poll_interval_seconds",
                        "display_timezone",
                    }:
                        raise ValueError(f"{field} cannot be null")
                    config[field] = value
            if config["polling_enabled"] and not all(
                config[x] for x in ("octopus_api_key", "account_number", "active_device_id")
            ):
                raise ValueError("An API key, account and device are required to enable polling")
            if config["active_device_id"]:
                if not config["account_number"]:
                    raise ValueError("An account is required for the selected device")
                await conn.execute(
                    "INSERT INTO devices(device_id,account_number,label,created_at_us) VALUES(?,?,?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET account_number=excluded.account_number",
                    (
                        config["active_device_id"],
                        config["account_number"],
                        config["active_device_id"],
                        now_us(),
                    ),
                )
            await conn.execute(
                "UPDATE app_config SET revision=revision+1,account_number=?,active_device_id=?,"
                "octopus_api_key=?,polling_enabled=?,poll_interval_seconds=?,display_timezone=?,"
                "updated_at_us=? WHERE singleton=1",
                tuple(
                    config[x]
                    for x in (
                        "account_number",
                        "active_device_id",
                        "octopus_api_key",
                        "polling_enabled",
                        "poll_interval_seconds",
                        "display_timezone",
                    )
                )
                + (now_us(),),
            )

    async def password_hash(self) -> str | None:
        async with self.read() as conn, conn.execute("SELECT password_hash FROM admin_auth") as cur:
            row = await cur.fetchone()
            return str(row[0]) if row else None

    async def set_password(self, value: str) -> None:
        async with self.write() as conn:
            await conn.execute(
                "INSERT INTO admin_auth VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "password_hash=excluded.password_hash,updated_at_us=excluded.updated_at_us",
                (value, now_us()),
            )
            await conn.execute("DELETE FROM admin_sessions")

    @staticmethod
    async def prune_sessions(conn: aiosqlite.Connection) -> None:
        await conn.execute("DELETE FROM admin_sessions WHERE expires_at_us<=?", (now_us(),))
        await conn.execute(
            "DELETE FROM admin_sessions WHERE id NOT IN "
            "(SELECT id FROM admin_sessions ORDER BY id DESC LIMIT ?)",
            (MAX_SESSIONS,),
        )

    async def load_sessions(self) -> list[tuple[str, str, int]]:
        async with self.write() as conn:
            await self.prune_sessions(conn)
            async with conn.execute(
                "SELECT token_digest,csrf_token,expires_at_us FROM admin_sessions ORDER BY id"
            ) as cursor:
                return [(str(row[0]), str(row[1]), int(row[2])) for row in await cursor.fetchall()]

    async def save_session(
        self, digest: str, csrf: str, expires_at_us: int, password_hash: str
    ) -> bool:
        async with self.write() as conn:
            current = await one(conn, "SELECT password_hash FROM admin_auth WHERE singleton=1")
            if current[0] != password_hash:
                return False
            await conn.execute(
                "INSERT INTO admin_sessions(token_digest,csrf_token,expires_at_us) VALUES(?,?,?)",
                (digest, csrf, expires_at_us),
            )
            await self.prune_sessions(conn)
            return True

    async def delete_session(self, digest: str) -> None:
        async with self.write() as conn:
            await conn.execute("DELETE FROM admin_sessions WHERE token_digest=?", (digest,))

    async def clear_sessions(self) -> None:
        async with self.write() as conn:
            await conn.execute("DELETE FROM admin_sessions")

    async def latest(self, device: str | None) -> dict[str, Any] | None:
        if device is None:
            return None
        async with (
            self.read() as conn,
            conn.execute(
                "SELECT * FROM readings WHERE device_id=? ORDER BY read_at_us DESC LIMIT 1",
                (device,),
            ) as cur,
        ):
            row = await cur.fetchone()
            return dict(row) if row else None

    async def capacity(self) -> dict[str, Any]:
        def measure() -> tuple[int, int]:
            size = 0
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    size += Path(str(self.path) + suffix).stat().st_size
                except FileNotFoundError:
                    continue
            return size, shutil.disk_usage(self.path.parent).free

        size, free = await asyncio.to_thread(measure)
        async with self.read() as conn:
            pages = int((await one(conn, "PRAGMA page_count"))[0])
            reusable = int((await one(conn, "PRAGMA freelist_count"))[0])
        page_headroom = self.page_limit - pages + reusable
        # Reserve room for a bounded ingest transaction, WAL and safe recovery.
        stop = (
            size >= self.policy.budget_bytes * 9 // 10
            or free < self.policy.reserve_bytes
            or page_headroom < 128
        )
        resume = (
            size < self.policy.budget_bytes * 8 // 10
            and free
            >= self.policy.reserve_bytes + min(16 * 1024**2, self.policy.budget_bytes // 20)
            and page_headroom >= 256
        )
        if stop:
            self.storage_paused = True
        elif resume:
            self.storage_paused = False
        return {
            "database_bytes": size,
            "disk_free_bytes": free,
            "storage_budget_bytes": self.policy.budget_bytes,
            "low_disk": free < self.policy.reserve_bytes,
            "storage_paused": self.storage_paused,
            "maintenance_error": self.maintenance_error,
            "last_maintenance_at": self.last_maintenance_at,
        }

    async def retention(self, conn: aiosqlite.Connection, device: str) -> dict[str, Any]:
        async with conn.execute(
            "SELECT * FROM retention_state WHERE device_id=?", (device,)
        ) as cur:
            row = await cur.fetchone()
        return (
            dict(row)
            if row
            else {
                "raw_before_us": 0,
                "five_before_us": 0,
                "expired_before_us": 0,
                "archive_timezone": None,
            }
        )

    async def plan_recovery(self, device: str, at: int, latest: int | None) -> None:
        target = at - 180_000_000
        horizon = at - self.policy.recovery_days * DAY_US
        async with self.write() as conn:
            async with conn.execute(
                "SELECT * FROM recovery_state WHERE device_id=?", (device,)
            ) as cur:
                old = await cur.fetchone()
            if old and old["status"] == "paused":
                return
            if old and old["status"] == "pending":
                cursor = int(old["cursor_us"])
                target = int(old["target_us"])
            elif latest is None:
                cursor = int(old["cursor_us"]) if old else horizon
            elif latest < target:
                cursor = max(latest, int(old["cursor_us"]) if old else latest)
            else:
                return
            if cursor < horizon:
                await conn.execute(
                    "INSERT OR IGNORE INTO recovery_gaps VALUES(?,?,?,'lookback_limit')",
                    (device, cursor, min(horizon, target)),
                )
                cursor = min(horizon, target)
            await conn.execute(
                "INSERT INTO recovery_state(device_id,cursor_us,target_us,status) VALUES(?,?,?,'pending') "
                "ON CONFLICT(device_id) DO UPDATE SET cursor_us=excluded.cursor_us,"
                "target_us=excluded.target_us,status='pending'",
                (device, cursor, target),
            )

    async def recovery(self, device: str) -> dict[str, Any] | None:
        async with (
            self.read() as conn,
            conn.execute("SELECT * FROM recovery_state WHERE device_id=?", (device,)) as cur,
        ):
            row = await cur.fetchone()
            return dict(row) if row else None

    async def pause_recovery(self, device: str, revision: int, code: str) -> None:
        async with self.write() as conn:
            await conn.execute(
                "UPDATE recovery_state SET status='paused',error_code=? WHERE device_id=? "
                "AND EXISTS(SELECT 1 FROM app_config WHERE revision=? AND active_device_id=?)",
                (code, device, revision, device),
            )

    async def resume_recovery(self) -> None:
        async with self.write() as conn:
            await conn.execute(
                "UPDATE recovery_state SET status='pending',error_code=NULL WHERE status='paused' "
                "AND device_id=(SELECT active_device_id FROM app_config)",
            )

    async def finish_recovery_chunk(
        self,
        device: str,
        start: int,
        end: int,
        revision: int,
        reason: str | None,
    ) -> None:
        async with self.write() as conn:
            config = await one(conn, "SELECT * FROM app_config")
            if (
                config["revision"] != revision
                or not config["polling_enabled"]
                or config["active_device_id"] != device
            ):
                return
            if reason:
                await conn.execute(
                    "INSERT OR REPLACE INTO recovery_gaps VALUES(?,?,?,?)",
                    (device, start, end, reason),
                )
            await conn.execute(
                "UPDATE recovery_state SET cursor_us=?,status=CASE WHEN ?>=target_us "
                "THEN 'complete' ELSE 'pending' END,last_attempt_us=?,"
                "unavailable_before_us=CASE WHEN ?='range_too_old' THEN ? ELSE unavailable_before_us END "
                "WHERE device_id=? AND cursor_us=?",
                (end, end, now_us(), reason, end, device, start),
            )

    async def recovery_health(self, device: str | None) -> dict[str, Any]:
        async with self.read() as conn:
            async with conn.execute(
                "SELECT * FROM recovery_state WHERE device_id=?", (device,)
            ) as cur:
                row = await cur.fetchone()
            gaps = (
                await one(
                    conn,
                    "SELECT count(*) FROM recovery_gaps WHERE device_id=?",
                    (device,),
                )
            )[0]
        return {
            "recovery_pending": bool(row and row["status"] == "pending"),
            "recovery_cursor_at": iso(row["cursor_us"]) if row else None,
            "recovery_target_at": iso(row["target_us"]) if row else None,
            "recovery_gap_count": int(gaps),
            "recovery_error": row["error_code"] if row else None,
            "recovery_incomplete": bool(gaps or (row and row["status"] != "complete")),
        }

    async def ingest(self, device: str, readings: list[Reading], revision: int) -> int:
        if (await self.capacity())["storage_paused"]:
            raise StoragePressure("Collection paused: storage limit or free-space reserve reached")
        if len(readings) > 1000:
            raise ValueError("Ingestion exceeds bounded transaction size")
        changes = 0
        async with self.write() as conn:
            config = await one(conn, "SELECT * FROM app_config WHERE singleton=1")
            if config["revision"] != revision or not config["polling_enabled"]:
                return 0
            if config["active_device_id"] != device:
                return 0
            retained = await self.retention(conn, device)
            for reading in sorted(readings, key=lambda r: r.read_at_us):
                if reading.read_at_us < retained["raw_before_us"]:
                    continue
                values = (
                    device,
                    reading.read_at,
                    reading.source_read_at,
                    reading.read_at_us,
                    reading.demand_mw,
                    reading.consumption_mwh,
                    reading.export_mwh,
                    reading.quality,
                    now_us(),
                    now_us(),
                )
                async with conn.execute(
                    "INSERT INTO readings VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(device_id,read_at) DO UPDATE SET "
                    "demand_mw=excluded.demand_mw,consumption_mwh=excluded.consumption_mwh,"
                    "export_mwh=excluded.export_mwh,quality=excluded.quality,"
                    "source_read_at=excluded.source_read_at,updated_at_us=excluded.updated_at_us "
                    "WHERE readings.demand_mw IS NOT excluded.demand_mw "
                    "OR readings.consumption_mwh IS NOT excluded.consumption_mwh "
                    "OR readings.export_mwh IS NOT excluded.export_mwh "
                    "OR readings.quality IS NOT excluded.quality",
                    values,
                ) as cur:
                    changed = cur.rowcount
                if changed:
                    changes += 1
                    # A supported segment cannot extend beyond GAP_US either side of this sample.
                    left = max(
                        retained["raw_before_us"],
                        (reading.read_at_us - GAP_US) // BUCKET_US * BUCKET_US,
                    )
                    right = (reading.read_at_us + GAP_US) // BUCKET_US * BUCKET_US
                    await conn.executemany(
                        "INSERT OR IGNORE INTO dirty_rollup_buckets VALUES(?,?)",
                        [(device, t) for t in range(max(0, left), right + 1, BUCKET_US)],
                    )
            if changes:
                await conn.execute(
                    "UPDATE devices SET data_version=data_version+1 WHERE device_id=?", (device,)
                )
        return changes

    @staticmethod
    async def raw_rows(
        conn: aiosqlite.Connection, device: str, start: int, end: int
    ) -> list[tuple[int, int | None]]:
        if end - start > 172_800_000_000:
            raise ValueError("Raw query exceeds bounded high-resolution window")
        result: list[tuple[int, int | None]] = []
        async with conn.execute(
            "SELECT read_at_us,demand_mw FROM readings WHERE device_id=? AND read_at_us>=? "
            "AND read_at_us<=? ORDER BY read_at_us",
            (device, start - GAP_US, end + GAP_US),
        ) as cur:
            while rows := await cur.fetchmany(256):
                result.extend((int(row[0]), row[1]) for row in rows)
                if len(result) > 50_000:
                    raise ValueError("Unexpected source density exceeds raw query budget")
        return result

    @staticmethod
    async def raw_metrics(conn: aiosqlite.Connection, device: str, start: int, end: int) -> Metrics:
        return integrate(await Store.raw_rows(conn, device, start, end), start, end)

    async def rebuild(self, limit: int = 24) -> int:
        count = 0
        for _ in range(limit):
            async with self.write() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO dirty_rollup_buckets SELECT device_id,bucket_start_us "
                    "FROM rollup_5m r WHERE algorithm_version<>? AND bucket_start_us >= "
                    "COALESCE((SELECT raw_before_us FROM retention_state s WHERE s.device_id=r.device_id),0) LIMIT 24",
                    (ALGORITHM_VERSION,),
                )
                async with conn.execute(
                    "SELECT * FROM dirty_rollup_buckets ORDER BY bucket_start_us DESC LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    break
                device, start = str(row[0]), int(row[1])
                retained = await self.retention(conn, device)
                if start < retained["raw_before_us"]:
                    raise RuntimeError("Refusing to rebuild an archive without raw readings")
                await self.rebuild_bucket(conn, device, start)
                count += 1
            await asyncio.sleep(0)
        return count

    async def rebuild_bucket(self, conn: aiosqlite.Connection, device: str, start: int) -> None:
        m = await self.raw_metrics(conn, device, start, start + BUCKET_US)
        await conn.execute(
            "INSERT OR REPLACE INTO rollup_5m VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                device,
                start,
                ALGORITHM_VERSION,
                m.observed_us,
                m.energy_ws,
                m.samples,
                m.invalid,
                m.peak_mw,
                m.peak_at,
                now_us(),
            ),
        )
        await conn.executemany(
            "INSERT INTO rollup_threshold_5m VALUES(?,?,?,?,?,?)",
            [
                (device, start, t, m.above[j], m.load[j], m.excess[j])
                for j, t in enumerate((3680, 5000, 6000))
            ],
        )
        await conn.execute(
            "DELETE FROM dirty_rollup_buckets WHERE device_id=? AND bucket_start_us=?",
            (device, start),
        )

    @staticmethod
    def rollup_metrics(row: aiosqlite.Row) -> Metrics:
        return Metrics(
            observed_us=row["observed_us"],
            energy_ws=row["import_watt_seconds"],
            samples=row["sample_count"],
            invalid=row["invalid_sample_count"],
            peak_mw=row["peak_import_mw"],
            peak_at=row["peak_at_us"],
            above=[row[f"a{j}"] for j in range(3)],
            load=[row[f"l{j}"] for j in range(3)],
            excess=[row[f"e{j}"] for j in range(3)],
        )

    async def compact(self, limit: int = 24, at: int | None = None) -> int:
        at = at if at is not None else now_us()
        raw_cutoff = (at - self.policy.raw_days * DAY_US) // BUCKET_US * BUCKET_US
        count = 0
        for _ in range(limit):
            async with self.write() as conn:
                # Ignore the small boundary context kept for the first retained bucket.
                async with conn.execute(
                    "SELECT d.device_id,(SELECT read_at_us FROM readings r WHERE r.device_id=d.device_id "
                    "AND r.read_at_us>=COALESCE(s.raw_before_us,0) AND r.read_at_us<? "
                    "ORDER BY r.read_at_us LIMIT 1)/?*? AS bucket FROM devices d "
                    "LEFT JOIN retention_state s ON s.device_id=d.device_id "
                    "WHERE bucket IS NOT NULL ORDER BY bucket LIMIT 1",
                    (raw_cutoff, BUCKET_US, BUCKET_US),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    # Empty/outage time still ages once old dirty buckets are finalized.
                    async with conn.execute(
                        "SELECT d.device_id,d.bucket_start_us FROM dirty_rollup_buckets d "
                        "LEFT JOIN retention_state s ON s.device_id=d.device_id "
                        "WHERE d.bucket_start_us>=COALESCE(s.raw_before_us,0) "
                        "AND d.bucket_start_us<? ORDER BY d.bucket_start_us LIMIT 1",
                        (raw_cutoff,),
                    ) as cur:
                        row = await cur.fetchone()
                if row is None:
                    await conn.execute(
                        "UPDATE retention_state SET raw_before_us=MAX(raw_before_us,?)",
                        (raw_cutoff,),
                    )
                    break
                device, start = str(row[0]), int(row[1])
                end = start + BUCKET_US
                async with conn.execute(
                    "SELECT bucket_start_us FROM dirty_rollup_buckets WHERE device_id=? "
                    "AND bucket_start_us<?",
                    (device, end),
                ) as cur:
                    dirty = await cur.fetchall()
                for bucket in dirty:
                    await self.rebuild_bucket(conn, device, int(bucket[0]))
                await self.rebuild_bucket(conn, device, start)
                # Replace-before-delete and the watermark commit atomically.
                await conn.execute(
                    "INSERT INTO retention_state(device_id,raw_before_us) VALUES(?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET raw_before_us=excluded.raw_before_us",
                    (device, end),
                )
                await conn.execute(
                    "DELETE FROM readings WHERE device_id=? AND read_at_us<? AND read_at_us<>"
                    "(SELECT MAX(read_at_us) FROM readings WHERE device_id=?)",
                    (device, end - GAP_US, device),
                )
                await conn.execute(
                    "UPDATE devices SET data_version=data_version+1 WHERE device_id=?", (device,)
                )
                count += 1
            await asyncio.sleep(0)

        # At most one calendar day per pass; never load an unbounded raw range.
        async with self.write() as conn:
            async with conn.execute(
                "SELECT r.device_id,MIN(r.bucket_start_us) AS bucket FROM rollup_5m r "
                "JOIN retention_state s ON s.device_id=r.device_id "
                "WHERE r.bucket_start_us<? AND r.bucket_start_us<s.raw_before_us "
                "GROUP BY r.device_id ORDER BY bucket LIMIT 1",
                (at - self.policy.five_minute_days * DAY_US,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                device, first = str(row[0]), int(row[1])
                state = await self.retention(conn, device)
                config = await one(conn, "SELECT display_timezone FROM app_config")
                timezone = state["archive_timezone"] or str(config[0])
                zone = ZoneInfo(timezone)
                date = datetime.fromisoformat(iso(first)).astimezone(zone).date()
                start = timestamp_us(datetime.combine(date, day_time(), zone).isoformat())
                end = timestamp_us(
                    datetime.combine(date + timedelta(days=1), day_time(), zone).isoformat()
                )
                if end <= min(state["raw_before_us"], at - self.policy.five_minute_days * DAY_US):
                    dirty = await one(
                        conn,
                        "SELECT count(*) FROM dirty_rollup_buckets WHERE device_id=? "
                        "AND bucket_start_us>=? AND bucket_start_us<?",
                        (device, start, end),
                    )
                    if dirty[0]:
                        raise RuntimeError("Cannot archive dirty historical buckets")
                    m = Metrics()
                    async with conn.execute(
                        "SELECT r.*,t0.above_us a0,t0.load_above_watt_seconds l0,t0.excess_watt_seconds e0,"
                        "t1.above_us a1,t1.load_above_watt_seconds l1,t1.excess_watt_seconds e1,"
                        "t2.above_us a2,t2.load_above_watt_seconds l2,t2.excess_watt_seconds e2 "
                        "FROM rollup_5m r "
                        "JOIN rollup_threshold_5m t0 USING(device_id,bucket_start_us) "
                        "JOIN rollup_threshold_5m t1 USING(device_id,bucket_start_us) "
                        "JOIN rollup_threshold_5m t2 USING(device_id,bucket_start_us) "
                        "WHERE r.device_id=? AND bucket_start_us>=? AND bucket_start_us<? "
                        "AND t0.threshold_w=3680 AND t1.threshold_w=5000 AND t2.threshold_w=6000",
                        (device, start, end),
                    ) as cur:
                        rows = list(await cur.fetchall())
                    expected = (
                        await one(
                            conn,
                            "SELECT count(*) FROM rollup_5m WHERE device_id=? "
                            "AND bucket_start_us>=? AND bucket_start_us<?",
                            (device, start, end),
                        )
                    )[0]
                    if len(rows) != expected or any(
                        r["algorithm_version"] != ALGORITHM_VERSION for r in rows
                    ):
                        raise RuntimeError("Cannot archive incomplete or incompatible summaries")
                    for r in rows:
                        m.merge(self.rollup_metrics(r))
                    await conn.execute(
                        "INSERT INTO daily_archive VALUES(?,?,?,?,?,?)",
                        (device, start, end, timezone, ALGORITHM_VERSION, json.dumps(asdict(m))),
                    )
                    await conn.execute(
                        "UPDATE retention_state SET five_before_us=?,archive_timezone=? WHERE device_id=?",
                        (end, timezone, device),
                    )
                    await conn.execute(
                        "DELETE FROM rollup_5m WHERE device_id=? AND bucket_start_us<?",
                        (device, end),
                    )
                    await conn.execute(
                        "UPDATE devices SET data_version=data_version+1 WHERE device_id=?",
                        (device,),
                    )
                    count += 1
            async with conn.execute(
                "SELECT device_id,start_us,end_us FROM daily_archive WHERE end_us<? "
                "ORDER BY start_us LIMIT 24",
                (at - self.policy.daily_days * DAY_US,),
            ) as cur:
                expired = await cur.fetchall()
            for row in expired:
                await conn.execute(
                    "DELETE FROM daily_archive WHERE device_id=? AND start_us=?",
                    (row[0], row[1]),
                )
                await conn.execute(
                    "UPDATE retention_state SET expired_before_us=MAX(expired_before_us,?) WHERE device_id=?",
                    (row[2], row[0]),
                )
                await conn.execute(
                    "UPDATE devices SET data_version=data_version+1 WHERE device_id=?",
                    (row[0],),
                )
                count += 1
            await conn.execute(
                "DELETE FROM recovery_gaps WHERE end_us<?",
                (at - self.policy.daily_days * DAY_US,),
            )
        self.last_maintenance_at = iso(at)
        return count

    async def maintenance(self) -> None:
        last_checkpoint = 0.0
        while True:
            count = 0
            try:
                count = await self.compact()
                count += await self.rebuild()
                if time.monotonic() - last_checkpoint > 60:
                    async with self.lock:
                        # Busy readers may prevent truncation; capacity checks still stop ingestion.
                        await self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    last_checkpoint = time.monotonic()
                self.maintenance_error = None
            except (aiosqlite.Error, RuntimeError):
                self.maintenance_error = "maintenance_failed"
                log.exception("Rollup storage maintenance failed")
                async with self.lock:
                    await self.writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
            await asyncio.sleep(0.1 if count else 5)
