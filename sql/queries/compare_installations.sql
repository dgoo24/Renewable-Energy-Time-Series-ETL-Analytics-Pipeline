-- Compare available photovoltaic installations.
-- The specific yield is a simple energy/capacity ratio, not a Performance Ratio.
-- Energy uses the project's 15-minute interval assumption.
WITH totals AS (
    SELECT
        i.code,
        i.name,
        i.capacity_kw,
        min(m.timestamp) AS first_timestamp,
        max(m.timestamp) AS last_timestamp,
        count(*) FILTER (WHERE s.sensor_type = 'active_power_kw'
                         AND m.processed_value IS NOT NULL) AS power_samples,
        sum(CASE WHEN s.sensor_type = 'active_power_kw'
                 THEN m.processed_value * (15.0 / 60.0) ELSE 0 END) AS energy_kwh,
        sum(CASE WHEN s.sensor_type = 'irradiance_w_m2'
                 THEN m.processed_value * (15.0 / 60.0) / 1000.0 ELSE 0 END) AS irradiation_kwh_m2,
        avg(CASE WHEN s.sensor_type = 'active_power_kw'
                 THEN m.processed_value END) AS mean_power_kw
    FROM measurements_processed AS m
    JOIN installations AS i ON i.id = m.installation_id
    JOIN sensors AS s       ON s.id = m.sensor_id
    GROUP BY i.code, i.name, i.capacity_kw
)
SELECT
    code,
    name,
    capacity_kw,
    first_timestamp,
    last_timestamp,
    power_samples,
    round(energy_kwh::numeric, 2)       AS energy_kwh,
    round(irradiation_kwh_m2::numeric, 2) AS irradiation_kwh_m2,
    round(mean_power_kw::numeric, 3)    AS mean_power_kw,
    CASE WHEN capacity_kw > 0
         THEN round((energy_kwh / capacity_kw)::numeric, 2)
         ELSE NULL END                  AS specific_yield_kwh_per_kwp
FROM totals
ORDER BY code;
