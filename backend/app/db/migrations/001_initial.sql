CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,
    account_number TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    data_version INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE TABLE app_config (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL DEFAULT 1,
    account_number TEXT,
    active_device_id TEXT REFERENCES devices(device_id),
    octopus_api_key TEXT,
    polling_enabled INTEGER NOT NULL DEFAULT 0 CHECK (polling_enabled IN (0, 1)),
    poll_interval_seconds INTEGER NOT NULL DEFAULT 45 CHECK (poll_interval_seconds BETWEEN 30 AND 3600),
    display_timezone TEXT NOT NULL DEFAULT 'Europe/London',
    updated_at_us INTEGER NOT NULL
) STRICT;
CREATE TABLE admin_auth (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    password_hash TEXT NOT NULL,
    updated_at_us INTEGER NOT NULL
) STRICT;
CREATE TABLE readings (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    read_at TEXT NOT NULL,
    source_read_at TEXT NOT NULL,
    read_at_us INTEGER NOT NULL,
    demand_mw INTEGER,
    consumption_mwh INTEGER CHECK (consumption_mwh IS NULL OR consumption_mwh >= 0),
    export_mwh INTEGER CHECK (export_mwh IS NULL OR export_mwh >= 0),
    quality TEXT NOT NULL CHECK (quality IN ('valid', 'missing_demand', 'invalid_demand')),
    first_received_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    PRIMARY KEY (device_id, read_at),
    CHECK ((quality = 'valid' AND demand_mw IS NOT NULL) OR (quality <> 'valid' AND demand_mw IS NULL))
) STRICT, WITHOUT ROWID;
CREATE UNIQUE INDEX idx_readings_device_time ON readings(device_id, read_at_us);
CREATE TABLE rollup_5m (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    bucket_start_us INTEGER NOT NULL CHECK (bucket_start_us % 300000000 = 0),
    algorithm_version INTEGER NOT NULL,
    observed_us INTEGER NOT NULL CHECK (observed_us BETWEEN 0 AND 300000000),
    import_watt_seconds REAL NOT NULL CHECK (import_watt_seconds >= 0),
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    invalid_sample_count INTEGER NOT NULL CHECK (invalid_sample_count >= 0),
    peak_import_mw INTEGER,
    peak_at_us INTEGER,
    updated_at_us INTEGER NOT NULL,
    PRIMARY KEY(device_id, bucket_start_us)
) STRICT, WITHOUT ROWID;
CREATE TABLE rollup_threshold_5m (
    device_id TEXT NOT NULL,
    bucket_start_us INTEGER NOT NULL,
    threshold_w INTEGER NOT NULL CHECK (threshold_w IN (3680, 5000, 6000)),
    above_us INTEGER NOT NULL CHECK (above_us BETWEEN 0 AND 300000000),
    load_above_watt_seconds REAL NOT NULL CHECK (load_above_watt_seconds >= 0),
    excess_watt_seconds REAL NOT NULL CHECK (excess_watt_seconds >= 0),
    PRIMARY KEY(device_id, bucket_start_us, threshold_w),
    FOREIGN KEY(device_id, bucket_start_us) REFERENCES rollup_5m(device_id, bucket_start_us) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;
CREATE TABLE dirty_rollup_buckets (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    bucket_start_us INTEGER NOT NULL,
    PRIMARY KEY(device_id, bucket_start_us)
) STRICT, WITHOUT ROWID;
INSERT INTO app_config(singleton, updated_at_us) VALUES (1, 0);
PRAGMA user_version = 1;
