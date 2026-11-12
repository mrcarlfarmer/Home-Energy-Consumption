import json
import sqlite3
from pathlib import Path

import pytest
from app.analytics.service import Analytics
from app.db.store import Store, one
from app.models import ALGORITHM_VERSION, BUCKET_US, timestamp_us
from app.storage import DAY_US
from conftest import DEVICE

MIGRATIONS = Path(__file__).parents[1] / "backend" / "app" / "db" / "migrations"
START = timestamp_us("2026-01-01T00:00:00Z")
BUCKET = START + DAY_US
ARCHIVE = {
    "observed_us": 900_000_000,
    "energy_ws": 6_000_000.123456789,
    "samples": 90,
    "invalid": 2,
    "peak_mw": 8_000_000,
    "peak_at": START + 10_000_000,
    "above": [800_000_000, 600_000_000, 300_000_000],
    "load": [5_000_000.1, 4_000_000.2, 2_400_000.123456789],
    "excess": [2_000_000.1, 1_000_000.2, 600_000.123456789],
}
LEGACY_THRESHOLDS = [
    (3680, 300_000_000, 2_400_000.0, 1_296_000.0),
    (5000, 200_000_000, 1_600_000.0, 600_000.0),
    (6000, 100_000_000, 800_000.123456789, 200_000.123456789),
]


def create_legacy_database(path: Path, version: int, metrics_json: str) -> None:
    migrations = [
        "001_initial.sql",
        "002_sessions.sql",
        "003_retention.sql",
        "004_home_assistant.sql",
    ]
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for filename in migrations[:version]:
            conn.executescript((MIGRATIONS / filename).read_text())
        conn.execute(
            "INSERT INTO devices VALUES(?,?,?,?,?)", (DEVICE, "A-TEST", "Test meter", START, 42)
        )
        conn.execute(
            "UPDATE app_config SET active_device_id=?,account_number='A-TEST',"
            "display_timezone='UTC',revision=7 WHERE singleton=1",
            (DEVICE,),
        )
        conn.execute(
            "INSERT INTO retention_state VALUES(?,?,?,?,?)",
            (DEVICE, START + 2 * DAY_US, BUCKET, 0, "UTC"),
        )
        conn.execute(
            "INSERT INTO rollup_5m VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                DEVICE,
                BUCKET,
                ALGORITHM_VERSION,
                300_000_000,
                2_400_000.0,
                30,
                0,
                8_000_000,
                BUCKET,
                BUCKET,
            ),
        )
        conn.executemany(
            "INSERT INTO rollup_threshold_5m VALUES(?,?,?,?,?,?)",
            [(DEVICE, BUCKET, *row) for row in LEGACY_THRESHOLDS],
        )
        conn.execute(
            "INSERT INTO daily_archive VALUES(?,?,?,?,?,?)",
            (DEVICE, START, BUCKET, "UTC", ALGORITHM_VERSION, metrics_json),
        )
        if version >= 4:
            conn.execute(
                "UPDATE app_config SET home_assistant_url='http://homeassistant.local:8123',"
                "home_assistant_token='synthetic-token',home_assistant_entity='sensor.ohme_power',"
                "home_assistant_enabled=1 WHERE singleton=1"
            )
            conn.execute(
                "INSERT INTO ev_intervals VALUES(?,?,?,?,?)",
                ("synthetic-source", DEVICE, BUCKET, BUCKET + 10_000_000, 7_000_000),
            )
            conn.execute(
                "INSERT INTO ev_sync VALUES(?,?,?)",
                ("synthetic-source", DEVICE, BUCKET + 10_000_000),
            )


