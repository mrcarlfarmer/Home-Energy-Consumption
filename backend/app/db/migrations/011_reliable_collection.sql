UPDATE app_config SET poll_interval_seconds = 120 WHERE poll_interval_seconds = 45;
UPDATE recovery_state SET status = 'pending', error_code = NULL
WHERE status = 'paused' AND error_code = 'graphql_KT-GB-4039';
PRAGMA user_version = 11;
