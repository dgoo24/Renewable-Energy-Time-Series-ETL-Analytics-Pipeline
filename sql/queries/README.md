# SQL analysis queries

These queries target the schema created by `python -m src.main init-db` on
PostgreSQL. They are plain SQL so they can be used from `psql`, pgAdmin, or any
SQL notebook.

Timestamps are UTC. Values use the documented units: power in kW, irradiance in
W/m², temperatures in °C. Energy and irradiation calculations assume the
project's default 15-minute sampling interval; adjust `15.0 / 60.0` if the
configured interval changes.

Examples:

```bash
psql "$DATABASE_URL" -v installation_code=INST_A \
  -f sql/queries/measurements_by_installation.sql

psql "$DATABASE_URL" -v installation_code=INST_A \
  -v start_ts='2023-06-01 00:00:00+00' \
  -v end_ts='2023-07-01 00:00:00+00' \
  -f sql/queries/measurements_by_time_range.sql

psql "$DATABASE_URL" -v include_missing=true \
  -f sql/queries/retrieve_processed_measurements.sql
```

Queries starting with `:` are psql variables. The application's Python analytics
module implements the Performance Ratio, which is intentionally not duplicated in
SQL.

`missing_measurement_intervals.sql` groups consecutive `quality_status = 'missing'`
slots into runs. Invalid values are kept separately and are not counted as missing;
they can be inspected with `measurements_by_installation.sql` or
`retrieve_processed_measurements.sql`.
