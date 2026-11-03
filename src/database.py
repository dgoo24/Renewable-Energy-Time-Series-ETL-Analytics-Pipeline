"""Database operations.

All writes are performed in explicit transactions.  The raw layer is append-only
per ingestion run; processed rows for an installation are replaced atomically when
they are rebuilt from raw data.  Rebuilding from raw is what keeps processed data
consistent when a new file is added for an existing installation.

Connection string example:
    postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import Integer, create_engine, delete, func, inspect, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from src.config import InstallationMeta
from src.ingestion import IngestionResult
from src.models import (
    Base,
    IngestionRun,
    Installation,
    MeasurementProcessed,
    MeasurementRaw,
    QualityReport,
    RejectedRecord,
    Sensor,
)
from src.validation import QualityReport as QualityReportData
from src.validation import ValidationResult


class DatabaseError(RuntimeError):
    """Raised for expected database setup or integrity problems."""


def create_engine_for_url(url: str, echo: bool = False) -> Engine:
    options: dict[str, Any] = {"echo": echo, "future": True}
    if not url.startswith("sqlite"):
        options["pool_pre_ping"] = True
    return create_engine(url, **options)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """Create all tables.  Safe to call repeatedly; never drops data."""

    Base.metadata.create_all(engine)


def schema_exists(engine: Engine) -> bool:
    inspector = inspect(engine)
    return inspector.has_table("installations") and inspector.has_table(
        "measurements_processed"
    )


def require_schema(engine: Engine) -> None:
    if not schema_exists(engine):
        raise DatabaseError(
            "Database schema is missing. Run `python -m src.main init-db` first."
        )


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _write_transaction(session: Session):
    """Return a transaction context that works inside or outside a session scope.

    Application code usually opens a fresh session, but tests and callers may run
    a read query first (which autobegins a transaction).  A nested SAVEPOINT keeps
    the write atomic in both cases.
    """

    if session.in_transaction():
        return session.begin_nested()
    return session.begin()


def file_already_imported(session: Session, file_sha256: str) -> IngestionRun | None:
    return session.execute(
        select(IngestionRun).where(IngestionRun.file_sha256 == file_sha256)
    ).scalar_one_or_none()


def _get_or_create_installation(
    session: Session, code: str, meta: InstallationMeta | None
) -> Installation:
    installation = session.execute(
        select(Installation).where(Installation.code == code)
    ).scalar_one_or_none()
    if installation is None:
        installation = Installation(code=code)
        session.add(installation)
        session.flush()
    if meta is not None:
        if meta.name is not None:
            installation.name = meta.name
        if meta.location is not None:
            installation.location = meta.location
        if meta.capacity_kw is not None:
            installation.capacity_kw = meta.capacity_kw
    return installation


def _get_or_create_sensor(
    session: Session,
    installation: Installation,
    code: str,
    metric: str,
    unit: str,
    description: str | None = None,
) -> Sensor:
    sensor = session.execute(
        select(Sensor).where(
            Sensor.installation_id == installation.id, Sensor.code == code
        )
    ).scalar_one_or_none()
    if sensor is None:
        sensor = Sensor(
            installation_id=installation.id,
            code=code,
            sensor_type=metric,
            unit=unit,
            description=description,
        )
        session.add(sensor)
        session.flush()
    return sensor


def _chunks(records: Sequence[dict[str, Any]], size: int = 5000) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(records), size):
        yield list(records[start : start + size])


def persist_ingestion(
    session: Session,
    ingestion: IngestionResult,
    validation: ValidationResult,
    metadata: Mapping[str, InstallationMeta],
) -> int | None:
    """Persist one file atomically.

    Returns the ingestion-run id, or ``None`` when the identical file (same
    SHA-256) was already imported.  Rejected rows are stored separately; raw
    duplicate rows are preserved.
    """

    with _write_transaction(session):
        existing = file_already_imported(session, ingestion.file_sha256)
        if existing is not None:
            return None

        installation_codes = sorted(ingestion.long["installation_id"].unique().tolist())
        installations: dict[str, Installation] = {}
        for code in installation_codes:
            installations[code] = _get_or_create_installation(
                session, str(code), metadata.get(str(code))
            )

        run = IngestionRun(
            source_file=ingestion.source_file,
            file_sha256=ingestion.file_sha256,
            status="completed",
            total_rows=int(ingestion.stats.get("total_rows", 0)),
            valid_rows=int(ingestion.stats.get("valid_rows", 0)),
            rejected_rows=int(ingestion.stats.get("rejected_rows", 0)),
        )
        session.add(run)
        session.flush()

        sensors: dict[tuple[str, str], Sensor] = {}
        for row in (
            ingestion.long[
                ["installation_id", "sensor_code", "metric", "unit", "sensor_id"]
            ]
            .drop_duplicates()
            .itertuples(index=False)
        ):
            key = (str(row.installation_id), str(row.sensor_code))
            if key in sensors:
                continue
            sensors[key] = _get_or_create_sensor(
                session,
                installations[str(row.installation_id)],
                str(row.sensor_code),
                str(row.metric),
                str(row.unit),
                description=f"Channel {row.metric} from source sensor {row.sensor_id}",
            )

        raw_rows: list[dict[str, Any]] = []
        for row in ingestion.long.itertuples(index=False):
            installation = installations[str(row.installation_id)]
            sensor = sensors[(str(row.installation_id), str(row.sensor_code))]
            raw_rows.append(
                {
                    "ingestion_run_id": run.id,
                    "installation_id": installation.id,
                    "sensor_id": sensor.id,
                    "timestamp": row.timestamp.to_pydatetime()
                    if hasattr(row.timestamp, "to_pydatetime")
                    else row.timestamp,
                    "metric": str(row.metric),
                    "measured_value": None
                    if pd.isna(row.value)
                    else float(row.value),
                    "raw_value_text": str(row.raw_value_text),
                    "source_file": ingestion.source_file,
                    "source_row": int(row.source_row),
                }
            )
        for chunk in _chunks(raw_rows):
            session.execute(insert(MeasurementRaw), chunk)

        rejected_rows = [
            {
                "ingestion_run_id": run.id,
                "source_file": ingestion.source_file,
                "source_row": int(row.source_row),
                "raw_data": str(row.raw_data),
                "reason": str(row.reason),
            }
            for row in ingestion.rejected.itertuples(index=False)
        ]
        if rejected_rows:
            session.execute(insert(RejectedRecord), rejected_rows)

        report = validation.report
        session.add(
            QualityReport(
                ingestion_run_id=run.id,
                source_file=report.source_file,
                total_records=report.total_records,
                total_measurements=report.total_measurements,
                valid_measurements=report.valid_measurements,
                invalid_measurements=report.invalid_measurements,
                suspicious_measurements=report.suspicious_measurements,
                missing_measurements=report.missing_measurements,
                missing_values=report.missing_values,
                duplicate_records=report.duplicate_records,
                duplicate_measurements=report.duplicate_measurements,
                missing_intervals=report.missing_intervals,
                missing_slots=report.missing_slots,
                irregular_intervals=report.irregular_intervals,
                rejected_records=report.rejected_records,
                issues=report.issues_json(),
            )
        )
        return run.id


def replace_processed_measurements(
    session: Session,
    processed_long: pd.DataFrame,
    installation_codes: Sequence[str] | None = None,
) -> int:
    """Atomically replace processed rows for the affected installations."""

    if processed_long.empty and not installation_codes:
        return 0
    codes = (
        sorted(installation_codes)
        if installation_codes is not None
        else sorted(processed_long["installation_id"].unique().tolist())
    )
    if not codes:
        return 0

    with _write_transaction(session):
        installation_rows = session.execute(
            select(Installation).where(Installation.code.in_(codes))
        ).scalars()
        installations = {item.code: item for item in installation_rows}
        missing = set(codes) - set(installations)
        if missing:
            raise DatabaseError(
                f"Cannot store processed data for unknown installation(s): "
                f"{sorted(missing)}"
            )
        installation_ids = [item.id for item in installations.values()]
        session.execute(
            delete(MeasurementProcessed).where(
                MeasurementProcessed.installation_id.in_(installation_ids)
            )
        )

        if processed_long.empty:
            return 0

        sensor_rows = session.execute(
            select(Sensor).where(Sensor.installation_id.in_(installation_ids))
        ).scalars()
        sensors = {
            (sensor.installation_id, sensor.code): sensor.id for sensor in sensor_rows
        }
        missing_sensors = {
            (str(row.installation_id), str(row.sensor_code))
            for row in processed_long[
                ["installation_id", "sensor_code"]
            ].drop_duplicates().itertuples(index=False)
            if (installations[str(row.installation_id)].id, str(row.sensor_code))
            not in sensors
        }
        if missing_sensors:
            raise DatabaseError(
                "Cannot store processed data; sensor rows are missing: "
                f"{sorted(missing_sensors)[:5]}"
            )

        rows: list[dict[str, Any]] = []
        for row in processed_long.itertuples(index=False):
            installation_id = installations[str(row.installation_id)].id
            sensor_id = sensors[(installation_id, str(row.sensor_code))]
            rows.append(
                {
                    "installation_id": installation_id,
                    "sensor_id": sensor_id,
                    "timestamp": row.timestamp.to_pydatetime()
                    if hasattr(row.timestamp, "to_pydatetime")
                    else row.timestamp,
                    "processed_value": None
                    if pd.isna(row.processed_value)
                    else float(row.processed_value),
                    "original_value": None
                    if pd.isna(row.original_value)
                    else float(row.original_value),
                    "quality_status": str(row.quality_status),
                    "source_quality_status": str(row.source_quality_status),
                    "is_imputed": bool(row.is_imputed),
                }
            )
        for chunk in _chunks(rows):
            session.execute(insert(MeasurementProcessed), chunk)
        return len(rows)


def load_raw_measurements(
    session: Session, installation_codes: Sequence[str] | None = None
) -> pd.DataFrame:
    """Load raw measurements for rebuilding processed data."""

    stmt = (
        select(
            Installation.code.label("installation_id"),
            Sensor.code.label("sensor_code"),
            Sensor.sensor_type.label("metric"),
            Sensor.unit.label("unit"),
            MeasurementRaw.timestamp,
            MeasurementRaw.measured_value.label("value"),
            MeasurementRaw.raw_value_text,
            MeasurementRaw.source_file,
            MeasurementRaw.source_row,
            MeasurementRaw.ingestion_run_id,
        )
        .join(Sensor, MeasurementRaw.sensor_id == Sensor.id)
        .join(Installation, MeasurementRaw.installation_id == Installation.id)
        .order_by(MeasurementRaw.timestamp, MeasurementRaw.id)
    )
    if installation_codes:
        stmt = stmt.where(Installation.code.in_(list(installation_codes)))
    frame = pd.read_sql(stmt, session.connection())
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "installation_id",
                "sensor_id",
                "sensor_code",
                "metric",
                "unit",
                "timestamp",
                "value",
                "raw_value_text",
                "value_parse_error",
                "source_file",
                "source_row",
                "ingestion_run_id",
            ]
        )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["sensor_id"] = frame["sensor_code"].str.split("::").str[0]
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["raw_value_text"] = frame["raw_value_text"].fillna("")
    frame["value_parse_error"] = frame["value"].isna() & (
        frame["raw_value_text"].astype(str).str.strip() != ""
    )
    return frame


def load_processed_measurements(
    session: Session, installation_codes: Sequence[str] | None = None
) -> pd.DataFrame:
    """Load processed measurements for reporting from stored data."""

    stmt = (
        select(
            Installation.code.label("installation_id"),
            Sensor.code.label("sensor_code"),
            Sensor.sensor_type.label("metric"),
            Sensor.unit.label("unit"),
            MeasurementProcessed.timestamp,
            MeasurementProcessed.processed_value,
            MeasurementProcessed.original_value,
            MeasurementProcessed.quality_status,
            MeasurementProcessed.is_imputed,
            MeasurementProcessed.source_quality_status,
        )
        .join(Sensor, MeasurementProcessed.sensor_id == Sensor.id)
        .join(Installation, MeasurementProcessed.installation_id == Installation.id)
        .order_by(MeasurementProcessed.timestamp, Sensor.code)
    )
    if installation_codes:
        stmt = stmt.where(Installation.code.in_(list(installation_codes)))
    frame = pd.read_sql(stmt, session.connection())
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "installation_id",
                "sensor_id",
                "sensor_code",
                "metric",
                "unit",
                "timestamp",
                "processed_value",
                "original_value",
                "quality_status",
                "is_imputed",
                "source_quality_status",
            ]
        )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["sensor_id"] = frame["sensor_code"].str.split("::").str[0]
    frame["processed_value"] = pd.to_numeric(
        frame["processed_value"], errors="coerce"
    )
    frame["original_value"] = pd.to_numeric(frame["original_value"], errors="coerce")
    frame["is_imputed"] = frame["is_imputed"].astype(bool)
    return frame


def load_installations(session: Session) -> pd.DataFrame:
    stmt = select(
        Installation.code,
        Installation.name,
        Installation.location,
        Installation.capacity_kw,
        Installation.created_at,
    ).order_by(Installation.code)
    frame = pd.read_sql(stmt, session.connection())
    if not frame.empty:
        frame["code"] = frame["code"].astype(str)
    return frame


def load_capacities(session: Session) -> dict[str, float]:
    frame = load_installations(session)
    if frame.empty:
        return {}
    result: dict[str, float] = {}
    for row in frame.itertuples(index=False):
        if pd.notna(row.capacity_kw):
            result[str(row.code)] = float(row.capacity_kw)
    return result


def load_installation_metadata(session: Session) -> dict[str, InstallationMeta]:
    frame = load_installations(session)
    result: dict[str, InstallationMeta] = {}
    for row in frame.itertuples(index=False):
        result[str(row.code)] = InstallationMeta(
            installation_id=str(row.code),
            name=None if pd.isna(row.name) else str(row.name),
            location=None if pd.isna(row.location) else str(row.location),
            capacity_kw=None
            if pd.isna(row.capacity_kw)
            else float(row.capacity_kw),
        )
    return result


def load_ingestion_runs(session: Session) -> pd.DataFrame:
    stmt = select(
        IngestionRun.id,
        IngestionRun.source_file,
        IngestionRun.file_sha256,
        IngestionRun.status,
        IngestionRun.total_rows,
        IngestionRun.valid_rows,
        IngestionRun.rejected_rows,
        IngestionRun.imported_at,
    ).order_by(IngestionRun.imported_at.desc(), IngestionRun.id.desc())
    return pd.read_sql(stmt, session.connection())


def load_quality_reports(session: Session) -> pd.DataFrame:
    stmt = (
        select(
            QualityReport.source_file,
            QualityReport.total_records,
            QualityReport.total_measurements,
            QualityReport.valid_measurements,
            QualityReport.invalid_measurements,
            QualityReport.suspicious_measurements,
            QualityReport.missing_measurements,
            QualityReport.missing_values,
            QualityReport.duplicate_records,
            QualityReport.duplicate_measurements,
            QualityReport.missing_intervals,
            QualityReport.missing_slots,
            QualityReport.irregular_intervals,
            QualityReport.rejected_records,
            QualityReport.issues,
            QualityReport.created_at,
            IngestionRun.file_sha256,
        )
        .join(
            IngestionRun,
            QualityReport.ingestion_run_id == IngestionRun.id,
            isouter=True,
        )
        .order_by(QualityReport.created_at.desc(), QualityReport.id.desc())
    )
    return pd.read_sql(stmt, session.connection())


def history_summary(session: Session) -> dict[str, pd.DataFrame]:
    installations = load_installations(session)
    raw_counts = pd.read_sql(
        select(
            Installation.code,
            func.count(MeasurementRaw.id).label("raw_measurements"),
            func.min(MeasurementRaw.timestamp).label("raw_first_timestamp"),
            func.max(MeasurementRaw.timestamp).label("raw_last_timestamp"),
        )
        .join(Installation, MeasurementRaw.installation_id == Installation.id)
        .group_by(Installation.code)
        .order_by(Installation.code),
        session.connection(),
    )
    processed_counts = pd.read_sql(
        select(
            Installation.code,
            func.count(MeasurementProcessed.id).label("processed_measurements"),
            func.sum(
                func.cast(MeasurementProcessed.is_imputed, Integer)
            ).label("imputed_measurements"),
            func.min(MeasurementProcessed.timestamp).label(
                "processed_first_timestamp"
            ),
            func.max(MeasurementProcessed.timestamp).label(
                "processed_last_timestamp"
            ),
        )
        .join(Installation, MeasurementProcessed.installation_id == Installation.id)
        .group_by(Installation.code)
        .order_by(Installation.code),
        session.connection(),
    )
    summary = installations.merge(raw_counts, on="code", how="left").merge(
        processed_counts, on="code", how="left"
    )
    return {
        "installations": summary,
        "ingestion_runs": load_ingestion_runs(session),
        "quality_reports": load_quality_reports(session),
    }


def get_installation_summary(session: Session) -> pd.DataFrame:
    return history_summary(session)["installations"]


def count_rows(session: Session, model: Any) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def database_health(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
        return True
    except SQLAlchemyError:
        return False
