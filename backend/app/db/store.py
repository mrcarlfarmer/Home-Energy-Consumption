import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from app.analytics.intervals import Metrics, integrate
from app.models import ALGORITHM_VERSION, BUCKET_US, GAP_US, ConfigUpdate, Reading, now_us

log = logging.getLogger(__name__)


class Conflict(Exception):
    pass


async def one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()) -> aiosqlite.Row:
    async with conn.execute(sql, args) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise LookupError("Database record not found")
    return row


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=2)
        self.connections: list[aiosqlite.Connection] = []
        self.writer: aiosqlite.Connection

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                    version = (await one(conn, "PRAGMA user_version"))[0]
                    if version > 1:
                        raise RuntimeError("Database schema is newer than this application")
                    if version == 0:
                        migration = (
                            Path(__file__).parent / "migrations" / "001_initial.sql"
                        ).read_text()
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

    async def ingest(self, device: str, readings: list[Reading], revision: int) -> int:
        changes = 0
        async with self.write() as conn:
            config = await one(conn, "SELECT * FROM app_config WHERE singleton=1")
            if config["revision"] != revision or not config["polling_enabled"]:
                return 0
            if config["active_device_id"] != device:
                return 0
            for reading in sorted(readings, key=lambda r: r.read_at_us):
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
                    left = (reading.read_at_us - GAP_US) // BUCKET_US * BUCKET_US
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
                    "FROM rollup_5m WHERE algorithm_version<>? LIMIT 24",
                    (ALGORITHM_VERSION,),
                )
                async with conn.execute(
                    "SELECT * FROM dirty_rollup_buckets ORDER BY bucket_start_us DESC LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    break
                device, start = str(row[0]), int(row[1])
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
                count += 1
            await asyncio.sleep(0)
        return count

    async def maintenance(self) -> None:
        last_checkpoint = 0.0
        while True:
            try:
                count = await self.rebuild()
                if time.monotonic() - last_checkpoint > 60:
                    async with self.lock:
                        await self.writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    last_checkpoint = time.monotonic()
            except aiosqlite.Error:
                log.exception("Rollup storage maintenance failed")
                raise
            await asyncio.sleep(0.1 if count else 5)
