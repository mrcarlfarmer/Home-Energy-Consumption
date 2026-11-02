ALTER TABLE app_config ADD COLUMN home_assistant_url TEXT;
ALTER TABLE app_config ADD COLUMN home_assistant_token TEXT;
ALTER TABLE app_config ADD COLUMN home_assistant_entity TEXT;
ALTER TABLE app_config ADD COLUMN home_assistant_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (home_assistant_enabled IN (0, 1));
CREATE TABLE ev_intervals (
    source TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    power_mw INTEGER CHECK (power_mw IS NULL OR power_mw >= 0),
    PRIMARY KEY (source, start_us)
) STRICT, WITHOUT ROWID;
CREATE INDEX idx_ev_intervals_device_end ON ev_intervals(device_id, end_us);
CREATE TABLE ev_sync (
    source TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    cursor_us INTEGER NOT NULL
) STRICT;
PRAGMA user_version = 4;
