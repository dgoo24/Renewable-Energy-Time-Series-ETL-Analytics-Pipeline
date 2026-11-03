-- Missing measurement intervals in the processed time series.
-- The processed layer stores every slot on the regular grid, and a slot that has
-- no usable measurement has quality_status = 'missing'.  Invalid values are kept
-- separately (quality_status = 'invalid') and are deliberately not reported here.
--
-- This query groups consecutive missing slots into runs.  It assumes the project's
-- default 15-minute sampling interval; change the interval literal if the
-- configured RESAMPLE_FREQUENCY differs.
WITH missing_rows AS (
    SELECT
        i.code AS installation,
        s.code AS sensor,
        s.sensor_type AS metric,
        m.timestamp,
        m.timestamp - (
            row_number() OVER (
                PARTITION BY m.installation_id, m.sensor_id
                ORDER BY m.timestamp
            ) * interval '15 minutes'
        ) AS run_group
    FROM measurements_processed AS m
    JOIN installations AS i ON i.id = m.installation_id
    JOIN sensors AS s       ON s.id = m.sensor_id
    WHERE m.quality_status = 'missing'
)
SELECT
    installation,
    sensor,
    metric,
    min(timestamp) AS start_timestamp,
    max(timestamp) AS end_timestamp,
    count(*)       AS missing_slots,
    round(count(*) * 15.0, 2) AS duration_minutes
FROM missing_rows
GROUP BY installation, sensor, metric, run_group
ORDER BY installation, sensor, start_timestamp;
