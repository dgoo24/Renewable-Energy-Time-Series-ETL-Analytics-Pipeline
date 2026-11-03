-- Processed measurements, including imputed and missing rows.
SELECT
    i.code                 AS installation,
    s.code                 AS sensor,
    s.sensor_type          AS metric,
    s.unit,
    m.timestamp,
    m.processed_value,
    m.original_value,
    m.quality_status,
    m.source_quality_status,
    m.is_imputed,
    m.processing_timestamp
FROM measurements_processed AS m
JOIN installations AS i ON i.id = m.installation_id
JOIN sensors AS s       ON s.id = m.sensor_id
WHERE (:include_missing::boolean IS TRUE OR m.processed_value IS NOT NULL)
ORDER BY i.code, m.timestamp, s.sensor_type
LIMIT 10000;
