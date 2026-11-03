"""Validation tests: status assignment, gap detection and quality reports."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import Settings
from src.ingestion import load_csv
from src.validation import (
    detect_missing_intervals,
    quality_report_frame,
    validate_measurements,
)
from tests.conftest import day_rows, measurement_row, write_csv


def _status_for(
    settings: Settings,
    tmp_path: Path,
    metric: str,
    value,
    *,
    capacity: float = 10.0,
) -> object:
    path = write_csv(
        tmp_path / "single.csv",
        [measurement_row("2023-01-01T00:00:00+00:00", **{_parameter(metric): value})],
    )
    ingestion = load_csv(path, settings)
    validation = validate_measurements(
        ingestion.long, settings, {"INST_A": capacity}
    )
    row = validation.long[validation.long["metric"] == metric].iloc[0]
    return row["quality_status"], row["status_reason"]


def _parameter(metric: str) -> str:
    return {
        "active_power_kw": "power",
        "irradiance_w_m2": "irradiance",
        "module_temperature_c": "module_temperature",
        "ambient_temperature_c": "ambient_temperature",
    }[metric]


@pytest.mark.parametrize(
    "value,expected_status,expected_reason",
    [
        (5.0, "valid", ""),
        (-0.05, "suspicious", "negative_power_small"),
        (-1.0, "invalid", "power_below_min"),
        (13.0, "suspicious", "power_above_capacity"),
        (40.0, "invalid", "power_far_above_capacity"),
        (999.0, "invalid", "power_above_max"),
    ],
)
def test_power_statuses(
    settings: Settings,
    tmp_path: Path,
    value: float,
    expected_status: str,
    expected_reason: str,
) -> None:
    assert _status_for(settings, tmp_path, "active_power_kw", value) == (
        expected_status,
        expected_reason,
    )


@pytest.mark.parametrize(
    "value,expected_status,expected_reason",
    [
        (500.0, "valid", ""),
        (-1.0, "invalid", "negative_irradiance"),
        (1400.0, "suspicious", "irradiance_above_suspicious_limit"),
        (1600.0, "invalid", "irradiance_above_max"),
    ],
)
def test_irradiance_statuses(
    settings: Settings,
    tmp_path: Path,
    value: float,
    expected_status: str,
    expected_reason: str,
) -> None:
    assert _status_for(settings, tmp_path, "irradiance_w_m2", value) == (
        expected_status,
        expected_reason,
    )


@pytest.mark.parametrize(
    "metric,value,expected_status,expected_reason",
    [
        ("module_temperature_c", 25.0, "valid", ""),
        ("module_temperature_c", 95.0, "suspicious", "module_temperature_suspicious"),
        ("module_temperature_c", 130.0, "invalid", "module_temperature_out_of_range"),
        ("ambient_temperature_c", 20.0, "valid", ""),
        ("ambient_temperature_c", 55.0, "suspicious", "ambient_temperature_suspicious"),
        ("ambient_temperature_c", 70.0, "invalid", "ambient_temperature_out_of_range"),
    ],
)
def test_temperature_statuses(
    settings: Settings,
    tmp_path: Path,
    metric: str,
    value: float,
    expected_status: str,
    expected_reason: str,
) -> None:
    assert _status_for(settings, tmp_path, metric, value) == (
        expected_status,
        expected_reason,
    )


def test_missing_and_unparseable_values(settings: Settings, tmp_path: Path) -> None:
    assert _status_for(settings, tmp_path, "active_power_kw", "") == (
        "missing",
        "missing_value",
    )
    assert _status_for(settings, tmp_path, "active_power_kw", "oops") == (
        "invalid",
        "unparseable_numeric",
    )


def test_missing_interval_detection() -> None:
    timestamps = pd.to_datetime(
        [
            "2023-01-01T00:00:00Z",
            "2023-01-01T00:15:00Z",
            "2023-01-01T01:00:00Z",
            "2023-01-01T01:15:00Z",
        ],
        utc=True,
    )
    long = pd.DataFrame(
        {
            "installation_id": ["INST_A"] * 4,
            "timestamp": timestamps,
        }
    )
    gaps, irregular, missing_slots = detect_missing_intervals(long, 15)
    assert len(gaps) == 1
    assert gaps.iloc[0]["gap_minutes"] == 45.0
    assert gaps.iloc[0]["missing_slots"] == 2
    assert missing_slots == 2
    assert irregular == 0


def test_irregular_sampling_is_counted() -> None:
    timestamps = pd.to_datetime(
        ["2023-01-01T00:00:00Z", "2023-01-01T00:10:00Z", "2023-01-01T00:20:00Z"],
        utc=True,
    )
    long = pd.DataFrame({"installation_id": ["INST_A"] * 3, "timestamp": timestamps})
    gaps, irregular, missing_slots = detect_missing_intervals(long, 15)
    assert gaps.empty
    assert irregular == 2
    assert missing_slots == 0


def test_quality_report_counts_and_issues(
    settings: Settings, tmp_path: Path
) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00", power=5.0),
        measurement_row("2023-01-01T00:15:00+00:00", power=13.0),
        measurement_row("2023-01-01T00:30:00+00:00", power=-2.0),
        measurement_row("2023-01-01T00:45:00+00:00", power="oops"),
        measurement_row("2023-01-01T02:00:00+00:00", power=""),
    ]
    path = write_csv(tmp_path / "quality.csv", rows)
    ingestion = load_csv(path, settings)
    validation = validate_measurements(
        ingestion.long,
        settings,
        {"INST_A": 10.0},
        source_file=path.name,
        total_records=ingestion.stats["total_rows"],
        rejected_records=ingestion.stats["rejected_rows"],
    )
    report = validation.report

    assert report.total_records == 5
    assert report.total_measurements == 20
    assert report.valid_measurements == 16
    assert report.suspicious_measurements == 1
    assert report.invalid_measurements == 2
    assert report.missing_measurements == 1
    assert report.missing_values == 1
    assert report.missing_intervals == 1
    assert report.missing_slots == 4

    codes = {issue.code for issue in report.issues}
    assert {
        "power_above_capacity",
        "power_below_min",
        "unparseable_numeric",
        "missing_value",
        "missing_intervals",
    }.issubset(codes)

    frame = quality_report_frame(report)
    assert len(frame) == 1
    issues = json.loads(frame.iloc[0]["issues"])
    assert any(issue["code"] == "missing_value" for issue in issues)
    assert frame.iloc[0]["issue_summary"]


def test_empty_measurements_are_validated_without_error(settings: Settings) -> None:
    empty = pd.DataFrame(columns=["installation_id", "timestamp", "value"])
    result = validate_measurements(empty, settings, {}, source_file="empty.csv")
    assert result.long.empty
    assert result.report.total_records == 0
    assert result.report.source_file == "empty.csv"


def test_capacity_without_metadata_does_not_invent_invalid_values(
    settings: Settings, tmp_path: Path
) -> None:
    path = write_csv(
        tmp_path / "no_capacity.csv",
        [measurement_row("2023-01-01T00:00:00+00:00", power=50.0)],
    )
    ingestion = load_csv(path, settings)
    validation = validate_measurements(ingestion.long, settings, {})
    row = validation.long[validation.long["metric"] == "active_power_kw"].iloc[0]
    # Without a known capacity only the absolute physical maximum applies.
    assert row["quality_status"] == "valid"
