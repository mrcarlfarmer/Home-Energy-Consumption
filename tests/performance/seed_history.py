"""Offline synthetic-data generator. Refuses to overwrite an existing database."""

import argparse
import asyncio
import sqlite3
from pathlib import Path

from app.analytics.intervals import integrate
from app.db.store import Store
from app.models import ALGORITHM_VERSION, BUCKET_US, THRESHOLDS, iso, now_us

DEVICE = "AA-BB-CC-DD-EE-FF-00-11"


async def initialize(path: Path) -> None:
    store = Store(path)
    await store.open()
    await store.close()


def demand(time: int) -> int | None:
    if (time // 10_000_000) % 8640 in (300, 301, 302, 303):
        return None
    return (400, 800, 1600, 2400, 4200, 6200)[(time // 600_000_000) % 6] * 1000


def seed(path: Path, days: int) -> None:
    if path.exists():
        raise ValueError("Use a new database path; this generator never replaces household data")
    asyncio.run(initialize(path))
    end = now_us() // BUCKET_US * BUCKET_US
    start = end - days * 86400_000_000
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "PRAGMA synchronous=FULL; PRAGMA cache_size=-4096; PRAGMA temp_store=FILE;"
        )
        conn.execute(
            "INSERT INTO devices VALUES(?,?,?,?,?)",
            (DEVICE, "A-SYNTHETIC", "Synthetic benchmark meter", start, 1),
        )
        conn.execute(
            "UPDATE app_config SET active_device_id=?,account_number='A-SYNTHETIC',revision=2",
            (DEVICE,),
        )
        rows = []
        base = []
        thresholds = []
        count = 0
        for bucket in range(start, end, BUCKET_US):
            context = [
                (t, demand(t))
                for t in range(bucket - 10_000_000, bucket + BUCKET_US + 1, 10_000_000)
            ]
            metrics = integrate(context, bucket, bucket + BUCKET_US)
            for at, power in context[1:-1]:
                stamp = iso(at)
                rows.append(
                    (
                        DEVICE,
                        stamp,
                        stamp,
                        at,
                        power,
                        None,
                        None,
                        "valid" if power is not None else "missing_demand",
                        end,
                        end,
                    )
                )
            base.append(
                (
                    DEVICE,
                    bucket,
                    ALGORITHM_VERSION,
                    metrics.observed_us,
                    metrics.energy_ws,
                    metrics.samples,
                    metrics.invalid,
                    metrics.peak_mw,
                    metrics.peak_at,
                    end,
                )
            )
            thresholds.extend(
                (DEVICE, bucket, t, metrics.above[j], metrics.load[j], metrics.excess[j])
                for j, t in enumerate(THRESHOLDS)
            )
            count += 1
            if count % 100 == 0 or bucket + BUCKET_US == end:
                conn.executemany("INSERT INTO readings VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
                conn.executemany("INSERT INTO rollup_5m VALUES(?,?,?,?,?,?,?,?,?,?)", base)
                conn.executemany("INSERT INTO rollup_threshold_5m VALUES(?,?,?,?,?,?)", thresholds)
                conn.commit()
                rows.clear()
                base.clear()
                thresholds.clear()
        stamp = iso(end)
        conn.execute(
            "INSERT INTO readings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                DEVICE,
                stamp,
                stamp,
                end,
                demand(end),
                None,
                None,
                "valid" if demand(end) is not None else "missing_demand",
                end,
                end,
            ),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"Seeded {days} days: {iso(start)} to {iso(end)}; {path.stat().st_size} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--days", type=int, default=365, choices=range(1, 367))
    options = parser.parse_args()
    seed(options.database, options.days)
