"""CSV ingestion.

This module is intentionally free of database code.  It reads one CSV file,
validates the schema, parses timestamps and numbers, records malformed rows in a
separate ``rejected`` frame, and returns measurements in long format (one row per
sensor channel and timestamp).

The original file is never modified.  Text representations of all values are kept
so that malformed or invalid source values remain traceable.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import (
    METRIC_COLUMNS,
    METRIC_UNITS,
    REQUIRED_COLUMNS,
    Settings,
)


class IngestionError(ValueError):
    """Raised when a file cannot be read as a valid measurement CSV."""


@dataclass
class IngestionResult:
    """Result of reading and parsing one CSV file."""

    path: Path
    file_sha256: str
    wide: pd.DataFrame
    long: pd.DataFrame
    rejected: pd.DataFrame
    stats: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def source_file(self) -> str:
        return self.path.name


def normalize_column_name(name: str) -> str:
    """Normalize a CSV header to ``lower_snake_case``."""

    text = str(name).replace("\ufeff", "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_timestamp(value: Any, timezone: str) -> pd.Timestamp:
    """Parse one timestamp value and return a UTC ``Timestamp`` or ``NaT``.

    Supported formats:

    * ISO 8601, with or without offset (``2023-01-01T00:00:00Z``,
      ``2023-04-01 12:00:00+02:00``, ``2023-01-01``)
    * German day-first format ``DD.MM.YYYY HH:MM[:SS]``
    * Unix epoch seconds (10 digits) and milliseconds (13 digits)

    Naive timestamps are interpreted in the configured input timezone.  Ambiguous
    local times during the autumn DST transition are interpreted as the first
    (DST) occurrence; non-existent spring times are shifted forward.
    """

    if value is None or (isinstance(value, float) and np.isnan(value)):
        return pd.NaT
    text = str(value).strip()
    if not text:
        return pd.NaT

    if re.fullmatch(r"\d{10}", text):
        return pd.Timestamp(int(text), unit="s", tz="UTC")
    if re.fullmatch(r"\d{13}", text):
        return pd.Timestamp(int(text), unit="ms", tz="UTC")

    parsed: datetime | pd.Timestamp | None = None
    if re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}", text):
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return pd.NaT
    else:
        try:
            parsed = pd.Timestamp(text)
        except (ValueError, TypeError):
            return pd.NaT
        if parsed is pd.NaT or pd.isna(parsed):
            return pd.NaT

    timestamp = pd.Timestamp(parsed)
    if timestamp.tzinfo is None:
        try:
            timestamp = timestamp.tz_localize(
                timezone, ambiguous=True, nonexistent="shift_forward"
            )
        except (TypeError, ValueError):
            return pd.NaT
    return timestamp.tz_convert("UTC")


def parse_timestamp_series(values: pd.Series, timezone: str) -> pd.Series:
    parsed = [parse_timestamp(value, timezone) for value in values]
    return pd.to_datetime(pd.Series(parsed, index=values.index), utc=True)


def parse_numeric_value(value: Any) -> tuple[float, bool]:
    """Parse a numeric value.

    Returns ``(number, parse_error)``.  An empty string is a missing value, not a
    parse error.  A decimal comma is accepted when no decimal point is present;
    otherwise commas are treated as thousands separators.
    """

    if value is None:
        return np.nan, False
    text = str(value).strip()
    if not text:
        return np.nan, False

    cleaned = text.replace(" ", "")
    if "," in cleaned:
        if "." in cleaned:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    try:
        number = float(cleaned)
    except (TypeError, ValueError):
        return np.nan, True
    if not np.isfinite(number):
        return np.nan, True
    return number, False


def _read_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
    except StopIteration as exc:
        raise IngestionError(f"{path.name}: file is empty") from exc
    except UnicodeDecodeError as exc:
        raise IngestionError(
            f"{path.name}: file is not valid UTF-8; convert it to UTF-8 first"
        ) from exc
    except OSError as exc:
        raise IngestionError(f"{path.name}: cannot read file: {exc}") from exc
    return header


def _validate_header(path: Path) -> tuple[list[str], list[str]]:
    header = _read_header(path)
    normalized = [normalize_column_name(name) for name in header]
    if any(not name for name in normalized):
        raise IngestionError(f"{path.name}: empty CSV column name")
    if len(set(normalized)) != len(normalized):
        duplicates = sorted(
            {name for name in normalized if normalized.count(name) > 1}
        )
        raise IngestionError(
            f"{path.name}: duplicate column names after normalization: {duplicates}"
        )
    missing = [column for column in REQUIRED_COLUMNS if column not in normalized]
    if missing:
        raise IngestionError(
            f"{path.name}: missing required columns: {', '.join(missing)}"
        )
    unknown = [name for name in normalized if name not in REQUIRED_COLUMNS]
    return normalized, unknown


def load_csv(path: Path, settings: Settings) -> IngestionResult:
    """Read and parse one measurement CSV file."""

    path = Path(path)
    if not path.exists():
        raise IngestionError(f"Input file does not exist: {path}")
    if not path.is_file():
        raise IngestionError(f"Input path is not a file: {path}")

    digest = file_sha256(path)
    columns, unknown_columns = _validate_header(path)

    try:
        raw = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            na_values=[],
            encoding="utf-8-sig",
        )
    except UnicodeDecodeError as exc:
        raise IngestionError(
            f"{path.name}: file is not valid UTF-8; convert it to UTF-8 first"
        ) from exc
    except pd.errors.EmptyDataError as exc:
        raise IngestionError(f"{path.name}: file contains no data rows") from exc
    except pd.errors.ParserError as exc:
        raise IngestionError(f"{path.name}: malformed CSV: {exc}") from exc
    except OSError as exc:
        raise IngestionError(f"{path.name}: cannot read file: {exc}") from exc

    if raw.empty:
        raise IngestionError(f"{path.name}: file contains no data rows")

    raw.columns = columns
    raw = raw.reset_index(drop=True)
    total_rows = len(raw)

    # Keep the original text for every required column.  This is what makes
    # malformed values and rejections traceable.
    original_text = raw.copy()
    original_text["source_row"] = np.arange(1, total_rows + 1)

    installation = raw["installation_id"].astype(str).str.strip()
    sensor = raw["sensor_id"].astype(str).str.strip()
    timestamps = parse_timestamp_series(raw["timestamp"], settings.input_timezone)

    data = pd.DataFrame(
        {
            "timestamp": timestamps,
            "installation_id": installation,
            "sensor_id": sensor,
        }
    )
    data["source_row"] = np.arange(1, total_rows + 1)

    parse_error_count = 0
    missing_value_count = 0
    for metric in METRIC_COLUMNS:
        parsed = [parse_numeric_value(value) for value in raw[metric]]
        data[metric] = [number for number, _ in parsed]
        data[f"{metric}__parse_error"] = [error for _, error in parsed]
        data[f"{metric}__raw"] = raw[metric].astype(str)
        parse_error_count += int(data[f"{metric}__parse_error"].sum())
        missing_value_count += int(
            (
                (raw[metric].astype(str).str.strip() == "")
                & ~data[f"{metric}__parse_error"]
            ).sum()
        )

    rejection_reasons: list[str] = []
    for row_index in range(total_rows):
        reasons: list[str] = []
        if pd.isna(data.at[row_index, "timestamp"]):
            reasons.append("invalid or missing timestamp")
        if not data.at[row_index, "installation_id"]:
            reasons.append("missing installation_id")
        if not data.at[row_index, "sensor_id"]:
            reasons.append("missing sensor_id")
        rejection_reasons.append("; ".join(reasons))

    reason_series = pd.Series(rejection_reasons, index=data.index)
    rejected_mask = reason_series != ""
    rejected_indices = data.index[rejected_mask]
    rejected = pd.DataFrame(
        {
            "source_row": data.loc[rejected_mask, "source_row"].astype(int),
            "reason": reason_series[rejected_mask],
            "raw_data": [
                json.dumps(
                    {
                        column: str(original_text.at[index, column])
                        for column in original_text.columns
                    },
                    ensure_ascii=False,
                )
                for index in rejected_indices
            ],
        }
    ).reset_index(drop=True)

    valid = data.loc[~rejected_mask].copy().reset_index(drop=True)
    valid["timestamp"] = pd.to_datetime(valid["timestamp"], utc=True)

    if valid.empty:
        duplicate_rows = 0
        long = _to_long(valid, path.name)
    else:
        duplicate_rows = int(
            valid.duplicated(
                subset=["installation_id", "sensor_id", "timestamp"], keep="first"
            ).sum()
        )
        long = _to_long(valid, path.name)

    issues: list[str] = []
    if rejected_rows := len(rejected):
        issues.append(f"{rejected_rows} row(s) rejected (see rejected-record output)")
    if duplicate_rows:
        issues.append(f"{duplicate_rows} duplicate row(s) within this file")
    if parse_error_count:
        issues.append(f"{parse_error_count} unparseable numeric value(s)")
    if missing_value_count:
        issues.append(f"{missing_value_count} blank measurement value(s)")
    if unknown_columns:
        issues.append(f"ignored unknown column(s): {', '.join(unknown_columns)}")

    stats: dict[str, Any] = {
        "source_file": path.name,
        "file_sha256": digest,
        "total_rows": total_rows,
        "valid_rows": len(valid),
        "rejected_rows": len(rejected),
        "duplicate_rows": duplicate_rows,
        "missing_values": missing_value_count,
        "numeric_parse_errors": parse_error_count,
        "timestamp_parse_errors": int(data["timestamp"].isna().sum()),
        "unknown_columns": unknown_columns,
        "installations": sorted(valid["installation_id"].unique().tolist()),
        "sensors": sorted(valid["sensor_id"].unique().tolist()),
        "total_measurements": len(long),
    }

    return IngestionResult(
        path=path,
        file_sha256=digest,
        wide=valid,
        long=long,
        rejected=rejected,
        stats=stats,
        issues=issues,
    )


def _to_long(wide: pd.DataFrame, source_file: str) -> pd.DataFrame:
    """Convert a wide parsed frame to one row per measurement channel."""

    if wide.empty:
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
            ]
        )

    parts: list[pd.DataFrame] = []
    for metric in METRIC_COLUMNS:
        parts.append(
            pd.DataFrame(
                {
                    "installation_id": wide["installation_id"].to_numpy(),
                    "sensor_id": wide["sensor_id"].to_numpy(),
                    "sensor_code": (
                        wide["sensor_id"].astype(str) + "::" + metric
                    ).to_numpy(),
                    "metric": metric,
                    "unit": METRIC_UNITS[metric],
                    "timestamp": wide["timestamp"].to_numpy(),
                    "value": wide[metric].to_numpy(dtype=float),
                    "raw_value_text": wide[f"{metric}__raw"].to_numpy(),
                    "value_parse_error": wide[
                        f"{metric}__parse_error"
                    ].to_numpy(dtype=bool),
                    "source_file": source_file,
                    "source_row": wide["source_row"].to_numpy(dtype=int),
                }
            )
        )
    long = pd.concat(parts, ignore_index=True)
    return long.sort_values(
        ["source_row", "metric"], kind="stable"
    ).reset_index(drop=True)
