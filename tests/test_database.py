"""Database tests.

The first group runs against a temporary SQLite file and covers transactional
persistence, duplicate-file protection and processed-data rebuilds.

The PostgreSQL integration test runs the real CLI against ``TEST_DATABASE_URL``.
It is skipped when that variable is absent and refuses to run against a database
whose name does not contain ``test``.  It must be executed (not merely written)
before claiming the PostgreSQL layer works.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from src.config import InstallationMeta, Settings
from src.cleaning import clean_measurements
from src.database import (
    count_rows,
    create_engine_for_url,
    create_session_factory,
    file_already_imported,
    history_summary,
    init_db,
    load_processed_measurements,
    load_raw_measurements,
    session_scope,
    persist_ingestion,
    replace_processed_measurements,
)
from src.ingestion import load_csv
from src.models import (
    Base,
    IngestionRun,
    Installation,
    MeasurementProcessed,
    MeasurementRaw,
    QualityReport,
)
from tests.conftest import day_rows, measurement_row, sha256, write_csv


@pytest.fixture()
def sqlite_engine(tmp_path: Path):
    engine = create_engine_for_url(f"sqlite+pysqlite:///{tmp_path / 'unit.db'}")
    init_db(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _ingest_and_validate(settings: Settings, path: Path, capacity: float = 10.0):
    ingestion = load_csv(path, settings)
    from src.validation import validate_measurements

    validation = validate_measurements(
        ingestion.long,
        settings,
        {"INST_A": capacity},
        source_file=ingestion.source_file,
        total_records=ingestion.stats["total_rows"],
        rejected_records=ingestion.stats["rejected_rows"],
    )
    return ingestion, validation


def _count(engine, model) -> int:
    with session_scope(create_session_factory(engine)) as session:
        return count_rows(session, model)


def test_persist_and_duplicate_protection(
    settings: Settings, tmp_path: Path, sqlite_engine
) -> None:
    path = write_csv(
        tmp_path / "db.csv",
        [
            measurement_row("2023-01-01T00:00:00+00:00", power=1.0),
            measurement_row("2023-01-01T00:15:00+00:00", power=2.0),
            measurement_row("2023-01-01T00:15:00+00:00", power=2.0),  # duplicate
        ],
    )
    ingestion, validation = _ingest_and_validate(settings, path)
    factory = create_session_factory(sqlite_engine)

    with session_scope(factory) as session:
        run_id = persist_ingestion(
            session,
            ingestion,
            validation,
            {"INST_A": InstallationMeta("INST_A", "North", "Campus", 10.0)},
        )
    assert run_id is not None

    raw_rows = len(ingestion.long)
    assert _count(sqlite_engine, MeasurementRaw) == raw_rows
    assert _count(sqlite_engine, IngestionRun) == 1
    assert _count(sqlite_engine, Installation) == 1
    assert _count(sqlite_engine, QualityReport) == 1

    # The duplicate remains stored in the raw layer.
    with session_scope(factory) as session:
        duplicates = session.execute(
            select(func.count())
            .select_from(MeasurementRaw)
            .where(MeasurementRaw.metric == "active_power_kw")
        ).scalar_one()
    assert duplicates == 3

    # Importing the identical bytes again must be rejected.
    ingestion_again, validation_again = _ingest_and_validate(settings, path)
    with session_scope(factory) as session:
        assert file_already_imported(session, ingestion_again.file_sha256) is not None
        assert (
            persist_ingestion(
                session,
                ingestion_again,
                validation_again,
                {"INST_A": InstallationMeta("INST_A", "North", "Campus", 10.0)},
            )
            is None
        )
    assert _count(sqlite_engine, MeasurementRaw) == raw_rows
    assert _count(sqlite_engine, IngestionRun) == 1


def test_processed_rebuild_is_idempotent(
    settings: Settings, tmp_path: Path, sqlite_engine
) -> None:
    path = write_csv(tmp_path / "db.csv", day_rows(periods=8))
    ingestion, validation = _ingest_and_validate(settings, path)
    factory = create_session_factory(sqlite_engine)

    with session_scope(factory) as session:
        persist_ingestion(
            session,
            ingestion,
            validation,
            {"INST_A": InstallationMeta("INST_A", "North", "Campus", 10.0)},
        )
        raw = load_raw_measurements(session)
    assert len(raw) == len(ingestion.long)

    cleaned = clean_measurements(validation.long, settings, {})
    with session_scope(factory) as session:
        written = replace_processed_measurements(session, cleaned.long, ["INST_A"])
    assert written == len(cleaned.long)
    assert _count(sqlite_engine, MeasurementProcessed) == written

    # Replacing again must not duplicate rows.
    with session_scope(factory) as session:
        replace_processed_measurements(session, cleaned.long, ["INST_A"])
    assert _count(sqlite_engine, MeasurementProcessed) == written

    with session_scope(factory) as session:
        processed = load_processed_measurements(session, ["INST_A"])
        summary = history_summary(session)
    assert len(processed) == written
    assert not summary["installations"].empty
    assert summary["installations"].iloc[0]["raw_measurements"] == len(ingestion.long)


def test_unknown_installation_cannot_be_written(
    settings: Settings, tmp_path: Path, sqlite_engine
) -> None:
    path = write_csv(tmp_path / "db.csv", day_rows(periods=2))
    ingestion, validation = _ingest_and_validate(settings, path)
    factory = create_session_factory(sqlite_engine)
    with session_scope(factory) as session:
        persist_ingestion(session, ingestion, validation, {})
        cleaned = clean_measurements(validation.long, settings, {})
        with pytest.raises(Exception):
            replace_processed_measurements(session, cleaned.long, ["MISSING"])


def _test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; PostgreSQL integration not run")
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must point at PostgreSQL")
    if "test" not in (parsed.database or "").lower():
        pytest.fail(
            "Refusing to run destructive integration tests: database name must "
            "contain 'test'"
        )
    return url


def test_postgresql_end_to_end_cli(monkeypatch, tmp_path: Path) -> None:
    from src.main import main

    url = _test_database_url()
    engine = create_engine_for_url(url)
    try:
        Base.metadata.drop_all(engine)
    except Exception as exc:  # noqa: BLE001 - reported as a clear skip/fail
        pytest.fail(f"Cannot reach TEST_DATABASE_URL: {exc}")
    Base.metadata.create_all(engine)

    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "output"
    config_dir = tmp_path / "config"
    raw_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    installations = config_dir / "installations.csv"
    installations.write_text(
        "installation_id,name,location,capacity_kw\n"
        "INST_A,North Roof Array,Research Campus North,10.0\n",
        encoding="utf-8",
    )

    first = write_csv(
        raw_dir / "a_first.csv",
        [
            measurement_row("2023-01-01T00:00:00+00:00", power=1.0),
            measurement_row("2023-01-01T00:15:00+00:00", power=1.0),
            measurement_row("2023-01-01T00:30:00+00:00", power=1.0),
            measurement_row("2023-01-01T00:45:00+00:00", power=1.0),
        ],
    )
    duplicate = write_csv(
        raw_dir / "b_duplicate.csv",
        [
            measurement_row("2023-01-01T00:00:00+00:00", power=1.0),
            measurement_row("2023-01-01T00:15:00+00:00", power=1.0),
            measurement_row("2023-01-01T00:30:00+00:00", power=1.0),
            measurement_row("2023-01-01T00:45:00+00:00", power=1.0),
        ],
    )
    assert sha256(first) == sha256(duplicate)
    third = write_csv(
        raw_dir / "c_third.csv",
        [
            measurement_row("2023-01-01T01:00:00+00:00", power=1.0),
            measurement_row("2023-01-01T01:15:00+00:00", power=1.0),
            measurement_row("2023-01-01T01:30:00+00:00", power=1.0),
            measurement_row("2023-01-01T01:45:00+00:00", power=1.0),
        ],
    )
    hashes = {path.name: sha256(path) for path in (first, duplicate, third)}

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("INPUT_DIR", str(raw_dir))
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("INSTALLATIONS_FILE", str(installations))
    monkeypatch.setenv("DATA_PROVENANCE", "SYNTHETIC EXAMPLE DATA - test run")

    try:
        assert main(["init-db"]) == 0
        assert main(["process"]) == 0
        assert _count(engine, IngestionRun) == 2  # duplicate file was skipped
        assert _count(engine, MeasurementRaw) == 8 * 4
        assert _count(engine, MeasurementProcessed) == 8 * 4
        assert _count(engine, QualityReport) == 2

        # Re-running must skip all files and leave stored data unchanged.
        assert main(["process"]) == 0
        assert _count(engine, IngestionRun) == 2
        assert _count(engine, MeasurementRaw) == 8 * 4
        assert _count(engine, MeasurementProcessed) == 8 * 4

        # A genuinely new file for the same installation is appended and the
        # processed layer is rebuilt from every stored raw row.
        write_csv(
            raw_dir / "d_fourth.csv",
            [
                measurement_row("2023-01-01T02:00:00+00:00", power=1.0),
                measurement_row("2023-01-01T02:15:00+00:00", power=1.0),
                measurement_row("2023-01-01T02:30:00+00:00", power=1.0),
                measurement_row("2023-01-01T02:45:00+00:00", power=1.0),
            ],
        )
        assert main(["process"]) == 0
        assert _count(engine, IngestionRun) == 3
        assert _count(engine, MeasurementRaw) == 12 * 4
        assert _count(engine, MeasurementProcessed) == 12 * 4

        assert main(["history", "--limit", "5"]) == 0
        assert main(["report"]) == 0
        assert (output_dir / "reports" / "summary_report.md").exists()

        # Raw input files must never be modified.
        assert {path.name: sha256(path) for path in (first, duplicate, third)} == hashes
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
