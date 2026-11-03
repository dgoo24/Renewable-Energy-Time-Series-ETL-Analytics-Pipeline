# Renewable Energy Time-Series ETL & Analytics Pipeline

A small, maintainable Python application that ingests photovoltaic (PV) measurement
CSV files, validates and cleans them, stores raw and processed data in PostgreSQL,
computes descriptive analytics and a simplified Performance Ratio (PR), and writes
CSV tables, PNG plots and a Markdown report.

> **The example datasets in this repository are synthetic.** They were generated for
> demonstration and testing. They are **not** measurements from a real photovoltaic
> facility and must never be presented as real research data. Every report and plot
> repeats this warning. See `data/raw/README.md` and
> `scripts/generate_synthetic_data.py`.

---

## What the pipeline does

1. **Ingest** every `*.csv` file in the input directory.
   - Normalises headers, accepts mixed timestamp formats, records malformed values.
   - Converts wide CSV rows into long format: one row per `installation × sensor ×
     metric × timestamp`.
   - Rejects only rows without a parseable timestamp or installation/sensor identifier.
     Rejected rows are preserved in a separate table and CSV export.
2. **Validate** each measurement against explicit physical and plausibility thresholds.
   - Statuses: `valid`, `suspicious`, `invalid`, `missing`.
   - Statistical outliers are **not** invalidated; only configured physical limits are used.
3. **Clean** the validated data.
   - Removes duplicate measurements deterministically (configurable `first`/`last`),
     while raw duplicates stay untouched in the database.
   - Places measurements on a regular UTC grid (default 15 minutes).
   - Imputes only short **internal** gaps; never extrapolates.
4. **Persist** to PostgreSQL.
   - `measurements_raw` is append-only and keeps invalid and duplicate values.
   - `measurements_processed` is rebuilt from all stored raw rows for each affected
     installation, so adding a file never loses or double-counts earlier data.
   - Identical files are detected by SHA-256 and skipped.
5. **Analyse**: descriptive statistics, daily/monthly energy and irradiation,
   Performance Ratio, statistical trends, reduced-performance months and seasonal
   comparisons.
6. **Report**: CSV tables, PNG plots and a Markdown report with assumptions and
   data-quality notes.

The only infrastructure component is PostgreSQL 16 (Docker Compose). There is no
Kafka, Spark, Airflow, Kubernetes, FastAPI or cloud dependency.

---

## Requirements

- Python **3.11+** (developed with 3.12)
- PostgreSQL **16** for the documented setup (any recent PostgreSQL works for local
  development; the integration test suite was executed against PostgreSQL 18)
- Python packages from `requirements.txt`: pandas, NumPy, SciPy, SQLAlchemy,
  psycopg2, Matplotlib, python-dotenv, pytest.

---

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # Windows: copy .env.example .env
docker compose up -d          # PostgreSQL 16 on localhost:5432

