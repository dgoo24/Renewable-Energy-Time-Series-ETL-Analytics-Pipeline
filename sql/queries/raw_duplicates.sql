-- Raw duplicate measurements across runs/files.
-- Raw duplicates are intentionally preserved; this query makes them inspectable.
SELECT
    i.code AS installation,
    s.code AS sensor,
    s.sensor_type AS metric,
    m.timestamp,
    count(*) AS duplicate_rows,
    array_agg(DISTINCT m.source_file) AS source_files,
    array_agg(m.measured_value ORDER BY m.source_file, m.source_row) AS values
FROM measurements_raw AS m
JOIN installations AS i ON i.id = m.installation_id
JOIN sensors AS s       ON s.id = m.sensor_id
GROUP BY i.code, s.code, s.sensor_type, m.timestamp
HAVING count(*) > 1
ORDER BY i.code, metric, m.timestamp;
