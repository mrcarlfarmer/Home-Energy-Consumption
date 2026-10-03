CREATE TABLE admin_sessions (
    id INTEGER PRIMARY KEY,
    token_digest TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    expires_at_us INTEGER NOT NULL CHECK (expires_at_us > 0)
) STRICT;
PRAGMA user_version = 2;
