"""Physical data-quality validation.

A *measurement* is one value of one channel at one timestamp.  A *record* is one
CSV data row (four measurements plus identifiers).  Validation never deletes rows;
it annotates each measurement with a quality status and produces a summary report.

Status definitions
------------------
* ``missing``   – blank source value (or no sample at all after gridding).
* ``invalid``   – unparseable or physically impossible value.
* ``suspicious``– physically possible but outside a configured plausibility range.
* ``valid``     – everything else.

Statistical outliers are deliberately **not** classified as invalid.  Only
explicit physical/plausibility thresholds are used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np
import pandas as pd

from src.config import METRIC_COLUMNS, Settings

VALID_STATUSES = ("valid", "suspicious", "invalid", "missing")

REASON_DESCRIPTIONS: dict[str, str] = {
    "missing_value": "Blank measurement value",
    "unparseable_numeric": "Unparseable numeric value",
    "negative_irradiance": "Negative irradiance (physically invalid)",
    "irradiance_above_max": "Irradiance above the configured physical maximum",
    "irradiance_above_suspicious_limit": "Irradiance above the plausibility limit",
    "power_below_min": "Active power below the configured minimum",
    "power_above_max": "Active power above the configured physical maximum",
    "negative_power_small": "Small negative active power (suspicious)",
    "power_above_capacity": "Active power above the installation capacity",
    "power_far_above_capacity": "Active power far above the installation capacity",
    "module_temperature_out_of_range": "Module temperature outside the physical range",
    "module_temperature_suspicious": "Module temperature outside the plausibility range",
    "ambient_temperature_out_of_range": "Ambient temperature outside the physical range",
    "ambient_temperature_suspicious": "Ambient temperature outside the plausibility range",
}

REASON_SEVERITY: dict[str, str] = {
    "missing_value": "missing",
    "unparseable_numeric": "invalid",
    "negative_irradiance": "invalid",
    "irradiance_above_max": "invalid",
    "irradiance_above_suspicious_limit": "suspicious",
    "power_below_min": "invalid",
    "power_above_max": "invalid",
    "negative_power_small": "suspicious",
    "power_above_capacity": "suspicious",
    "power_far_above_capacity": "invalid",
    "module_temperature_out_of_range": "invalid",
    "module_temperature_suspicious": "suspicious",
    "ambient_temperature_out_of_range": "invalid",
    "ambient_temperature_suspicious": "suspicious",
}


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    count: int
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "count": self.count,
            "description": self.description,
        }


@dataclass
class QualityReport:
    source_file: str
    total_records: int
    total_measurements: int
    valid_measurements: int
    invalid_measurements: int
    suspicious_measurements: int
    missing_measurements: int
    missing_values: int
    duplicate_records: int
    duplicate_measurements: int
    missing_intervals: int
    missing_slots: int
    irregular_intervals: int
    rejected_records: int
    issues: list[QualityIssue] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "total_records": self.total_records,
            "total_measurements": self.total_measurements,
            "valid_measurements": self.valid_measurements,
            "invalid_measurements": self.invalid_measurements,
            "suspicious_measurements": self.suspicious_measurements,
            "missing_measurements": self.missing_measurements,
            "missing_values": self.missing_values,
            "duplicate_records": self.duplicate_records,
            "duplicate_measurements": self.duplicate_measurements,
            "missing_intervals": self.missing_intervals,
            "missing_slots": self.missing_slots,
            "irregular_intervals": self.irregular_intervals,
            "rejected_records": self.rejected_records,
            "issues": [issue.to_dict() for issue in self.issues],
            "created_at": self.created_at.isoformat(),
        }

    def issues_json(self) -> str:
        import json

        return json.dumps([issue.to_dict() for issue in self.issues], ensure_ascii=False)

    def issues_text(self) -> str:
        if not self.issues:
            return "No quality problems detected."
        return "; ".join(
            f"{issue.count} x {issue.description}" for issue in self.issues
        )


@dataclass
class ValidationResult:
    long: pd.DataFrame
    report: QualityReport
    missing_intervals: pd.DataFrame
    reason_counts: dict[str, int] = field(default_factory=dict)


def detect_missing_intervals(
    long: pd.DataFrame,
    expected_interval_minutes: int,
    tolerance_seconds: float = 1.0,
) -> tuple[pd.DataFrame, int, int]:
    """Detect gaps in the timestamp sequence of each installation.

    Returns ``(gap_table, irregular_interval_count, missing_slot_count)``.
    Duplicate timestamps (zero-length differences) are excluded here; they are
    reported separately.
    """

    columns = [
        "installation_id",
        "previous_timestamp",
        "next_timestamp",
        "gap_minutes",
        "missing_slots",
    ]
    if long.empty:
        return pd.DataFrame(columns=columns), 0, 0

    expected_seconds = expected_interval_minutes * 60.0
    gap_threshold = expected_seconds * 1.5
    rows: list[dict[str, Any]] = []
    irregular = 0

    for installation_id, group in long.groupby("installation_id", sort=True):
        timestamps = (
            pd.Series(pd.to_datetime(group["timestamp"], utc=True).unique())
            .sort_values()
            .reset_index(drop=True)
        )
        if len(timestamps) < 2:
            continue
        differences = timestamps.diff().dt.total_seconds().iloc[1:].to_numpy()
        for offset, difference in enumerate(differences, start=1):
            if difference <= 0:
                continue
            if difference > gap_threshold:
                missing_slots = max(0, int(round(difference / expected_seconds)) - 1)
                rows.append(
                    {
                        "installation_id": installation_id,
                        "previous_timestamp": timestamps.iloc[offset - 1],
                        "next_timestamp": timestamps.iloc[offset],
                        "gap_minutes": round(difference / 60.0, 2),
                        "missing_slots": missing_slots,
                    }
                )
            elif abs(difference - expected_seconds) > tolerance_seconds:
                irregular += 1

    gap_table = pd.DataFrame(rows, columns=columns)
    missing_slots = int(gap_table["missing_slots"].sum()) if not gap_table.empty else 0
    return gap_table, irregular, missing_slots


def _set_status(
    status: pd.Series,
    reason: pd.Series,
    mask: pd.Series,
    new_status: str,
    reason_code: str,
) -> None:
    status.loc[mask] = new_status
    reason.loc[mask] = reason_code


def validate_measurements(
    long: pd.DataFrame,
    settings: Settings,
    capacities: Mapping[str, float] | None = None,
    *,
    source_file: str | None = None,
    total_records: int | None = None,
    rejected_records: int = 0,
) -> ValidationResult:
    """Annotate long-format measurements with quality statuses."""

    if long.empty:
        report = QualityReport(
            source_file=source_file or "unknown",
            total_records=total_records or 0,
            total_measurements=0,
            valid_measurements=0,
            invalid_measurements=0,
            suspicious_measurements=0,
            missing_measurements=0,
            missing_values=0,
            duplicate_records=0,
            duplicate_measurements=0,
            missing_intervals=0,
            missing_slots=0,
            irregular_intervals=0,
            rejected_records=rejected_records,
            issues=[],
        )
        return ValidationResult(
            long=long.copy(),
            report=report,
            missing_intervals=pd.DataFrame(
                columns=[
                    "installation_id",
                    "previous_timestamp",
                    "next_timestamp",
                    "gap_minutes",
                    "missing_slots",
                ]
            ),
        )

    data = long.copy()
    if "value_parse_error" not in data.columns:
        data["value_parse_error"] = False
    data["value_parse_error"] = data["value_parse_error"].fillna(False).astype(bool)
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)

    status = pd.Series("valid", index=data.index, dtype=object)
    reason = pd.Series("", index=data.index, dtype=object)
    config = settings.validation

    parse_error = data["value_parse_error"]
    blank = data["value"].isna() & ~parse_error
    _set_status(status, reason, parse_error, "invalid", "unparseable_numeric")
    _set_status(status, reason, blank, "missing", "missing_value")

    capacity = (
        data["installation_id"].map(capacities).astype(float)
        if capacities
        else pd.Series(np.nan, index=data.index)
    )
    value = data["value"]

    power = data["metric"] == "active_power_kw"
    if power.any():
        subset = power & ~parse_error & data["value"].notna()
        values = value[subset]
        caps = capacity[subset]
        invalid_cap = caps.notna() & (
            values > caps * config.power_capacity_invalid_factor
        )
        suspicious_negative = (values < 0) & (values >= config.power_min_kw)
        suspicious_cap = (
            caps.notna()
            & (values > caps * config.power_capacity_suspicious_factor)
            & ~invalid_cap
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & values.lt(config.power_min_kw),
            "invalid",
            "power_below_min",
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & values.gt(config.power_max_kw),
            "invalid",
            "power_above_max",
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & invalid_cap,
            "invalid",
            "power_far_above_capacity",
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & suspicious_negative,
            "suspicious",
            "negative_power_small",
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & suspicious_cap,
            "suspicious",
            "power_above_capacity",
        )

    irradiance = data["metric"] == "irradiance_w_m2"
    if irradiance.any():
        subset = irradiance & ~parse_error & data["value"].notna()
        values = value[subset]
        _set_status(
            status,
            reason,
            subset & reason.eq("") & values.lt(config.irradiance_min_w_m2),
            "invalid",
            "negative_irradiance",
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & values.gt(config.irradiance_max_w_m2),
            "invalid",
            "irradiance_above_max",
        )
        _set_status(
            status,
            reason,
            subset
            & reason.eq("")
            & values.gt(config.irradiance_suspicious_max_w_m2),
            "suspicious",
            "irradiance_above_suspicious_limit",
        )

    module_temp = data["metric"] == "module_temperature_c"
    if module_temp.any():
        subset = module_temp & ~parse_error & data["value"].notna()
        values = value[subset]
        invalid = (values < config.module_temp_invalid_min_c) | (
            values > config.module_temp_invalid_max_c
        )
        suspicious = (
            (values < config.module_temp_suspicious_min_c)
            | (values > config.module_temp_suspicious_max_c)
        ) & ~invalid
        _set_status(
            status,
            reason,
            subset & reason.eq("") & invalid,
            "invalid",
            "module_temperature_out_of_range",
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & suspicious,
            "suspicious",
            "module_temperature_suspicious",
        )

    ambient_temp = data["metric"] == "ambient_temperature_c"
    if ambient_temp.any():
        subset = ambient_temp & ~parse_error & data["value"].notna()
        values = value[subset]
        invalid = (values < config.ambient_temp_invalid_min_c) | (
            values > config.ambient_temp_invalid_max_c
        )
        suspicious = (
            (values < config.ambient_temp_suspicious_min_c)
            | (values > config.ambient_temp_suspicious_max_c)
        ) & ~invalid
        _set_status(
            status,
            reason,
            subset & reason.eq("") & invalid,
            "invalid",
            "ambient_temperature_out_of_range",
        )
        _set_status(
            status,
            reason,
            subset & reason.eq("") & suspicious,
            "suspicious",
            "ambient_temperature_suspicious",
        )

    data["quality_status"] = status
    data["status_reason"] = reason

    duplicate_mask = data.duplicated(
        subset=["installation_id", "sensor_id", "metric", "timestamp"], keep="first"
    )
    data["is_duplicate"] = duplicate_mask
    duplicate_measurements = int(duplicate_mask.sum())

    # Count duplicate CSV records (a record spans four measurement channels).
    record_keys = data.drop_duplicates(subset=["source_file", "source_row"])[
        ["installation_id", "sensor_id", "timestamp"]
    ]
    duplicate_records = int(
        record_keys.duplicated(
            subset=["installation_id", "sensor_id", "timestamp"], keep="first"
        ).sum()
    )

    missing_intervals, irregular_intervals, missing_slots = detect_missing_intervals(
        data, settings.cleaning.expected_interval_minutes
    )

    status_counts = data["quality_status"].value_counts()
    reason_counts = data.loc[
        data["status_reason"].ne(""), "status_reason"
    ].value_counts()
    # Missing values are blank source values; unparseable values are invalid.
    missing_values = int(data["status_reason"].eq("missing_value").sum())

    issues: list[QualityIssue] = []
    for code, count in reason_counts.items():
        safe_count = int(count)
        if safe_count <= 0:
            continue
        issues.append(
            QualityIssue(
                code=code,
                severity=REASON_SEVERITY.get(code, "warning"),
                count=safe_count,
                description=REASON_DESCRIPTIONS.get(code, code),
            )
        )
    if duplicate_measurements:
        issues.append(
            QualityIssue(
                "duplicate_measurements",
                "warning",
                duplicate_measurements,
                "Duplicate measurement rows (same sensor and timestamp)",
            )
        )
    if len(missing_intervals):
        issues.append(
            QualityIssue(
                "missing_intervals",
                "missing",
                len(missing_intervals),
                "Missing sampling interval(s)",
            )
        )
    if irregular_intervals:
        issues.append(
            QualityIssue(
                "irregular_sampling",
                "warning",
                irregular_intervals,
                "Irregular sampling interval(s)",
            )
        )
    if rejected_records:
        issues.append(
            QualityIssue(
                "rejected_records",
                "warning",
                rejected_records,
                "Row(s) rejected during ingestion (invalid timestamp or identifiers)",
            )
        )
    issues.sort(key=lambda item: (item.severity, -item.count, item.code))

    if total_records is None:
        total_records = int(
            data[["source_file", "source_row"]].drop_duplicates().shape[0]
        ) + rejected_records

    report = QualityReport(
        source_file=source_file or str(data["source_file"].iloc[0]),
        total_records=int(total_records),
        total_measurements=len(data),
        valid_measurements=int(status_counts.get("valid", 0)),
        invalid_measurements=int(status_counts.get("invalid", 0)),
        suspicious_measurements=int(status_counts.get("suspicious", 0)),
        missing_measurements=int(status_counts.get("missing", 0)),
        missing_values=missing_values,
        duplicate_records=int(duplicate_records),
        duplicate_measurements=duplicate_measurements,
        missing_intervals=len(missing_intervals),
        missing_slots=missing_slots,
        irregular_intervals=irregular_intervals,
        rejected_records=rejected_records,
        issues=issues,
    )

    return ValidationResult(
        long=data,
        report=report,
        missing_intervals=missing_intervals,
        reason_counts={str(key): int(value) for key, value in reason_counts.items()},
    )


def quality_report_frame(report: QualityReport) -> pd.DataFrame:
    """One-row tabular form of a quality report.

    ``issues`` is stored as JSON so that reports can aggregate issue codes;
    ``issue_summary`` is the human-readable form.
    """

    row = report.to_dict()
    row["issues"] = report.issues_json()
    row["issue_summary"] = report.issues_text()
    return pd.DataFrame([row])
