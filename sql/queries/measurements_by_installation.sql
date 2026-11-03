-- Measurements for one installation.
-- Usage: psql -v installation_code=INST_A -f measurements_by_installation.sql
-- Units: power kW, irradiance W/m2, temperatures degC.
SELECT
    i.code                AS installation,
    s.code                AS sensor,
    s.sensor_type         AS metric,
    s.unit,
    m.timestamp,
    m.processed_value,
    m.quality_status,
    m.is_imputed,
    m.source_quality_status
FROM measurements_processed AS m
JOIN installations AS i ON i.id = m.installation_id
JOIN sensors AS s       ON s.id = m.sensor_id
WHERE i.code = :'installation_code'
ORDER BY m.timestamp, s.sensor_type
LIMIT 10000;
