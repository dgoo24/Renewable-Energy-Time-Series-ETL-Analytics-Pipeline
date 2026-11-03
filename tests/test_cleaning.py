"""Cleaning tests: deduplication, gridding and bounded imputation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import CleaningConfig, Settings
from src.cleaning import CleaningError, _limited_interpolate, clean_measurements
from src.ingestion import load_csv
from src.validation import validate_measurements
from tests.conftest import day_rows, measurement_row, run_file_pipeline, write_csv


def _clean(settings: Settings, path: Path, capacity: float = 10.0):
    ingestion = load_csv(path, settings)
    validation = validate_measurements(
        ingestion.long, settings, {"INST_A": capacity}
    )
    return clean_measurements(validation.long, settings, {})


def test_duplicate_measurements_are_removed_keeping_first(
    settings: Settings, tmp_path: Path
) -> None:
    first = measurement_row("2023-01-01T00:00:00+00:00", power=1.0)
    duplicate = measurement_row("2023-01-01T00:00:00+00:00", power=9.0)
    path = write_csv(tmp_path / "dupes.csv", [first, duplicate])

    cleaned = _clean(settings, path)
    power = cleaned.long[cleaned.long["metric"] == "active_power_kw"]

    assert len(power) == 1
    assert power.iloc[0]["processed_value"] == pytest.approx(1.0)
    assert any("duplicate" in warning for warning in cleaned.warnings)
    # One duplicated CSV record produces four duplicate measurements.
    assert cleaned.stats.iloc[0]["duplicates_removed"] == 4


def test_regular_grid_includes_missing_slots(
    settings: Settings, tmp_path: Path
) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00"),
        measurement_row("2023-01-01T00:15:00+00:00"),
        # 00:30 deliberately absent
        measurement_row("2023-01-01T00:45:00+00:00"),
        measurement_row("2023-01-01T01:00:00+00:00"),
    ]
    path = write_csv(tmp_path / "gap.csv", rows)
    cleaned = _clean(settings, path)

    wide = cleaned.wide["INST_A"]
    assert len(wide) == 5
    assert pd.Timestamp("2023-01-01T00:30:00Z") in wide.index


def test_single_missing_sample_is_interpolated(settings: Settings, tmp_path: Path) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00", power=1.0),
        measurement_row("2023-01-01T00:15:00+00:00", power=2.0),
        # 00:30 absent -> span between observed samples is 30 minutes
        measurement_row("2023-01-01T00:45:00+00:00", power=3.0),
        measurement_row("2023-01-01T01:00:00+00:00", power=4.0),
    ]
    path = write_csv(tmp_path / "impute.csv", rows)
    cleaned = _clean(settings, path)

    power = cleaned.long[cleaned.long["metric"] == "active_power_kw"].set_index(
        "timestamp"
    )
    gap = power.loc[pd.Timestamp("2023-01-01T00:30:00Z")]
    assert gap["is_imputed"] == True  # noqa: E712 - numpy bool
    assert gap["quality_status"] == "imputed"
    assert gap["processed_value"] == pytest.approx(2.5)
    assert gap["original_value"] is None or pd.isna(gap["original_value"])


def test_two_consecutive_missing_samples_are_not_interpolated(
    settings: Settings, tmp_path: Path
) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00", power=1.0),
        measurement_row("2023-01-01T00:15:00+00:00", power=2.0),
        # 00:30 and 00:45 absent -> span is 45 minutes > 30 minute limit
        measurement_row("2023-01-01T01:00:00+00:00", power=5.0),
    ]
    path = write_csv(tmp_path / "long_gap.csv", rows)
    cleaned = _clean(settings, path)

    power = cleaned.long[cleaned.long["metric"] == "active_power_kw"].set_index(
        "timestamp"
    )
    for timestamp in ("2023-01-01T00:30:00Z", "2023-01-01T00:45:00Z"):
        row = power.loc[pd.Timestamp(timestamp)]
        assert pd.isna(row["processed_value"])
        assert row["is_imputed"] == False  # noqa: E712 - numpy bool
        assert row["quality_status"] == "missing"


def test_no_extrapolation_at_series_edges(settings: Settings, tmp_path: Path) -> None:
    rows = [
        # Leading gap: blank source value with no earlier observation.
        measurement_row("2023-01-01T00:00:00+00:00", power=""),
        measurement_row("2023-01-01T00:15:00+00:00", power=2.0),
        measurement_row("2023-01-01T00:30:00+00:00", power=3.0),
        measurement_row("2023-01-01T00:45:00+00:00", power=4.0),
        # Trailing gap: blank source value after the last observation.
        measurement_row("2023-01-01T01:00:00+00:00", power=""),
    ]
    path = write_csv(tmp_path / "edges.csv", rows)
    cleaned = _clean(settings, path)

    power = cleaned.long[cleaned.long["metric"] == "active_power_kw"].set_index(
        "timestamp"
    )
    assert pd.isna(power.loc[pd.Timestamp("2023-01-01T00:00:00Z"), "processed_value"])
    assert pd.isna(power.loc[pd.Timestamp("2023-01-01T01:00:00Z"), "processed_value"])
    assert cleaned.long["is_imputed"].sum() == 0


def test_invalid_values_are_kept_as_nan_with_original(
    settings: Settings, tmp_path: Path
) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00", power=5.0),
        measurement_row("2023-01-01T00:15:00+00:00", power=60.0),
        measurement_row("2023-01-01T00:30:00+00:00", power=5.0),
    ]
    path = write_csv(tmp_path / "invalid.csv", rows)
    cleaned = _clean(settings, path)

    power = cleaned.long[cleaned.long["metric"] == "active_power_kw"].set_index(
        "timestamp"
    )
    invalid = power.loc[pd.Timestamp("2023-01-01T00:15:00Z")]
    assert invalid["quality_status"] == "invalid"
    assert invalid["source_quality_status"] == "invalid"
    assert pd.isna(invalid["processed_value"])
    assert invalid["original_value"] == pytest.approx(60.0)


def test_suspicious_values_are_retained_with_status(
    settings: Settings, tmp_path: Path
) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00", power=5.0),
        measurement_row("2023-01-01T00:15:00+00:00", power=13.0),
    ]
    path = write_csv(tmp_path / "suspicious.csv", rows)
    cleaned = _clean(settings, path)

    power = cleaned.long[cleaned.long["metric"] == "active_power_kw"].set_index(
        "timestamp"
    )
    row = power.loc[pd.Timestamp("2023-01-01T00:15:00Z")]
    assert row["quality_status"] == "suspicious"
    assert row["processed_value"] == pytest.approx(13.0)


def test_missing_intervals_table_tracks_fully_missing_runs(
    settings: Settings, tmp_path: Path
) -> None:
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00"),
        measurement_row("2023-01-01T00:15:00+00:00"),
        # 00:30 and 00:45 absent on every channel
        measurement_row("2023-01-01T01:00:00+00:00"),
    ]
    path = write_csv(tmp_path / "missing_run.csv", rows)
    cleaned = _clean(settings, path)

    assert len(cleaned.missing_intervals) == 1
    interval = cleaned.missing_intervals.iloc[0]
    assert interval["installation_id"] == "INST_A"
    assert interval["missing_slots"] == 2
    assert interval["duration_minutes"] == pytest.approx(30.0)


def test_limited_interpolate_handles_non_datetime_index() -> None:
    series = pd.Series([1.0, np.nan, 3.0])
    with pytest.raises(CleaningError):
        _limited_interpolate(series, pd.Timedelta(minutes=30))


@pytest.mark.parametrize("strategy", ["none", "drop"])
def test_missing_strategies_without_imputation(
    settings: Settings, tmp_path: Path, strategy: str
) -> None:
    adjusted = Settings(
        **{
            **settings.__dict__,
            "cleaning": CleaningConfig(
                expected_interval_minutes=15,
                resample_frequency="15min",
                missing_strategy=strategy,
                interpolation_max_gap_minutes=30,
                duplicate_keep="first",
            ),
        }
    )
    rows = [
        measurement_row("2023-01-01T00:00:00+00:00"),
        measurement_row("2023-01-01T00:15:00+00:00"),
        measurement_row("2023-01-01T00:45:00+00:00"),
    ]
    path = write_csv(tmp_path / f"{strategy}.csv", rows)
    cleaned = _clean(adjusted, path)

    assert "INST_A" in cleaned.wide
    if strategy == "none":
        assert len(cleaned.wide["INST_A"]) == 4
        assert cleaned.long["is_imputed"].sum() == 0
    else:
        # drop removes timestamps with any missing channel
        assert pd.Timestamp("2023-01-01T00:30:00Z") not in cleaned.wide["INST_A"].index


def test_full_day_coverage_and_duplicate_counts(
    settings: Settings, tmp_path: Path
) -> None:
    rows = day_rows()
    rows.append(dict(rows[0]))
    path = write_csv(tmp_path / "full_day.csv", rows)
    cleaned = _clean(settings, path)

    stats = cleaned.stats.iloc[0]
    assert stats["grid_slots"] == 96
    assert stats["duplicates_removed"] == 4
    assert stats["coverage_fraction"] == pytest.approx(1.0)
    assert stats["processed_values"] == 96 * 4
