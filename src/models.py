"""SQLAlchemy models for the small research database.

The schema follows a layered design:

* raw measurements are immutable and may contain duplicates or invalid values;
* processed measurements are derived, unique per sensor/timestamp, and rebuildable;
* ingestion runs and quality reports document what happened to each source file;
* rejected rows are preserved in their own table.

No PostgreSQL-specific column types are required, which keeps the models usable
with SQLite for a small smoke test.  Production and integration use PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Installation(Base):
    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    capacity_kw: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sensors: Mapped[list["Sensor"]] = relationship(
        back_populates="installation", cascade="all, delete-orphan"
    )
    raw_measurements: Mapped[list["MeasurementRaw"]] = relationship(
        back_populates="installation"
    )
    processed_measurements: Mapped[list["MeasurementProcessed"]] = relationship(
        back_populates="installation"
    )


class Sensor(Base):
    __tablename__ = "sensors"
    __table_args__ = (
        UniqueConstraint("installation_id", "code", name="uq_sensors_installation_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(150), nullable=False)
    sensor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    installation: Mapped[Installation] = relationship(back_populates="sensors")
    raw_measurements: Mapped[list["MeasurementRaw"]] = relationship(
        back_populates="sensor"
    )
    processed_measurements: Mapped[list["MeasurementProcessed"]] = relationship(
        back_populates="sensor"
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_file: Mapped[str] = mapped_column(String(500), nullable=False)
    file_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    measurements: Mapped[list["MeasurementRaw"]] = relationship(
        back_populates="ingestion_run", cascade="all, delete-orphan"
    )
    quality_report: Mapped["QualityReport | None"] = relationship(
        back_populates="ingestion_run", cascade="all, delete-orphan", uselist=False
    )
    rejected_records: Mapped[list["RejectedRecord"]] = relationship(
        back_populates="ingestion_run", cascade="all, delete-orphan"
    )


class MeasurementRaw(Base):
    __tablename__ = "measurements_raw"
    __table_args__ = (
        UniqueConstraint(
            "ingestion_run_id",
            "source_row",
            "metric",
            name="uq_raw_run_row_metric",
        ),
        Index(
            "ix_raw_installation_sensor_timestamp",
            "installation_id",
            "sensor_id",
            "timestamp",
        ),
        Index("ix_raw_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), nullable=False
    )
    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    measured_value: Mapped[float | None] = mapped_column(Float)
    raw_value_text: Mapped[str | None] = mapped_column(Text)
    source_file: Mapped[str] = mapped_column(String(500), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ingestion_run: Mapped[IngestionRun] = relationship(back_populates="measurements")
    installation: Mapped[Installation] = relationship(back_populates="raw_measurements")
    sensor: Mapped[Sensor] = relationship(back_populates="raw_measurements")


class MeasurementProcessed(Base):
    __tablename__ = "measurements_processed"
    __table_args__ = (
        UniqueConstraint(
            "installation_id", "sensor_id", "timestamp", name="uq_processed_key"
        ),
        Index(
            "ix_processed_installation_timestamp", "installation_id", "timestamp"
        ),
        Index("ix_processed_status", "quality_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), nullable=False
    )
    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    processed_value: Mapped[float | None] = mapped_column(Float)
    original_value: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_quality_status: Mapped[str | None] = mapped_column(String(20))
    is_imputed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processing_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    installation: Mapped[Installation] = relationship(
        back_populates="processed_measurements"
    )
    sensor: Mapped[Sensor] = relationship(back_populates="processed_measurements")


class QualityReport(Base):
    __tablename__ = "quality_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    source_file: Mapped[str] = mapped_column(String(500), nullable=False)
    total_records: Mapped[int] = mapped_column(Integer, nullable=False)
    total_measurements: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_measurements: Mapped[int] = mapped_column(Integer, nullable=False)
    invalid_measurements: Mapped[int] = mapped_column(Integer, nullable=False)
    suspicious_measurements: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_measurements: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_values: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_records: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_measurements: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_intervals: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    irregular_intervals: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_records: Mapped[int] = mapped_column(Integer, nullable=False)
    issues: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ingestion_run: Mapped[IngestionRun] = relationship(back_populates="quality_report")


class RejectedRecord(Base):
    __tablename__ = "rejected_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file: Mapped[str] = mapped_column(String(500), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_data: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ingestion_run: Mapped[IngestionRun] = relationship(
        back_populates="rejected_records"
    )
