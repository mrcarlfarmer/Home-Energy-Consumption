ALTER TABLE app_config ADD COLUMN tariff_pricing_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (tariff_pricing_enabled IN (0, 1));

CREATE TABLE tariff_cache (
    source TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    fetched_at_us INTEGER NOT NULL,
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    details_json TEXT NOT NULL CHECK (json_valid(details_json))
) STRICT;

CREATE TABLE tariff_rates (
    source TEXT NOT NULL REFERENCES tariff_cache(source) ON DELETE CASCADE,
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    pence_per_kwh TEXT NOT NULL,
    tariff_code TEXT NOT NULL,
    band TEXT NOT NULL,
    PRIMARY KEY (source, start_us)
) STRICT, WITHOUT ROWID;

PRAGMA user_version = 6;