python -m src.main init-db
python -m src.main process
python -m src.main history
python -m src.main report
```

Place input CSVs in `data/raw/`. The repository ships with a generator for synthetic
example data:

```bash
python scripts/generate_synthetic_data.py
# copies nine synthetic files into data/raw/
```

To run the complete pipeline without a database (useful for a quick check):

```bash
python -m src.main process --skip-db
```

`--skip-db` has no persistent file-hash ledger. Re-running it re-ingests the same
files and overwrites the generated outputs.

### Using an existing PostgreSQL instance

Set `DATABASE_URL` in `.env` (or pass `--database-url`) and run `init-db` once:

```bash
python -m src.main init-db --database-url "postgresql+psycopg2://user:pass@host:5432/db"
```

`init-db` is idempotent: it creates missing tables and never drops data.

---

## Input CSV format

Required columns (header names are normalised to lower snake case):

| column | meaning | unit |
|---|---|---|
| `timestamp` | measurement time | ISO 8601, `DD.MM.YYYY HH:MM[:SS]`, epoch seconds or milliseconds |
| `installation_id` | installation code | — |
| `sensor_id` | source sensor / logger channel | — |
| `active_power_kw` | AC active power | kW |
| `irradiance_w_m2` | irradiance (assumed plane-of-array) | W/m² |
| `module_temperature_c` | module temperature | °C |
| `ambient_temperature_c` | ambient temperature | °C |

Additional columns are ignored but reported as an ingestion issue.

- Timestamps with an offset are converted to UTC.
- Timestamps without an offset are interpreted in `INPUT_TIMEZONE`
  (default `Europe/Berlin`).
- Blank numeric values are `missing`; unparseable numeric text is `invalid`.
- Decimal commas and thousands separators are accepted.
- Raw files are never modified.

---

## Data-quality model

| status | meaning | stored in raw | processed value |
|---|---|---|---|
| `valid` | inside all physical/plausibility limits | yes | value retained |
| `suspicious` | physically possible but outside a plausibility range | yes | value retained, flagged |
| `invalid` | unparseable or physically impossible | yes | `NULL`, original value kept |
| `missing` | blank source value or absent grid slot | yes (if a row exists) | `NULL` unless imputed |

Invalid values are **never silently deleted**. They remain in `measurements_raw` and
in the rejected/quality exports. Duplicate raw measurements also remain stored; only
the processed layer is deduplicated.

Quality reports record counts per file, including duplicate measurements, missing
intervals, irregular sampling and rejected rows. `issues` is stored as JSON so it can
be aggregated or queried.

---

## Cleaning and imputation rules

- **Duplicates**: same installation, sensor, metric and timestamp. Rows are sorted
  deterministically by timestamp, source file and source row; the first (default) or
  last occurrence is kept. Removed counts are reported.
- **Grid**: resampling to `RESAMPLE_FREQUENCY` (default `15min`). If more than one raw
  sample falls in a bin, values are averaged and a warning is emitted.
- **Imputation**: only *internal* gaps where the time between the last observed
  sample and the next observed sample is at most
  `INTERPOLATION_MAX_GAP_MINUTES` (default 30 minutes). Leading and trailing gaps are
  never filled. With 15-minute data this fills at most one missing sample.
  - `interpolate` (default): time-based linear interpolation.
  - `ffill`: previous observation.
  - `none`: leave missing values.
  - `drop`: remove timestamps with any missing channel.
- Imputed values are flagged (`is_imputed`, `quality_status = imputed`); the original
  source value remains available (`original_value`).
- Invalid observations are **not** imputed, even when they sit between valid samples.

---

## Performance Ratio

```
final yield      Yf = E_ac [kWh] / P_nom [kWp]          (hours)
reference yield  Yr = H_poa [kWh/m²] / G_stc [kW/m²]    (hours)
PR = Yf / Yr
```

with `G_stc = 1 kW/m²`.

- PR is calculated only when capacity is known and paired power/irradiance coverage
  is at least `PR_MIN_COVERAGE` (default 80%).
- Irradiance is **assumed** to be plane-of-array; this is not verified by the data.
- Periods with no daylight, no irradiance, missing inputs or insufficient coverage are
  labelled with a `quality_flag` and do not contribute a PR value.
- Energy uses rectangular integration with the configured sampling interval. Missing
  samples contribute no energy and reduce reported coverage.
- Trends are fitted to monthly PR values. **A statistical slope is not proof of
  physical module degradation.** Seasonal effects, soiling, outages, curtailment and
  missing data can all produce trends. The report states this explicitly.

---

## CLI reference

```bash
python -m src.main init-db [--database-url URL]
python -m src.main process [--skip-db] [--input-dir DIR] [--output-dir DIR] [--database-url URL]
python -m src.main history [--limit N] [--database-url URL]
python -m src.main report [--output-dir DIR] [--installation CODE ...] [--database-url URL]
```

- `process` uses the database by default. Files whose SHA-256 was already imported are
  skipped. After a run, processed data is rebuilt for every touched installation from
  all raw rows stored for it.
- If every file was already imported, `process` rebuilds all installations so an
  earlier failed processing step can be recovered.
- `report` regenerates analytics and outputs from the stored processed measurements
  without re-reading the source files.

---

## Outputs

By default (`OUTPUT_DIR=output`):

```
output/
├── csv/
│   ├── statistics.csv
│   ├── daily_summary.csv
│   ├── monthly_summary.csv
│   ├── pr_daily.csv
│   ├── pr_monthly.csv
│   ├── pr_summary.csv
│   ├── pr_comparison.csv
│   ├── trends.csv
│   ├── reduced_performance_periods.csv
│   ├── seasonal_pr.csv
│   ├── installations.csv
│   ├── quality_reports.csv
│   ├── quality_issues.csv
│   ├── missing_intervals.csv
│   ├── file_processing_summary.csv
│   ├── processed_measurements.csv
│   ├── quality_<file>.csv
│   └── rejected_<file>.csv
├── plots/
│   ├── <installation>_active_power.png
│   ├── <installation>_irradiance.png
│   ├── <installation>_temperatures.png
│   ├── <installation>_daily_energy.png
│   ├── <installation>_monthly_pr.png
│   ├── quality_overview.png
│   ├── missing_values_heatmap.png
│   ├── missing_values_by_metric.png
│   ├── pr_comparison.png
│   ├── pr_trend.png
│   └── seasonal_pr.png
└── reports/
    └── summary_report.md
