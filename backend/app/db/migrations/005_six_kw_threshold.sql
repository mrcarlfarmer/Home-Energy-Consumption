CREATE TABLE rollup_threshold_5m_v5 (
    device_id TEXT NOT NULL,
    bucket_start_us INTEGER NOT NULL,
    threshold_w INTEGER NOT NULL CHECK (threshold_w = 6000),
    above_us INTEGER NOT NULL CHECK (above_us BETWEEN 0 AND 300000000),
    load_above_watt_seconds REAL NOT NULL CHECK (load_above_watt_seconds >= 0),
    excess_watt_seconds REAL NOT NULL CHECK (excess_watt_seconds >= 0),
    PRIMARY KEY (device_id, bucket_start_us, threshold_w),
    FOREIGN KEY (device_id, bucket_start_us)
        REFERENCES rollup_5m(device_id, bucket_start_us) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

INSERT INTO rollup_threshold_5m_v5
SELECT * FROM rollup_threshold_5m WHERE threshold_w = 6000;

DROP TABLE rollup_threshold_5m;
ALTER TABLE rollup_threshold_5m_v5 RENAME TO rollup_threshold_5m;

-- Removing array entries retains the original JSON numbers without rounding them.
UPDATE daily_archive
SET metrics_json = CASE
    WHEN json_valid(metrics_json) THEN CASE
        WHEN json_type(metrics_json) = 'object'
            AND json_type(metrics_json, '$.observed_us') = 'integer'
            AND json_extract(metrics_json, '$.observed_us') >= 0
            AND json_type(metrics_json, '$.energy_ws') IN ('integer', 'real')
            AND json_extract(metrics_json, '$.energy_ws') >= 0
            AND json_type(metrics_json, '$.samples') = 'integer'
            AND json_extract(metrics_json, '$.samples') >= 0
            AND json_type(metrics_json, '$.invalid') = 'integer'
            AND json_extract(metrics_json, '$.invalid') >= 0
            AND json_type(metrics_json, '$.peak_mw') IN ('integer', 'null')
            AND json_type(metrics_json, '$.peak_at') IN ('integer', 'null')
            AND json_array_length(metrics_json, '$.above') = 3
            AND json_array_length(metrics_json, '$.load') = 3
            AND json_array_length(metrics_json, '$.excess') = 3
            AND json_type(metrics_json, '$.above[2]') = 'integer'
            AND json_extract(metrics_json, '$.above[2]')
                BETWEEN 0 AND json_extract(metrics_json, '$.observed_us')
            AND json_type(metrics_json, '$.load[2]') IN ('integer', 'real')
            AND json_extract(metrics_json, '$.load[2]') >= 0
            AND json_type(metrics_json, '$.excess[2]') IN ('integer', 'real')
            AND json_extract(metrics_json, '$.excess[2]') >= 0
        THEN json_set(
            metrics_json,
            '$.above', json_remove(json_extract(metrics_json, '$.above'), '$[0]', '$[0]'),
            '$.load', json_remove(json_extract(metrics_json, '$.load'), '$[0]', '$[0]'),
            '$.excess', json_remove(json_extract(metrics_json, '$.excess'), '$[0]', '$[0]')
        )
        ELSE NULL
    END
    ELSE NULL
END;

PRAGMA user_version = 5;
