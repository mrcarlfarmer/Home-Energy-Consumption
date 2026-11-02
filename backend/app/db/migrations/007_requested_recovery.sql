ALTER TABLE recovery_state ADD COLUMN requested INTEGER NOT NULL DEFAULT 0
    CHECK (requested IN (0, 1));
PRAGMA user_version = 7;
