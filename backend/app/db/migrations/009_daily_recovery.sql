CREATE TABLE daily_recovery_schedule (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    last_day TEXT NOT NULL
) STRICT, WITHOUT ROWID;
PRAGMA user_version = 9;
