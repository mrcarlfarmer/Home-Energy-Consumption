CREATE TABLE retention_state (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    raw_before_us INTEGER NOT NULL DEFAULT 0,
    five_before_us INTEGER NOT NULL DEFAULT 0,
    expired_before_us INTEGER NOT NULL DEFAULT 0,
    archive_timezone TEXT
) STRICT;
CREATE TABLE daily_archive (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL CHECK(end_us > start_us),
    timezone TEXT NOT NULL,
    algorithm_version INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    PRIMARY KEY(device_id, start_us)
) STRICT, WITHOUT ROWID;
CREATE TABLE recovery_state (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    cursor_us INTEGER NOT NULL,
    target_us INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'complete', 'paused')),
    error_code TEXT,
    unavailable_before_us INTEGER,
    last_attempt_us INTEGER
) STRICT;
CREATE TABLE recovery_gaps (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL CHECK(end_us > start_us),
    reason TEXT NOT NULL CHECK(reason IN ('lookback_limit','range_too_old','empty','partial')),
    PRIMARY KEY(device_id,start_us,end_us)
) STRICT, WITHOUT ROWID;
PRAGMA user_version = 3;
