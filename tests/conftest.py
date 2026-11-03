"""Shared pytest fixtures and small helpers.

Tests are deliberately built on tiny, hand-written CSV files so that every
assertion is explainable.  The provenance of all test data is synthetic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
import pytest

from src.config import (
    METRIC_COLUMNS,
    AnalyticsConfig,
    CleaningConfig,
    InstallationMeta,
    Settings,
    ValidationConfig,
)
from src.cleaning import CleanResult, clean_measurements
from src.ingestion import IngestionResult, load_csv
from src.validation import QualityReport, ValidationResult, validate_measurements

BASE_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "installation_id",
    "sensor_id",
    *METRIC_COLUMNS,
)

TZ = "UTC"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Settings whose input/output paths live inside the test's tmp_path."""

    result = Settings(
        project_root=tmp_path,
        input_dir=tmp_path / "data" / "raw",
        output_dir=tmp_path / "output",
        synthetic_dir=tmp_path / "data" / "synthetic",
        installations_file=tmp_path / "config" / "installations.csv",
        database_url="sqlite+pysqlite:///:memory:",
        input_timezone="Europe/Berlin",
        data_provenance=(
            "SYNTHETIC EXAMPLE DATA - generated for demonstration, not real measurements"
        ),
        plot_dpi=60,
        validation=ValidationConfig(),
        cleaning=CleaningConfig(),
        analytics=AnalyticsConfig(),
    )
    result.ensure_directories()
    return result


@pytest.fixture()
def metadata() -> dict[str, InstallationMeta]:
    return {
        "INST_A": InstallationMeta(
            installation_id="INST_A",
            name="North Roof Array",
            location="Research Campus North",
            capacity_kw=10.0,
        )
    }


def measurement_row(
    timestamp: str,
    *,
    installation: str = "INST_A",
    sensor: str = "INV1",
    power: Any = 1.0,
    irradiance: Any = 500.0,
    module_temperature: Any = 20.0,
    ambient_temperature: Any = 15.0,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "installation_id": installation,
        "sensor_id": sensor,
        "active_power_kw": power,
        "irradiance_w_m2": irradiance,
        "module_temperature_c": module_temperature,
        "ambient_temperature_c": ambient_temperature,
    }


def write_csv(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    columns: Sequence[str] | None = None,
) -> Path:
    frame = pd.DataFrame(list(rows), columns=list(columns or BASE_COLUMNS))
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


def day_rows(
    *,
    date: str = "2024-01-01",
    periods: int = 96,
    power: float = 5.0,
    irradiance: float = 1000.0,
    installation: str = "INST_A",
    sensor: str = "INV1",
) -> list[dict[str, Any]]:
    """A complete day of constant 15-minute PV measurements (synthetic)."""

    timestamps = pd.date_range(
        pd.Timestamp(date, tz=TZ), periods=periods, freq="15min"
    )
    return [
        measurement_row(
            timestamp.isoformat(),
            installation=installation,
            sensor=sensor,
            power=power,
            irradiance=irradiance,
            module_temperature=25.0,
            ambient_temperature=20.0,
        )
        for timestamp in timestamps
    ]


def capacities(metadata: Mapping[str, InstallationMeta]) -> dict[str, float]:
    return {
        code: meta.capacity_kw
        for code, meta in metadata.items()
        if meta.capacity_kw is not None
    }


def run_file_pipeline(
    path: Path,
    settings: Settings,
    metadata: Mapping[str, InstallationMeta] | None = None,
) -> tuple[IngestionResult, ValidationResult, CleanResult]:
    ingestion = load_csv(path, settings)
    validation = validate_measurements(
        ingestion.long,
        settings,
        capacities(metadata or {}),
        source_file=ingestion.source_file,
        total_records=ingestion.stats["total_rows"],
        rejected_records=ingestion.stats["rejected_rows"],
    )
    cleaned = clean_measurements(validation.long, settings, metadata or {})
    return ingestion, validation, cleaned


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def single_quality_frame(report: QualityReport) -> pd.DataFrame:
    from src.validation import quality_report_frame

    return quality_report_frame(report)
