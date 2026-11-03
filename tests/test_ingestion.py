"""Ingestion tests: parsing, rejection and traceability."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import METRIC_COLUMNS, Settings
from src.ingestion import (
    IngestionError,
    load_csv,
    normalize_column_name,
    parse_numeric_value,
    parse_timestamp,
)
from tests.conftest import BASE_COLUMNS, day_rows, measurement_row, sha256, write_csv


def test_normalize_column_name() -> None:
    assert normalize_column_name("\ufeff Timestamp ") == "timestamp"
    assert normalize_column_name("Active Power (kW)") == "active_power_kw"


def test_parse_timestamp_formats() -> None:
    assert parse_timestamp("2023-01-01T00:00:00Z", "UTC") == pd.Timestamp(
        "2023-01-01T00:00:00Z"
    )
    assert parse_timestamp("2023-01-01 00:00:00", "Europe/Berlin") == pd.Timestamp(
        "2022-12-31T23:00:00Z"
    )
    assert parse_timestamp("01.02.2023 12:30", "UTC") == pd.Timestamp(
        "2023-02-01T12:30:00Z"
    )
    assert parse_timestamp("1672531200", "UTC") == pd.Timestamp("2023-01-01T00:00:00Z")
    assert parse_timestamp("1672531200000", "UTC") == pd.Timestamp(
        "2023-01-01T00:00:00Z"
    )
    assert pd.isna(parse_timestamp("not a date", "UTC"))
    assert pd.isna(parse_timestamp("", "UTC"))
    assert pd.isna(parse_timestamp(None, "UTC"))


def test_parse_numeric_value() -> None:
    assert parse_numeric_value("1.5") == (1.5, False)
    assert parse_numeric_value("1,5") == (1.5, False)
    assert parse_numeric_value("1,234.5") == (1234.5, False)
    assert parse_numeric_value("  ") == (np.nan, False)
    number, error = parse_numeric_value("abc")
    assert np.isnan(number) and error is True
    number, error = parse_numeric_value("nan")
    assert np.isnan(number) and error is True


def test_load_csv_produces_long_format(settings: Settings, tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "data" / "raw" / "one.csv",
        [
            measurement_row("2024-01-01T00:00:00+00:00", power=2.0),
            measurement_row("2024-01-01T00:15:00+00:00", power=3.0),
        ],
    )
    result = load_csv(path, settings)

    assert result.stats["total_rows"] == 2
    assert result.stats["rejected_rows"] == 0
    assert len(result.long) == 2 * len(METRIC_COLUMNS)
    assert set(result.long["metric"]) == set(METRIC_COLUMNS)
    assert set(result.long["unit"]) == {"kW", "W/m2", "degC"}
    assert result.long["timestamp"].dt.tz is not None
    assert result.stats["total_measurements"] == 8
    assert result.file_sha256 == sha256(path)
    assert result.source_file == "one.csv"


def test_naive_timestamps_use_configured_timezone(
    settings: Settings, tmp_path: Path
) -> None:
    path = write_csv(
        tmp_path / "naive.csv",
        [measurement_row("2023-01-01 00:00:00")],
    )
    result = load_csv(path, settings)
    timestamp = result.long["timestamp"].iloc[0]
    assert timestamp == pd.Timestamp("2022-12-31T23:00:00Z")


def test_mixed_timestamp_formats(settings: Settings, tmp_path: Path) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00"),
        measurement_row("01.01.2023 00:15"),
        measurement_row("1672532100"),
        measurement_row("1672533000000"),
    ]
    path = write_csv(tmp_path / "mixed.csv", rows)
    result = load_csv(path, settings)
    timestamps = sorted(result.long["timestamp"].unique())
    assert len(timestamps) == 4
    assert result.stats["rejected_rows"] == 0


def test_invalid_rows_are_rejected_and_preserved(
    settings: Settings, tmp_path: Path
) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00"),
        measurement_row("this-is-not-a-time"),
        measurement_row("2023-01-01T00:30:00+00:00", installation=""),
    ]
    path = write_csv(tmp_path / "rejected.csv", rows)
    before = sha256(path)

    result = load_csv(path, settings)

    assert result.stats["total_rows"] == 3
    assert result.stats["valid_rows"] == 1
    assert len(result.rejected) == 2
    assert set(result.rejected["source_row"]) == {2, 3}
    assert "invalid or missing timestamp" in result.rejected["reason"].iloc[0]
    assert "missing installation_id" in result.rejected["reason"].iloc[1]
    # The raw file must never be modified.
    assert sha256(path) == before


def test_duplicate_rows_within_file_are_reported(
    settings: Settings, tmp_path: Path
) -> None:
    row = measurement_row("2023-01-01T00:00:00+00:00")
    path = write_csv(tmp_path / "dupes.csv", [row, dict(row), row])
    result = load_csv(path, settings)

    assert result.stats["duplicate_rows"] == 2
    assert len(result.long) == 3 * len(METRIC_COLUMNS)


def test_blank_and_unparseable_values_are_preserved_long(
    settings: Settings, tmp_path: Path
) -> None:
    path = write_csv(
        tmp_path / "bad_values.csv",
        [measurement_row("2023-01-01T00:00:00+00:00", power="", irradiance="oops")],
    )
    result = load_csv(path, settings)
    power = result.long[result.long["metric"] == "active_power_kw"].iloc[0]
    irradiance = result.long[result.long["metric"] == "irradiance_w_m2"].iloc[0]

    assert np.isnan(power["value"])
    assert power["raw_value_text"] == ""
    assert power["value_parse_error"] == False  # noqa: E712 - numpy bool
    assert np.isnan(irradiance["value"])
    assert irradiance["raw_value_text"] == "oops"
    assert irradiance["value_parse_error"] == True  # noqa: E712 - numpy bool
    assert result.stats["numeric_parse_errors"] == 1


def test_missing_required_column_raises(settings: Settings, tmp_path: Path) -> None:
    columns = [column for column in BASE_COLUMNS if column != "irradiance_w_m2"]
    path = tmp_path / "missing_column.csv"
    pd.DataFrame(
        [["2023-01-01T00:00:00Z", "INST_A", "INV1", 1.0, 20.0, 15.0]],
        columns=columns,
    ).to_csv(path, index=False)

    with pytest.raises(IngestionError, match="missing required columns"):
        load_csv(path, settings)


def test_unknown_columns_are_ignored_but_reported(
    settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "extra.csv"
    frame = pd.DataFrame([measurement_row("2023-01-01T00:00:00+00:00")])
    frame["operator_note"] = "synthetic"
    frame.to_csv(path, index=False)

    result = load_csv(path, settings)
    assert result.stats["unknown_columns"] == ["operator_note"]
    assert any("operator_note" in issue for issue in result.issues)


def test_empty_and_missing_files_raise(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="does not exist"):
        load_csv(tmp_path / "nope.csv", settings)

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(IngestionError, match="empty"):
        load_csv(empty, settings)


def test_header_only_file_raises(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "header_only.csv"
    path.write_text(",".join(BASE_COLUMNS) + "\n", encoding="utf-8")
    with pytest.raises(IngestionError, match="no data rows"):
        load_csv(path, settings)


def test_complete_day_long_count(settings: Settings, tmp_path: Path) -> None:
    path = write_csv(tmp_path / "day.csv", day_rows())
    result = load_csv(path, settings)
    assert result.stats["total_rows"] == 96
    assert len(result.long) == 96 * 4
