"""Command-line entry point.

Commands
--------
init-db          create the PostgreSQL schema
process          ingest, validate, clean, persist, analyse and report
process --skip-db  same pipeline without a database
history          show stored installations, ingestion runs and quality reports
report           regenerate analytics and reports from stored processed data
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from src.analytics import compute_analytics, missing_intervals_from_wide
from src.cleaning import CleanResult, clean_measurements
from src.config import (
    ConfigurationError,
    InstallationMeta,
    Settings,
    describe_database_url,
    load_installation_metadata,
    load_settings,
)
from src.database import (
    DatabaseError,
    create_engine_for_url,
    create_session_factory,
    file_already_imported,
    history_summary,
    init_db,
    load_ingestion_runs,
    load_installation_metadata as load_db_installation_metadata,
    load_processed_measurements,
    load_quality_reports,
    load_raw_measurements,
    persist_ingestion,
    replace_processed_measurements,
    require_schema,
    session_scope,
)
from src.ingestion import IngestionError, IngestionResult, load_csv
from src.reports import ReportOutputs, generate_reports
from src.validation import (
    QualityReport,
    ValidationResult,
    quality_report_frame,
    validate_measurements,
)

LOGGER = logging.getLogger("renewable_etl")


def _safe_stem(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    updates: dict[str, Any] = {}
    if getattr(args, "input_dir", None):
        updates["input_dir"] = Path(args.input_dir).expanduser().resolve()
    if getattr(args, "output_dir", None):
        updates["output_dir"] = Path(args.output_dir).expanduser().resolve()
    if getattr(args, "database_url", None):
        updates["database_url"] = args.database_url
    return replace(settings, **updates) if updates else settings


def _quality_frame(reports: Sequence[QualityReport]) -> pd.DataFrame:
    frames = [quality_report_frame(report) for report in reports]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _file_summary(rows: list[dict[str, Any]]) -> pd.DataFrame:
    columns = [
        "source_file",
        "status",
        "total_rows",
        "rejected_rows",
        "duplicate_rows",
        "message",
    ]
    summary = pd.DataFrame(rows)
    if summary.empty:
        return pd.DataFrame(columns=columns)
    for column in columns:
        if column not in summary:
            summary[column] = None
    return summary[columns]


def _write_file_outputs(
    ingestion: IngestionResult, validation: ValidationResult, settings: Settings
) -> None:
    stem = _safe_stem(ingestion.path)
    quality_report_frame(validation.report).to_csv(
        settings.csv_dir / f"quality_{stem}.csv", index=False, encoding="utf-8"
    )
    if not ingestion.rejected.empty:
        ingestion.rejected.to_csv(
            settings.csv_dir / f"rejected_{stem}.csv",
            index=False,
            encoding="utf-8",
        )


def _open_database(settings: Settings):
    engine = create_engine_for_url(settings.database_url)
    try:
        require_schema(engine)
    except Exception:
        engine.dispose()
        raise
    return engine, create_session_factory(engine)


def _print_report_outputs(outputs: ReportOutputs, settings: Settings) -> None:
    print(f"Report: {outputs.report_path}")
    print(f"CSV files: {len(outputs.csv_paths)} in {settings.csv_dir}")
    print(f"Plots: {len(outputs.plot_paths)} in {settings.plots_dir}")


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    engine = create_engine_for_url(settings.database_url)
    try:
        init_db(engine)
        tables = sorted(inspect(engine).get_table_names())
        print(f"Database schema ready at {describe_database_url(settings.database_url)}")
        print("Tables: " + ", ".join(tables))
        return 0
    except SQLAlchemyError as exc:
        LOGGER.error("Could not initialise the database: %s", exc)
        print(
            "Check DATABASE_URL and that PostgreSQL is running. "
            "Docker users can run `docker compose up -d`.",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()


def cmd_process(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    settings.ensure_directories()
    metadata = load_installation_metadata(settings.installations_file)
    capacities = {
        code: meta.capacity_kw
        for code, meta in metadata.items()
        if meta.capacity_kw is not None
    }
    if not metadata:
        LOGGER.warning(
            "No installation metadata found at %s; Performance Ratio will be skipped.",
            settings.installations_file,
        )

    files = sorted(settings.input_dir.glob("*.csv"))
    if not files:
        LOGGER.error("No CSV files found in %s", settings.input_dir)
        return 1

    engine = None
    factory = None
    if not args.skip_db:
        try:
            engine, factory = _open_database(settings)
        except DatabaseError as exc:
            LOGGER.error("%s", exc)
            return 1
        except SQLAlchemyError as exc:
            LOGGER.error("Cannot connect to the database: %s", exc)
            print(
                "Start PostgreSQL (`docker compose up -d`) and run "
                "`python -m src.main init-db`.",
                file=sys.stderr,
            )
            return 1

    validation_reports: list[QualityReport] = []
    long_parts: list[pd.DataFrame] = []
    touched: set[str] = set()
    file_rows: list[dict[str, Any]] = []
    processing_warnings: list[str] = []
    failures = 0
    successes = 0
    skipped = 0

    try:
        for path in files:
            LOGGER.info("Reading %s", path.name)
            try:
                ingestion = load_csv(path, settings)
            except IngestionError as exc:
                failures += 1
                file_rows.append(
                    {
                        "source_file": path.name,
                        "status": "failed",
                        "message": str(exc),
                    }
                )
                LOGGER.error("%s", exc)
                continue

            if factory is not None:
                with session_scope(factory) as session:
                    existing = file_already_imported(session, ingestion.file_sha256)
                if existing is not None:
                    skipped += 1
                    file_rows.append(
                        {
                            "source_file": path.name,
                            "status": "skipped_duplicate",
                            "total_rows": ingestion.stats["total_rows"],
                            "rejected_rows": ingestion.stats["rejected_rows"],
                            "duplicate_rows": ingestion.stats["duplicate_rows"],
                            "message": f"identical file already imported (run {existing.id})",
                        }
                    )
                    processing_warnings.append(
                        f"{path.name}: skipped because its SHA-256 was already imported."
                    )
                    LOGGER.info("Skipping %s: identical file already imported", path.name)
                    continue

            validation = validate_measurements(
                ingestion.long,
                settings,
                capacities,
                source_file=ingestion.source_file,
                total_records=ingestion.stats["total_rows"],
                rejected_records=ingestion.stats["rejected_rows"],
            )
            validation_reports.append(validation.report)
            _write_file_outputs(ingestion, validation, settings)

            run_id: int | None = None
            if factory is not None:
                try:
                    with session_scope(factory) as session:
                        run_id = persist_ingestion(
                            session, ingestion, validation, metadata
                        )
                except (DatabaseError, SQLAlchemyError) as exc:
                    failures += 1
                    file_rows.append(
                        {
                            "source_file": path.name,
                            "status": "failed",
                            "total_rows": ingestion.stats["total_rows"],
                            "rejected_rows": ingestion.stats["rejected_rows"],
                            "duplicate_rows": ingestion.stats["duplicate_rows"],
                            "message": f"database error: {exc}",
                        }
                    )
                    LOGGER.error("Could not persist %s: %s", path.name, exc)
                    continue
                if run_id is None:
                    skipped += 1
                    file_rows.append(
                        {
                            "source_file": path.name,
                            "status": "skipped_duplicate",
                            "total_rows": ingestion.stats["total_rows"],
                            "rejected_rows": ingestion.stats["rejected_rows"],
                            "duplicate_rows": ingestion.stats["duplicate_rows"],
                            "message": "identical file already imported",
                        }
                    )
                    continue

            long_parts.append(validation.long)
            touched.update(validation.long["installation_id"].astype(str).unique())
            successes += 1
            file_rows.append(
                {
                    "source_file": path.name,
                    "status": "processed",
                    "total_rows": ingestion.stats["total_rows"],
                    "rejected_rows": ingestion.stats["rejected_rows"],
                    "duplicate_rows": ingestion.stats["duplicate_rows"],
                    "message": (
                        f"ingestion run {run_id}" if run_id is not None else "skip-db mode"
                    ),
                }
            )
            LOGGER.info(
                "%s: %s rows, %s rejected, %s duplicates, valid=%s suspicious=%s "
                "invalid=%s missing=%s",
                path.name,
                ingestion.stats["total_rows"],
                ingestion.stats["rejected_rows"],
                ingestion.stats["duplicate_rows"],
                validation.report.valid_measurements,
                validation.report.suspicious_measurements,
                validation.report.invalid_measurements,
                validation.report.missing_measurements,
            )

        if args.skip_db:
            processing_warnings.append(
                "skip-db mode has no persistent file-hash ledger; re-running will "
                "re-ingest files."
            )
            if not long_parts:
                LOGGER.error("No files were processed successfully.")
                return 1
            long = pd.concat(long_parts, ignore_index=True)
            cleaned = clean_measurements(long, settings, metadata)
            analytics = compute_analytics(cleaned.long, settings, metadata)
            quality = _quality_frame(validation_reports)
            outputs = generate_reports(
                analytics,
                quality,
                cleaned.missing_intervals,
                settings,
                file_summary=_file_summary(file_rows),
                processing_warnings=[*processing_warnings, *cleaned.warnings],
                processed_long=cleaned.long,
            )
            _print_report_outputs(outputs, settings)
            print(
                f"Processed {successes} file(s), skipped {skipped}, failed {failures}; "
                f"{len(analytics.wide)} installation(s)."
            )
            return 0 if successes else 1

        # Database mode: rebuild processed data for the affected installations from
        # all raw rows stored for them.  If every file was already imported, rebuild
        # all installations so that a failed earlier processing step can be recovered.
        assert factory is not None
        if touched:
            rebuild_codes = sorted(touched)
        elif successes == 0 and failures == 0:
            with session_scope(factory) as session:
                from src.database import load_installations

                rebuild_codes = load_installations(session)["code"].astype(str).tolist()
        else:
            rebuild_codes = []

        if not rebuild_codes:
            LOGGER.warning("No new measurements were imported; nothing to rebuild.")
            return 0 if failures == 0 else 1

        LOGGER.info(
            "Rebuilding processed measurements for: %s", ", ".join(rebuild_codes)
        )
        with session_scope(factory) as session:
            raw = load_raw_measurements(session, rebuild_codes)
            db_metadata = load_db_installation_metadata(session)
            db_quality = load_quality_reports(session)
        if raw.empty:
            LOGGER.warning("No raw measurements found for rebuild.")
            return 0 if failures == 0 else 1

        db_capacities = {
            code: meta.capacity_kw
            for code, meta in db_metadata.items()
            if meta.capacity_kw is not None
        }
        raw_validation = validate_measurements(raw, settings, db_capacities)
        cleaned = clean_measurements(raw_validation.long, settings, db_metadata)
        with session_scope(factory) as session:
            inserted = replace_processed_measurements(
                session, cleaned.long, rebuild_codes
            )
        LOGGER.info("Stored %s processed measurements", inserted)

        analytics = compute_analytics(cleaned.long, settings, db_metadata)
        outputs = generate_reports(
            analytics,
            db_quality,
            cleaned.missing_intervals,
            settings,
            file_summary=_file_summary(file_rows),
            processing_warnings=[*processing_warnings, *cleaned.warnings],
            processed_long=None,
        )
        _print_report_outputs(outputs, settings)
        print(
            f"Processed {successes} file(s), skipped {skipped}, failed {failures}; "
            f"rebuilt {len(rebuild_codes)} installation(s) in PostgreSQL."
        )
        return 0 if (successes or skipped) and failures == 0 else 1
    finally:
        if engine is not None:
            engine.dispose()


def cmd_history(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    try:
        engine, factory = _open_database(settings)
    except DatabaseError as exc:
        LOGGER.error("%s", exc)
        return 1
    except SQLAlchemyError as exc:
        LOGGER.error("Cannot connect to the database: %s", exc)
        return 1
    try:
        with session_scope(factory) as session:
            summary = history_summary(session)
    finally:
        engine.dispose()

    installations = summary["installations"]
    runs = summary["ingestion_runs"]
    reports = summary["quality_reports"]
    if installations.empty:
        print("No installations have been processed yet.")
        return 0
    limit = max(1, args.limit)
    print(f"Database: {describe_database_url(settings.database_url)}")
    print("\nInstallations and measurement counts")
    print(
        installations.head(limit).to_string(
            index=False, columns=[
                "code",
                "name",
                "location",
                "capacity_kw",
                "raw_measurements",
                "processed_measurements",
                "imputed_measurements",
                "raw_first_timestamp",
                "raw_last_timestamp",
            ]
        )
    )
    if not runs.empty:
        print("\nIngestion runs")
        print(
            runs.head(limit).to_string(
                index=False,
                columns=[
                    "id",
                    "source_file",
                    "status",
                    "total_rows",
                    "valid_rows",
                    "rejected_rows",
                    "imported_at",
                ],
            )
        )
    if not reports.empty:
        print("\nQuality reports")
        print(
            reports.head(limit).to_string(
                index=False,
                columns=[
                    "source_file",
                    "total_records",
                    "valid_measurements",
                    "invalid_measurements",
                    "suspicious_measurements",
                    "missing_measurements",
                    "duplicate_records",
                    "rejected_records",
                    "created_at",
                ],
            )
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    settings.ensure_directories()
    try:
        engine, factory = _open_database(settings)
    except DatabaseError as exc:
        LOGGER.error("%s", exc)
        return 1
    except SQLAlchemyError as exc:
        LOGGER.error("Cannot connect to the database: %s", exc)
        return 1

    requested = args.installation or None
    try:
        with session_scope(factory) as session:
            processed = load_processed_measurements(session, requested)
            metadata = load_db_installation_metadata(session)
            quality = load_quality_reports(session)
            runs = load_ingestion_runs(session)
    finally:
        engine.dispose()

    if processed.empty:
        LOGGER.error("No stored processed measurements were found.")
        return 1
    if requested:
        available = set(processed["installation_id"].astype(str))
        missing = set(requested) - available
        if missing:
            LOGGER.warning("No processed data for: %s", ", ".join(sorted(missing)))

    analytics = compute_analytics(processed, settings, metadata)
    missing_intervals = missing_intervals_from_wide(
        analytics.wide, settings.cleaning.expected_interval_minutes
    )
    if not runs.empty:
        file_summary = runs.copy()
        file_summary["status"] = "stored"
        file_summary["message"] = "previously imported"
        if "duplicate_rows" not in file_summary:
            file_summary["duplicate_rows"] = pd.NA
    else:
        file_summary = None
    outputs = generate_reports(
        analytics,
        quality,
        missing_intervals,
        settings,
        file_summary=file_summary,
        processing_warnings=analytics.warnings,
        processed_long=None,
    )
    _print_report_outputs(outputs, settings)
    print(f"Report regenerated for {len(analytics.wide)} installation(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Renewable energy time-series ETL and analytics pipeline",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="create the database schema")
    init_parser.add_argument("--database-url", help="override DATABASE_URL")
    init_parser.set_defaults(func=cmd_init_db)

    process_parser = subparsers.add_parser(
        "process", help="ingest, validate, clean, store and report"
    )
    process_parser.add_argument(
        "--skip-db", action="store_true", help="run without PostgreSQL"
    )
    process_parser.add_argument("--input-dir", help="directory containing CSV files")
    process_parser.add_argument("--output-dir", help="output directory")
    process_parser.add_argument("--database-url", help="override DATABASE_URL")
    process_parser.set_defaults(func=cmd_process)

    history_parser = subparsers.add_parser(
        "history", help="show stored processing history"
    )
    history_parser.add_argument("--limit", type=int, default=20)
    history_parser.add_argument("--database-url", help="override DATABASE_URL")
    history_parser.set_defaults(func=cmd_history)

    report_parser = subparsers.add_parser(
        "report", help="regenerate reports from stored processed data"
    )
    report_parser.add_argument("--output-dir", help="output directory")
    report_parser.add_argument("--database-url", help="override DATABASE_URL")
    report_parser.add_argument(
        "--installation",
        action="append",
        help="only report this installation code (repeatable)",
    )
    report_parser.set_defaults(func=cmd_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    try:
        return int(args.func(args))
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2
    except DatabaseError as exc:
        LOGGER.error("%s", exc)
        return 2
    except SQLAlchemyError as exc:
        LOGGER.error("Database error: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.error("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