@pytest.mark.parametrize("version", [3, 4])
async def test_upgrade_preserves_six_kw_archives_and_other_data(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "legacy.sqlite3"
    create_legacy_database(path, version, json.dumps(ARCHIVE))
    store = Store(path)
    await store.open()
    try:
        config = await store.public_config()
        assert config["revision"] == 7
        assert config["thresholds_w"] == [6000]
        assert config["home_assistant_enabled"] is (version == 4)
        assert config["has_home_assistant_token"] is (version == 4)
        if version == 4:
            assert (await store.config())["home_assistant_token"] == "synthetic-token"
        async with store.read() as conn:
            assert (await one(conn, "PRAGMA user_version"))[0] == 9
            assert (await one(conn, "PRAGMA integrity_check"))[0] == "ok"
            async with conn.execute("PRAGMA foreign_key_check") as cur:
                assert await cur.fetchall() == []
            async with conn.execute(
                "SELECT threshold_w,above_us,load_above_watt_seconds,excess_watt_seconds "
                "FROM rollup_threshold_5m"
            ) as cur:
                assert [tuple(row) for row in await cur.fetchall()] == [LEGACY_THRESHOLDS[2]]
            archived = json.loads((await one(conn, "SELECT metrics_json FROM daily_archive"))[0])
            expected = {
                **ARCHIVE,
                "above": [ARCHIVE["above"][2]],
                "load": [ARCHIVE["load"][2]],
                "excess": [ARCHIVE["excess"][2]],
            }
            assert archived == expected
            assert (await one(conn, "SELECT count(*) FROM ev_intervals"))[0] == int(version == 4)
            assert (await one(conn, "SELECT count(*) FROM ev_sync"))[0] == int(version == 4)

        result = await Analytics(store).summary(START, BUCKET + BUCKET_US, "UTC")
        assert result["observed_seconds"] == 1200
        assert result["sampled_peak_w"] == 8000
        assert len(result["thresholds"]) == 1
        threshold = result["thresholds"][0]
        assert threshold["threshold_w"] == 6000
        assert threshold["seconds_above"] == 400
        assert threshold["time_above_pct"] == pytest.approx(100 / 3)
        assert threshold["energy_when_above_kwh"] == pytest.approx(
            (ARCHIVE["load"][2] + LEGACY_THRESHOLDS[2][2]) / 3_600_000
        )
        assert threshold["excess_energy_kwh"] == pytest.approx(
            (ARCHIVE["excess"][2] + LEGACY_THRESHOLDS[2][3]) / 3_600_000
        )

        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            async with store.write() as conn:
                await conn.execute(
                    "INSERT INTO rollup_threshold_5m VALUES(?,?,?,?,?,?)",
                    (DEVICE, BUCKET, 5000, 0, 0, 0),
                )
    finally:
        await store.close()


async def test_upgrade_preserves_empty_daily_archive(tmp_path: Path) -> None:
    path = tmp_path / "empty-archive.sqlite3"
    create_legacy_database(path, 4, json.dumps(ARCHIVE))
    empty = {
        "observed_us": 0,
        "energy_ws": 0,
        "samples": 0,
        "invalid": 5,
        "peak_mw": None,
        "peak_at": None,
        "above": [0, 0, 0],
        "load": [0.0, 0.0, 0.0],
        "excess": [0.0, 0.0, 0.0],
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO daily_archive VALUES(?,?,?,?,?,?)",
            (DEVICE, START - DAY_US, START, "UTC", ALGORITHM_VERSION, json.dumps(empty)),
        )
    store = Store(path)
    await store.open()
    try:
        async with store.read() as conn:
            row = await one(
                conn, "SELECT metrics_json FROM daily_archive WHERE start_us=?", (START - DAY_US,)
            )
            assert json.loads(row[0]) == {
                **empty,
                "above": [0],
                "load": [0.0],
                "excess": [0.0],
            }
        result = await Analytics(store).summary(START - DAY_US, START, "UTC")
        assert result["observed_seconds"] == 0
        assert result["sampled_peak_w"] is None
        assert result["thresholds"][0]["time_above_pct"] is None
        assert result["thresholds"][0]["energy_when_above_kwh"] is None
    finally:
        await store.close()


@pytest.mark.parametrize(
    "metrics_json",
    [
        "{not json",
        json.dumps({**ARCHIVE, "above": [100, 200]}),
        json.dumps({**ARCHIVE, "load": [1, 2, "invalid"]}),
        json.dumps({**ARCHIVE, "energy_ws": "invalid"}),
    ],
)
async def test_malformed_archive_rolls_back_migration(tmp_path: Path, metrics_json: str) -> None:
    path = tmp_path / "malformed.sqlite3"
    create_legacy_database(path, 4, metrics_json)
    store = Store(path)
    with pytest.raises(sqlite3.IntegrityError, match="daily_archive.metrics_json"):
        await store.open()

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT metrics_json FROM daily_archive").fetchone()[0] == metrics_json
        assert (
            conn.execute(
                "SELECT threshold_w,above_us,load_above_watt_seconds,excess_watt_seconds "
                "FROM rollup_threshold_5m ORDER BY threshold_w"
            ).fetchall()
            == LEGACY_THRESHOLDS
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='rollup_threshold_5m_v5'"
            ).fetchone()[0]
            == 0
        )
