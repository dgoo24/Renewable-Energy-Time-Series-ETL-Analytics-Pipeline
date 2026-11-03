-- Measurements inside a half-open time range [start, end).
-- Timestamps are stored in UTC.  Replace the psql variables at the command line:
--   psql -v installation_code=INST_A \
--        -v start_ts='2023-06-01 00:00:00+00' \
--        -v end_ts='2023-07-01 00:00:00+00' \
--        -f measurements_by_time_range.sql
SELECT
    i.code        AS installation,
    s.code        AS sensor,
    s.sensor_type AS metric,
    m.timestamp,
    m.processed_value,
    m.quality_status
FROM measurements_processed AS m
JOIN installations AS i ON i.id = m.installation_id
JOIN sensors AS s       ON s.id = m.sensor_id
WHERE i.code = :'installation_code'
  AND m.timestamp >= :'start_ts'::timestamptz
  AND m.timestamp <  :'end_ts'::timestamptz
ORDER BY m.timestamp, s.sensor_type;
