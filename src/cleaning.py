"""Cleaning and time-series preparation.

The cleaner never modifies the incoming raw frame.  It produces:

* a long processed frame with one row per installation/sensor/timestamp,
  including explicit ``missing`` rows, original values and imputation flags;
* wide per-installation frames used by the analytics module;
* a per-installation summary and a table of fully-missing intervals.

Duplicate rule
--------------
Measurements with the same installation, source sensor, metric and timestamp are
duplicates.  They are sorted deterministically by timestamp, source file name and
source row, then the first (default) or last row is kept.  The number of removed
rows is reported.  Raw duplicates remain untouched in the raw layer.

Imputation rule
---------------
Only *internal* gaps are filled, never leading/trailing gaps.  A gap is filled
only when the time between the last observed sample before it and the first
observed sample after it is at most ``INTERPOLATION_MAX_GAP_MINUTES``.  With
15-minute data and a 30-minute limit this fills at most one missing sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from src.config import METRIC_COLUMNS, METRIC_UNITS, Settings


class CleaningError(ValueError):
    """Raised when a time series cannot be cleaned with the given settings."""


@dataclass
class CleanResult:
    long: pd.DataFrame
    wide: dict[str, pd.DataFrame]
    missing_intervals: pd.DataFrame
    stats: pd.DataFrame
    metric_stats: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


def _limited_interpolate(series: pd.Series, max_gap: pd.Timedelta) -> pd.Series:
    """Time-interpolate only short internal gaps."""

    if not series.isna().any() or len(series) < 3:
        return series.copy()
    if not isinstance(series.index, pd.DatetimeIndex):
        raise CleaningError("Time interpolation requires a DatetimeIndex")

    interpolated = series.interpolate(method="time", limit_area="inside")
    result = series.copy()
    missing = series.isna()
    run_ids = (missing != missing.shift(fill_value=False)).cumsum()
    for run_id in run_ids[missing].unique():
        positions = series.index[run_ids == run_id]
        location = series.index.get_indexer(positions)
        if len(location) == 0:
            continue
        first, last = int(location[0]), int(location[-1])
        if first == 0 or last == len(series) - 1:
            continue
        previous_time = series.index[first - 1]
        next_time = series.index[last + 1]
        if next_time - previous_time <= max_gap:
            result.iloc[location] = interpolated.iloc[location]
    return result


def _limited_ffill(series: pd.Series, max_gap: pd.Timedelta) -> pd.Series:
    """Forward-fill only short internal gaps."""

    if not series.isna().any() or len(series) < 2:
        return series.copy()

    result = series.copy()
    missing = series.isna()
    run_ids = (missing != missing.shift(fill_value=False)).cumsum()
    for run_id in run_ids[missing].unique():
        positions = series.index[run_ids == run_id]
        location = series.index.get_indexer(positions)
        if len(location) == 0:
            continue
        first, last = int(location[0]), int(location[-1])
        if first == 0 or last == len(series) - 1:
            continue
        previous_time = series.index[first - 1]
        next_time = series.index[last + 1]
        if next_time - previous_time <= max_gap:
            result.iloc[location] = series.iloc[first - 1]
    return result


def _aggregate(
    frame: pd.DataFrame,
    column: str,
    how: str,
    settings: Settings,
) -> pd.Series:
    """Aggregate one column by resampling or by exact timestamp.

    With resampling enabled, raw samples are assigned to fixed-frequency bins.  Two
    samples in the same bin are averaged; the status code uses the more severe
    value.  This is documented as a limitation.
    """

    frequency = settings.cleaning.resample_frequency
    try:
        if frequency:
            grouped = frame.set_index("timestamp")[column].resample(
                frequency, label="left", closed="left"
            )
        else:
            grouped = frame.groupby("timestamp")[column]
        result = grouped.mean() if how == "mean" else grouped.max()
    except ValueError as exc:
        raise CleaningError(
            f"Invalid RESAMPLE_FREQUENCY {frequency!r}: {exc}"
        ) from exc
    return result.rename(column)


def _status_code(values: pd.Series) -> pd.Series:
    return values.map(
        {"valid": 1.0, "suspicious": 2.0, "invalid": 0.0, "missing": 0.0}
    ).astype(float)


def _dataframe_status(code: pd.Series, has_raw: pd.Series) -> pd.Series:
    status = pd.Series("missing", index=code.index, dtype=object)
    status.loc[has_raw] = "invalid"
    status.loc[code == 1.0] = "valid"
    status.loc[code == 2.0] = "suspicious"
    return status


def _missing_runs(
    frame: pd.DataFrame,
    metrics: list[str],
    expected_interval_minutes: int,
) -> pd.DataFrame:
    """Find contiguous intervals where every measurement channel is missing."""

    columns = [
        "installation_id",
        "start_timestamp",
        "end_timestamp",
        "duration_minutes",
        "missing_slots",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    all_missing = frame[metrics].isna().all(axis=1)
    if not all_missing.any():
        return pd.DataFrame(columns=columns)

    installation_id = str(frame.attrs.get("installation_id", "unknown"))
    run_ids = (all_missing != all_missing.shift(fill_value=False)).cumsum()
    rows: list[dict[str, Any]] = []
    for run_id in run_ids[all_missing].unique():
        positions = frame.index[run_ids == run_id]
        start = positions[0]
        end = positions[-1]
        slots = len(positions)
        duration = (end - start).total_seconds() / 60.0 + expected_interval_minutes
        rows.append(
            {
                "installation_id": installation_id,
                "start_timestamp": start,
                "end_timestamp": end,
                "duration_minutes": round(duration, 2),
                "missing_slots": slots,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def clean_measurements(
    long: pd.DataFrame,
    settings: Settings,
    installations: Mapping[str, Any] | None = None,
) -> CleanResult:
    """Clean validated long-format measurements.

    ``long`` must contain ``quality_status`` (from validation) and the ingestion
    columns produced by :func:`src.ingestion.load_csv`.
    """

    if long.empty:
        return CleanResult(
            long=_empty_processed_frame(),
            wide={},
            missing_intervals=pd.DataFrame(
                columns=[
                    "installation_id",
                    "start_timestamp",
                    "end_timestamp",
                    "duration_minutes",
                    "missing_slots",
                ]
            ),
            stats=pd.DataFrame(),
            metric_stats=pd.DataFrame(),
            warnings=["No measurements to clean."],
        )

    data = long.copy()
    required = {
        "installation_id",
        "sensor_id",
        "metric",
        "timestamp",
        "value",
        "quality_status",
        "source_file",
        "source_row",
    }
    missing_columns = required.difference(data.columns)
    if missing_columns:
        raise CleaningError(
            f"Long measurement frame lacks columns: {sorted(missing_columns)}"
        )

    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
    data["quality_status"] = data["quality_status"].fillna("missing")

    key = ["installation_id", "sensor_id", "metric", "timestamp"]
    data = data.sort_values(
        ["timestamp", "source_file", "source_row"], kind="stable"
    )
    duplicate_mask = data.duplicated(subset=key, keep=settings.cleaning.duplicate_keep)
    duplicates_removed = int(duplicate_mask.sum())
    duplicates_by_installation = (
        data.loc[duplicate_mask].groupby("installation_id").size().to_dict()
    )
    data = data.loc[~duplicate_mask].copy()

    warnings: list[str] = []
    if duplicates_removed:
        warnings.append(
            f"{duplicates_removed} duplicate measurement(s) removed before cleaning "
            f"(rule: keep {settings.cleaning.duplicate_keep})."
        )
    long_parts: list[pd.DataFrame] = []
    wide_frames: dict[str, pd.DataFrame] = {}
    missing_tables: list[pd.DataFrame] = []
    stats_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    max_gap = pd.Timedelta(minutes=settings.cleaning.interpolation_max_gap_minutes)

    for installation_id, group in data.groupby("installation_id", sort=True):
        installation_id = str(installation_id)
        group = group.copy()

        sensor_counts = (
            group.groupby(["metric", "sensor_id"]).size().reset_index(name="count")
        )
        sensor_map: dict[str, str] = {}
        multi_sensor_metrics: list[str] = []
        for metric in METRIC_COLUMNS:
            options = sensor_counts[sensor_counts["metric"] == metric].sort_values(
                "sensor_id"
            )
            if options.empty:
                continue
            sensor_map[metric] = str(options.iloc[0]["sensor_id"])
            if len(options) > 1:
                multi_sensor_metrics.append(metric)
        if multi_sensor_metrics:
            warnings.append(
                f"{installation_id}: multiple sensors found for "
                f"{', '.join(multi_sensor_metrics)}; values per timestamp are averaged."
            )

        # Build per-metric value, original-value and status series.
        value_series: dict[str, pd.Series] = {}
        original_series: dict[str, pd.Series] = {}
        status_series: dict[str, pd.Series] = {}
        source_status_series: dict[str, pd.Series] = {}
        multiple_samples = 0

        for metric in METRIC_COLUMNS:
            subset = group[group["metric"] == metric].copy()
            if subset.empty:
                continue
            subset["_status_code"] = _status_code(subset["quality_status"])
            subset["_valid_value"] = np.where(
                subset["quality_status"].isin(["valid", "suspicious"]),
                subset["value"],
                np.nan,
            )
            joined = pd.concat(
                [
                    _aggregate(subset, "_valid_value", "mean", settings).rename(
                        "_value"
                    ),
                    _aggregate(subset, "value", "mean", settings).rename(
                        "_raw_value"
                    ),
                    _aggregate(subset, "_status_code", "max", settings).rename(
                        "_status"
                    ),
                ],
                axis=1,
            )
            joined["_status"] = joined["_status"].fillna(0.0)
            if settings.cleaning.resample_frequency:
                # Count raw samples per resampled bin to warn about aggregation.
                counts = (
                    subset.set_index("timestamp")
                    .resample(
                        settings.cleaning.resample_frequency,
                        label="left",
                        closed="left",
                    )
                    .size()
                )
                multiple_samples += int((counts > 1).sum())

            value_series[metric] = joined["_value"]
            original_series[metric] = joined["_raw_value"]
            status_series[metric] = joined["_status"].fillna(0.0)
            source_status_series[metric] = _dataframe_status(
                joined["_status"].fillna(0.0), joined["_raw_value"].notna()
            )

        if not value_series:
            warnings.append(f"{installation_id}: no recognised measurement channels.")
            continue

        wide = pd.concat(
            {
                metric: value_series[metric].rename(metric)
                for metric in value_series
            },
            axis=1,
        ).sort_index()
        wide = wide[~wide.index.duplicated(keep="first")]

        # Add original/status columns on the same index.
        for metric in value_series:
            wide[f"{metric}__original"] = original_series[metric].reindex(wide.index)
            wide[f"{metric}__source_status"] = source_status_series[
                metric
            ].reindex(wide.index).fillna("missing")
            wide[f"{metric}__status"] = "missing"
            wide[f"{metric}__imputed"] = False

        if settings.cleaning.resample_frequency and multiple_samples:
            warnings.append(
                f"{installation_id}: {multiple_samples} resampling bin(s) contained "
                "more than one raw sample; values were averaged."
            )

        for metric in value_series:
            series = wide[metric]
            strategy = settings.cleaning.missing_strategy
            if strategy == "interpolate":
                filled = _limited_interpolate(series, max_gap)
            elif strategy == "ffill":
                filled = _limited_ffill(series, max_gap)
            elif strategy in ("none", "drop"):
                filled = series
            else:  # pragma: no cover - validated in config
                raise CleaningError(f"Unknown missing strategy {strategy!r}")

            source_status = wide[f"{metric}__source_status"]
            # Only blank/missing source values may be imputed.  Invalid source
            # values stay NaN so that their original value remains distinguishable
            # and they are never presented as if they had been measured.
            imputable = series.isna() & source_status.eq("missing")
            imputed = imputable & filled.notna()
            filled = filled.copy()
            filled.loc[source_status.eq("invalid")] = np.nan
            imputed = imputed & filled.notna()

            status = pd.Series("missing", index=wide.index, dtype=object)
            status.loc[source_status == "valid"] = "valid"
            status.loc[source_status == "suspicious"] = "suspicious"
            status.loc[imputed] = "imputed"
            status.loc[source_status == "invalid"] = "invalid"
            wide[metric] = filled
            wide[f"{metric}__imputed"] = imputed
            wide[f"{metric}__status"] = status

        if settings.cleaning.missing_strategy == "drop":
            before = len(wide)
            wide = wide.dropna(subset=list(value_series.keys())).copy()
            removed = before - len(wide)
            if removed:
                warnings.append(
                    f"{installation_id}: missing_strategy=drop removed {removed} "
                    "timestamp(s) with at least one missing channel."
                )

        wide.attrs["installation_id"] = installation_id
        wide_frames[installation_id] = wide

        # Long processed frame, including explicit missing rows.
        for metric in value_series:
            if metric not in sensor_map:
                continue
            sensor_node = sensor_map[metric]
            sensor_code = f"{sensor_node}::{metric}"
            part = pd.DataFrame(
                {
                    "installation_id": installation_id,
                    "sensor_id": sensor_node,
                    "sensor_code": sensor_code,
                    "metric": metric,
                    "unit": METRIC_UNITS[metric],
                    "timestamp": wide.index,
                    "processed_value": wide[metric].to_numpy(),
                    "original_value": wide[f"{metric}__original"].to_numpy(),
                    "quality_status": wide[f"{metric}__status"].to_numpy(),
                    "is_imputed": wide[f"{metric}__imputed"].to_numpy(dtype=bool),
                    "source_quality_status": wide[
                        f"{metric}__source_status"
                    ].to_numpy(),
                }
            )
            long_parts.append(part)

        missing_tables.append(
            _missing_runs(
                wide,
                list(value_series.keys()),
                settings.cleaning.expected_interval_minutes,
            )
        )

        status_counts = pd.concat(
            [wide[f"{metric}__status"] for metric in value_series]
        ).value_counts()
        source_counts = pd.concat(
            [wide[f"{metric}__source_status"] for metric in value_series]
        ).value_counts()
        grid_slots = len(wide)
        total_cells = grid_slots * len(value_series)
        processed_values = int(
            sum(wide[metric].notna().sum() for metric in value_series)
        )
        imputed_values = int(
            sum(wide[f"{metric}__imputed"].sum() for metric in value_series)
        )
        stats_rows.append(
            {
                "installation_id": installation_id,
                "first_timestamp": wide.index.min(),
                "last_timestamp": wide.index.max(),
                "grid_slots": grid_slots,
                "measurements_in": int(len(group)),
                "duplicates_removed": int(
                    duplicates_by_installation.get(installation_id, 0)
                ),
                "valid_values": int(status_counts.get("valid", 0)),
                "suspicious_values": int(status_counts.get("suspicious", 0)),
                "missing_values": int(status_counts.get("missing", 0)),
                "invalid_values": int(status_counts.get("invalid", 0)),
                "imputed_values": imputed_values,
                "processed_values": processed_values,
                "coverage_fraction": round(processed_values / total_cells, 4)
                if total_cells
                else 0.0,
                "imputed_fraction": round(imputed_values / total_cells, 4)
                if total_cells
                else 0.0,
                "source_invalid_values": int(
                    source_counts.get("invalid", 0)
                ),
                "resampled": bool(settings.cleaning.resample_frequency),
            }
        )

        for metric in value_series:
            counts = wide[f"{metric}__status"].value_counts()
            metric_rows.append(
                {
                    "installation_id": installation_id,
                    "metric": metric,
                    "unit": METRIC_UNITS[metric],
                    "slots": grid_slots,
                    "valid": int(counts.get("valid", 0)),
                    "suspicious": int(counts.get("suspicious", 0)),
                    "invalid": int(counts.get("invalid", 0)),
                    "missing": int(counts.get("missing", 0)),
                    "imputed": int(wide[f"{metric}__imputed"].sum()),
                    "coverage_fraction": round(
                        int(wide[metric].notna().sum()) / grid_slots, 4
                    )
                    if grid_slots
                    else 0.0,
                }
            )

    processed = (
        pd.concat(long_parts, ignore_index=True)
        if long_parts
        else _empty_processed_frame()
    )
    if not processed.empty:
        processed = processed.sort_values(
            ["installation_id", "timestamp", "metric"], kind="stable"
        ).reset_index(drop=True)

    non_empty_missing = [table for table in missing_tables if not table.empty]
    missing_intervals = (
        pd.concat(non_empty_missing, ignore_index=True)
        if non_empty_missing
        else pd.DataFrame(
            columns=[
                "installation_id",
                "start_timestamp",
                "end_timestamp",
                "duration_minutes",
                "missing_slots",
            ]
        )
    )

    stats = pd.DataFrame(stats_rows)

    return CleanResult(
        long=processed,
        wide=wide_frames,
        missing_intervals=missing_intervals,
        stats=stats,
        metric_stats=pd.DataFrame(metric_rows),
        warnings=warnings,
    )


def _empty_processed_frame() -> pd.DataFrame:
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
