CREATE TABLE diagnostic_events (
    id INTEGER PRIMARY KEY,
    at_us INTEGER NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
) STRICT;
CREATE INDEX idx_diagnostic_events_at ON diagnostic_events(at_us);
PRAGMA user_version = 10;