```

Generated outputs and raw/synthetic CSVs are git-ignored. Only `.gitkeep` files and
documentation are committed.

---

## Configuration

All settings are read from environment variables and an optional `.env` file. See
`.env.example` for the full list.

| variable | default | purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy` | main database |
| `TEST_DATABASE_URL` | `…/renewable_energy_test` | integration tests only |
| `INPUT_DIR` / `OUTPUT_DIR` | `data/raw` / `output` | input and generated files |
| `INSTALLATIONS_FILE` | `config/installations.csv` | optional capacity/name metadata |
| `INPUT_TIMEZONE` | `Europe/Berlin` | timezone for naive timestamps |
| `EXPECTED_INTERVAL_MINUTES` | `15` | expected sampling interval |
| `RESAMPLE_FREQUENCY` | `15min` | grid frequency; empty disables resampling |
| `MISSING_STRATEGY` | `interpolate` | `interpolate`, `ffill`, `none`, `drop` |
| `INTERPOLATION_MAX_GAP_MINUTES` | `30` | maximum internal gap to fill |
| `DUPLICATE_KEEP` | `first` | `first` or `last` |
| `PR_MIN_IRRADIANCE_W_M2` | `20` | daylight threshold for PR diagnostics |
| `PR_MIN_COVERAGE` | `0.8` | minimum paired coverage for PR |
| `DATA_PROVENANCE` | synthetic warning | label printed in every report/plot |

Validation thresholds (`POWER_*`, `IRRADIANCE_*`, `*_TEMPERATURE_*`) are listed in
`.env.example`.

Installation capacity is optional metadata. Without it, PR is skipped with an explicit
warning; ingestion, cleaning and other analytics still run.

---

## Database schema

| table | purpose |
|---|---|
| `installations` | installation codes, names, locations, capacity |
| `sensors` | sensor channel per installation (metric + unit) |
| `ingestion_runs` | one row per imported file, unique SHA-256, row counts |
| `measurements_raw` | append-only raw measurements, including invalid and duplicate rows |
| `rejected_records` | source rows rejected during ingestion, with raw JSON |
| `quality_reports` | per-file quality counts and JSON issue list |
| `measurements_processed` | deduplicated, gridded, possibly imputed values with status flags |

`measurements_raw` has a unique key on `(ingestion_run_id, source_row, metric)`.
`measurements_processed` is unique on `(installation_id, sensor_id, timestamp)` and is
replaced atomically per installation during a rebuild.

All writes use transactions. `init-db` never drops existing data.

---

## SQL queries

Plain, documented SQL lives in `sql/queries/`:

- `measurements_by_installation.sql`
- `measurements_by_time_range.sql`
- `retrieve_processed_measurements.sql`
- `daily_averages.sql`
- `monthly_averages.sql`
- `compare_installations.sql`
- `missing_measurement_intervals.sql`
- `raw_duplicates.sql`
- `quality_reports.sql`

Example:

```bash
psql "$DATABASE_URL" -v installation_code=INST_A \
  -f sql/queries/measurements_by_installation.sql
```

See `sql/queries/README.md` for psql variables. Energy calculations in SQL assume the
same 15-minute interval as the application.

---

## Tests

```bash
pytest
```

The suite covers ingestion, validation, cleaning/imputation, analytics and PR,
reporting, SQLite-backed database operations, duplicate-file protection and CLI
behaviour.

### PostgreSQL integration test

The integration test is skipped unless `TEST_DATABASE_URL` is set. It runs the real
CLI against PostgreSQL, including `init-db`, repeated `process` runs, duplicate-file
skipping, rebuilds after new data, `history` and `report`. For safety it refuses to run
against any database whose name does not contain `test`.

```bash
# PowerShell
$env:TEST_DATABASE_URL="postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy_test"
pytest

# bash
TEST_DATABASE_URL="postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy_test" pytest
```

The test drops and recreates the tables in the test database. Never point it at a
production database.

---

## Project structure

```
├── config/installations.csv      # synthetic example installation metadata
├── data/raw/                     # input CSVs (git-ignored)
├── output/                       # generated CSV/PNG/Markdown (git-ignored)
├── scripts/generate_synthetic_data.py
├── sql/queries/                  # documented analytical SQL
├── src/
│   ├── config.py                 # settings and metadata
│   ├── ingestion.py              # CSV parsing and long format
│   ├── validation.py             # quality statuses and reports
│   ├── cleaning.py               # dedupe, gridding, bounded imputation
│   ├── models.py                 # SQLAlchemy schema
│   ├── database.py               # transactions and persistence
│   ├── analytics.py              # statistics, energy, PR, trends
│   ├── reports.py                # CSV/PNG/Markdown outputs
│   └── main.py                   # CLI
├── tests/
├── docker-compose.yml            # PostgreSQL 16 only
├── requirements.txt
└── .env.example
```

---

## Assumptions and limitations

- Example data is synthetic. No result here describes a real PV system.
- Irradiance is assumed to be plane-of-array and AC power is assumed at the inverter
  output; both should be confirmed before using real data.
- Energy integration is rectangular and conservative for missing samples.
- PR excludes partial days/months below the coverage threshold.
- Multiple sensors for the same metric are averaged per timestamp; this is reported as
  a cleaning warning and is a simplification.
- Ambiguous local timestamps during the autumn DST change are interpreted as the first
  occurrence; non-existent spring times are shifted forward.
- Statistical trends are descriptive only. They do not establish module degradation.
- The pipeline is intentionally single-process and file-oriented. It is suitable for a
  research group's periodic CSV imports, not for high-frequency streaming ingestion.
