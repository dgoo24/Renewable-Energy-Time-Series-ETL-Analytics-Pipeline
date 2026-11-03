-- Monthly averages and sample counts for processed measurements.
-- Energy/irradiation use a 15-minute rectangular integration.
SELECT
    i.code                                  AS installation,
    s.sensor_type                           AS metric,
    s.unit,
    date_trunc('month', m.timestamp)        AS month,
    count(*)                                AS total_rows,
    count(m.processed_value)                AS valid_values,
    avg(m.processed_value)                  AS average_value,
    min(m.processed_value)                  AS minimum_value,
    max(m.processed_value)                  AS maximum_value,
    sum(
        CASE WHEN s.sensor_type = 'active_power_kw'
             THEN m.processed_value * (15.0 / 60.0)
             ELSE 0 END
    )                                       AS energy_kwh,
    sum(
        CASE WHEN s.sensor_type = 'irradiance_w_m2'
             THEN m.processed_value * (15.0 / 60.0) / 1000.0
             ELSE 0 END
    )                                       AS irradiation_kwh_m2
FROM measurements_processed AS m
JOIN installations AS i ON i.id = m.installation_id
JOIN sensors AS s       ON s.id = m.sensor_id
WHERE m.processed_value IS NOT NULL
GROUP BY i.code, s.sensor_type, s.unit, date_trunc('month', m.timestamp)
ORDER BY i.code, month, metric;
