"""Photovoltaic analytics.

The module works on *processed* long-format measurements.  It pivots them to wide
per-installation frames, calculates descriptive statistics and period aggregates,
and computes a simplified Performance Ratio (PR) only when the required inputs
and coverage exist.

Energy integration uses the configured sampling interval as a rectangular
approximation.  Missing samples contribute no energy; this is conservative and
is reported through period coverage.

PR definition
-------------
    final yield       Yf = E_ac [kWh] / P_nom [kWp]          (hours)
    reference yield   Yr = H_poa [kWh/m²] / G_stc [kW/m²]    (hours)
    performance ratio PR = Yf / Yr

with ``G_stc = 1 kW/m²``.  Irradiance is assumed to be measured in the
plane of the array; this is an explicit assumption, not verified by the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from src.config import METRIC_COLUMNS, METRIC_UNITS, Settings


@dataclass
class AnalyticsResult:
    statistics: pd.DataFrame
    daily: pd.DataFrame
    monthly: pd.DataFrame
    pr_daily: pd.DataFrame
    pr_monthly: pd.DataFrame
    pr_summary: pd.DataFrame
    pr_comparison: pd.DataFrame
    trends: pd.DataFrame
    reduced_periods: pd.DataFrame
    seasonal: pd.DataFrame
    installations: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    wide: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)


STAT_COLUMNS = [
    "installation_id",
    "metric",
    "unit",
    "count",
    "missing_count",
    "imputed_count",
    "imputed_fraction",
    "mean",
    "median",
    "min",
    "max",
    "std",
]

PERIOD_COLUMNS = [
    "installation_id",
    "period",
    "period_type",
    "metric",
    "unit",
    "count",
    "expected_slots",
    "coverage",
    "mean",
    "median",
    "min",
    "max",
    "std",
    "energy_kwh",
    "irradiation_kwh_m2",
    "imputed_count",
]

PR_COLUMNS = [
    "installation_id",
    "period",
    "period_type",
    "capacity_kw",
    "energy_kwh",
    "irradiation_kwh_m2",
    "final_yield_h",
    "reference_yield_h",
    "performance_ratio",
    "coverage",
    "imputed_fraction",
    "daylight_slots",
    "quality_flag",
]

PR_SUMMARY_COLUMNS = [
    "installation_id",
    "capacity_kw",
    "n_days",
    "n_months",
    "mean_daily_pr",
    "median_daily_pr",
    "mean_monthly_pr",
    "overall_pr",
    "total_energy_kwh",
    "total_irradiation_kwh_m2",
    "mean_coverage",
    "imputed_fraction",
    "status",
]

TREND_COLUMNS = [
    "installation_id",
    "n_months",
    "first_period",
    "last_period",
    "slope_per_year",
    "intercept",
    "r_squared",
    "p_value",
    "stderr",
    "status",
]

REDUCED_COLUMNS = [
    "installation_id",
    "period",
    "performance_ratio",
    "installation_mean_pr",
    "threshold",
    "deviation_from_mean",
]

SEASONAL_COLUMNS = [
    "installation_id",
    "calendar_month",
    "season",
    "mean_pr",
    "median_pr",
    "n_days",
]

SEASONS = {
    12: "winter",
    1: "winter",
    2: "winter",
    3: "spring",
    4: "spring",
    5: "spring",
    6: "summer",
    7: "summer",
    8: "summer",
    9: "autumn",
    10: "autumn",
    11: "autumn",
}


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def _interval_minutes(settings: Settings) -> float:
    frequency = settings.cleaning.resample_frequency
    if frequency:
        try:
            return float(pd.Timedelta(frequency).total_seconds() / 60.0)
        except (ValueError, TypeError):
            pass
    return float(settings.cleaning.expected_interval_minutes)


def prepare_wide(long: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Pivot processed long measurements to per-installation wide frames."""

    if long.empty:
        return {}
    required = {"installation_id", "timestamp", "metric", "processed_value"}
    if not required.issubset(long.columns):
        raise ValueError(f"Missing columns for analytics: {sorted(required - set(long.columns))}")

    data = long.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
    data = data.dropna(subset=["timestamp"])
    if "is_imputed" not in data.columns:
        data["is_imputed"] = False
    data["is_imputed"] = data["is_imputed"].fillna(False).astype(bool)

    frames: dict[str, pd.DataFrame] = {}
    for installation_id, group in data.groupby("installation_id", sort=True):
        values = group.pivot_table(
            index="timestamp",
            columns="metric",
            values="processed_value",
            aggfunc="mean",
        )
        imputed = group.pivot_table(
            index="timestamp",
            columns="metric",
            values="is_imputed",
            aggfunc="max",
        )
        imputed = imputed.reindex(columns=values.columns, fill_value=False).astype(bool)
        imputed.columns = [f"{metric}__imputed" for metric in imputed.columns]
        wide = values.join(imputed, how="outer").sort_index()
        for metric in METRIC_COLUMNS:
            if metric not in wide.columns:
                wide[metric] = np.nan
            if f"{metric}__imputed" not in wide.columns:
                wide[f"{metric}__imputed"] = False
        wide = wide[[*METRIC_COLUMNS, *[f"{m}__imputed" for m in METRIC_COLUMNS]]]
        wide.attrs["installation_id"] = str(installation_id)
        frames[str(installation_id)] = wide
    return frames


def _expected_slots(start: pd.Timestamp, end: pd.Timestamp, interval_minutes: float) -> int:
    if interval_minutes <= 0:
        return 0
    duration = (end - start).total_seconds() / 60.0
    return max(0, int(round(duration / interval_minutes)))


def _period_end(period: pd.Timestamp, period_type: str) -> pd.Timestamp:
    if period_type == "day":
        return period + pd.Timedelta(days=1)
    return period + pd.offsets.MonthBegin(1)


def _period_table(
    wide: pd.DataFrame,
    installation_id: str,
    settings: Settings,
    period_type: str,
) -> pd.DataFrame:
    frequency = "D" if period_type == "day" else "MS"
    interval_minutes = _interval_minutes(settings)
    interval_hours = interval_minutes / 60.0
    rows: list[dict[str, Any]] = []

    for metric in METRIC_COLUMNS:
        if metric not in wide.columns:
            continue
        series = wide[metric]
        if series.notna().sum() == 0:
            continue
        imputed = (
            wide[f"{metric}__imputed"]
            if f"{metric}__imputed" in wide.columns
            else pd.Series(False, index=wide.index)
        )
        value_frame = pd.DataFrame({"value": series, "imputed": imputed})
        grouped = value_frame.resample(frequency)
        summary = grouped["value"].agg(
            ["count", "mean", "median", "min", "max", "std"]
        )
        imputed_counts = grouped["imputed"].sum()
        contribution = (series * interval_hours).resample(frequency).sum(min_count=1)

        for period, row in summary.iterrows():
            expected = _expected_slots(
                period, _period_end(period, period_type), interval_minutes
            )
            count = int(row["count"])
            coverage = min(1.0, count / expected) if expected else np.nan
            rows.append(
                {
                    "installation_id": installation_id,
                    "period": period,
                    "period_type": period_type,
                    "metric": metric,
                    "unit": METRIC_UNITS[metric],
                    "count": count,
                    "expected_slots": expected,
                    "coverage": coverage,
                    "mean": row["mean"],
                    "median": row["median"],
                    "min": row["min"],
                    "max": row["max"],
                    "std": row["std"],
                    "energy_kwh": contribution.loc[period]
                    if metric == "active_power_kw"
                    else np.nan,
                    "irradiation_kwh_m2": contribution.loc[period] / 1000.0
                    if metric == "irradiance_w_m2"
                    else np.nan,
                    "imputed_count": int(imputed_counts.loc[period]),
                }
            )
    return pd.DataFrame(rows, columns=PERIOD_COLUMNS)


def _pr_table(
    wide: pd.DataFrame,
    installation_id: str,
    capacity_kw: float | None,
    settings: Settings,
    period_type: str,
) -> pd.DataFrame:
    if not {"active_power_kw", "irradiance_w_m2"}.issubset(wide.columns):
        return _empty(PR_COLUMNS)
    if capacity_kw is None or not np.isfinite(capacity_kw) or capacity_kw <= 0:
        return _empty(PR_COLUMNS)

    frequency = "D" if period_type == "day" else "MS"
    interval_minutes = _interval_minutes(settings)
    interval_hours = interval_minutes / 60.0
    power = wide["active_power_kw"]
    irradiance = wide["irradiance_w_m2"]
    paired = power.notna() & irradiance.notna()

    power_imputed = wide.get(
        "active_power_kw__imputed", pd.Series(False, index=wide.index)
    )
    irradiance_imputed = wide.get(
        "irradiance_w_m2__imputed", pd.Series(False, index=wide.index)
    )
    any_imputed = (power_imputed | irradiance_imputed) & paired

    energy = (
        (power.where(paired) * interval_hours)
        .resample(frequency)
        .sum(min_count=1)
    )
    irradiation = (
        (irradiance.where(paired) * interval_hours)
        .resample(frequency)
        .sum(min_count=1)
        / 1000.0
    )
    paired_count = paired.resample(frequency).sum()
    imputed_count = any_imputed.resample(frequency).sum()
    daylight_count = (
        (irradiance.where(paired) >= settings.analytics.pr_min_irradiance_w_m2)
        .resample(frequency)
        .sum()
    )

    rows: list[dict[str, Any]] = []
    for period in energy.index:
        period_energy = energy.loc[period]
        period_irradiation = irradiation.loc[period]
        count = int(paired_count.loc[period])
        expected = _expected_slots(
            period, _period_end(period, period_type), interval_minutes
        )
        coverage = min(1.0, count / expected) if expected else np.nan
        imputed_fraction = (
            float(imputed_count.loc[period]) / count if count else np.nan
        )
        daylight_slots = int(daylight_count.loc[period])

        quality_flag = "ok"
        if count == 0 or pd.isna(period_energy) or pd.isna(period_irradiation):
            quality_flag = "missing_inputs"
        elif period_irradiation <= 0.001:
            quality_flag = "no_irradiance"
        elif pd.isna(coverage) or coverage < settings.analytics.pr_min_coverage:
            quality_flag = "insufficient_coverage"
        elif daylight_slots == 0:
            quality_flag = "no_daylight"

        performance_ratio = np.nan
        final_yield = np.nan
        reference_yield = np.nan
        if quality_flag == "ok":
            final_yield = float(period_energy) / capacity_kw
            reference_yield = float(period_irradiation) / 1.0
            if reference_yield > 0:
                performance_ratio = final_yield / reference_yield
            else:
                quality_flag = "no_irradiance"

        rows.append(
            {
                "installation_id": installation_id,
                "period": period,
                "period_type": period_type,
                "capacity_kw": capacity_kw,
                "energy_kwh": period_energy,
                "irradiation_kwh_m2": period_irradiation,
                "final_yield_h": final_yield,
                "reference_yield_h": reference_yield,
                "performance_ratio": performance_ratio,
                "coverage": coverage,
                "imputed_fraction": imputed_fraction,
                "daylight_slots": daylight_slots,
                "quality_flag": quality_flag,
            }
        )
    return pd.DataFrame(rows, columns=PR_COLUMNS)


def _statistics(wide: pd.DataFrame, installation_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric in METRIC_COLUMNS:
        if metric not in wide.columns:
            continue
        series = wide[metric].dropna()
        imputed = (
            wide[f"{metric}__imputed"]
            if f"{metric}__imputed" in wide.columns
            else pd.Series(False, index=wide.index)
        )
        count = int(len(series))
        rows.append(
            {
                "installation_id": installation_id,
                "metric": metric,
                "unit": METRIC_UNITS[metric],
                "count": count,
                "missing_count": int(len(wide) - count),
                "imputed_count": int(imputed.sum()),
                "imputed_fraction": round(float(imputed.sum()) / count, 4)
                if count
                else np.nan,
                "mean": series.mean() if count else np.nan,
                "median": series.median() if count else np.nan,
                "min": series.min() if count else np.nan,
                "max": series.max() if count else np.nan,
                "std": series.std(ddof=1) if count > 1 else np.nan,
            }
        )
    return rows


def _meta_value(meta: Any, name: str) -> Any:
    if meta is None:
        return None
    if isinstance(meta, Mapping):
        return meta.get(name)
    return getattr(meta, name, None)


def _installation_frame(
    installations: Mapping[str, Any] | None,
    installation_ids: list[str],
) -> pd.DataFrame:
    rows = []
    for code in installation_ids:
        meta = installations.get(code) if installations else None
        rows.append(
            {
                "installation_id": code,
                "name": _meta_value(meta, "name"),
                "location": _meta_value(meta, "location"),
                "capacity_kw": _meta_value(meta, "capacity_kw"),
            }
        )
    return pd.DataFrame(rows)


def missing_intervals_from_wide(
    wide: Mapping[str, pd.DataFrame],
    expected_interval_minutes: int,
) -> pd.DataFrame:
    """Find contiguous intervals where every channel of an installation is missing."""

    columns = [
        "installation_id",
        "start_timestamp",
        "end_timestamp",
        "duration_minutes",
        "missing_slots",
    ]
    rows: list[dict[str, Any]] = []
    for installation_id, frame in wide.items():
        if frame.empty:
            continue
        metrics = [metric for metric in METRIC_COLUMNS if metric in frame]
        if not metrics:
            continue
        all_missing = frame[metrics].isna().all(axis=1)
        if not all_missing.any():
            continue
        run_ids = (all_missing != all_missing.shift(fill_value=False)).cumsum()
        for run_id in run_ids[all_missing].unique():
            positions = frame.index[run_ids == run_id]
            start = positions[0]
            end = positions[-1]
            duration = (
                (end - start).total_seconds() / 60.0 + expected_interval_minutes
            )
            rows.append(
                {
                    "installation_id": installation_id,
                    "start_timestamp": start,
                    "end_timestamp": end,
                    "duration_minutes": round(duration, 2),
                    "missing_slots": len(positions),
                }
            )
    return pd.DataFrame(rows, columns=columns)


def compute_analytics(
    processed_long: pd.DataFrame,
    settings: Settings,
    installations: Mapping[str, Any] | None = None,
) -> AnalyticsResult:
    """Compute all analytics tables from processed measurements."""

    wide = prepare_wide(processed_long)
    warnings: list[str] = []
    notes = [
        "Energy uses a rectangular integration with the configured sampling interval.",
        "Performance Ratio assumes irradiance is measured in the plane of the array.",
        "A statistical PR trend is not evidence of physical module degradation.",
    ]
    if not wide:
        return AnalyticsResult(
            statistics=_empty(STAT_COLUMNS),
            daily=_empty(PERIOD_COLUMNS),
            monthly=_empty(PERIOD_COLUMNS),
            pr_daily=_empty(PR_COLUMNS),
            pr_monthly=_empty(PR_COLUMNS),
            pr_summary=_empty(PR_SUMMARY_COLUMNS),
            pr_comparison=pd.DataFrame(),
            trends=_empty(TREND_COLUMNS),
            reduced_periods=_empty(REDUCED_COLUMNS),
            seasonal=_empty(SEASONAL_COLUMNS),
            installations=_empty(
                ["installation_id", "name", "location", "capacity_kw"]
            ),
            warnings=["No processed measurements available for analytics."],
            notes=notes,
        )

    installation_ids = sorted(wide.keys())
    stat_rows: list[dict[str, Any]] = []
    daily_frames: list[pd.DataFrame] = []
    monthly_frames: list[pd.DataFrame] = []
    pr_daily_frames: list[pd.DataFrame] = []
    pr_monthly_frames: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    trend_rows: list[dict[str, Any]] = []
    reduced_frames: list[pd.DataFrame] = []
    seasonal_frames: list[pd.DataFrame] = []

    for installation_id in installation_ids:
        frame = wide[installation_id]
        meta = installations.get(installation_id) if installations else None
        capacity = _meta_value(meta, "capacity_kw")
        stat_rows.extend(_statistics(frame, installation_id))
        daily = _period_table(frame, installation_id, settings, "day")
        monthly = _period_table(frame, installation_id, settings, "month")
        daily_frames.append(daily)
        monthly_frames.append(monthly)

        if capacity is None or not np.isfinite(capacity) or capacity <= 0:
            warnings.append(
                f"{installation_id}: Performance Ratio skipped because capacity_kw "
                "is missing or invalid."
            )
        pr_daily = _pr_table(frame, installation_id, capacity, settings, "day")
        pr_monthly = _pr_table(frame, installation_id, capacity, settings, "month")
        pr_daily_frames.append(pr_daily)
        pr_monthly_frames.append(pr_monthly)

        valid_daily = pr_daily[pr_daily["performance_ratio"].notna()].copy()
        valid_monthly = pr_monthly[pr_monthly["performance_ratio"].notna()].copy()
        if valid_daily.empty:
            status = (
                "missing_capacity"
                if capacity is None or not np.isfinite(capacity) or capacity <= 0
                else "no_valid_periods"
            )
            summary_rows.append(
                {
                    "installation_id": installation_id,
                    "capacity_kw": capacity,
                    "n_days": 0,
                    "n_months": 0,
                    "status": status,
                }
            )
        else:
            total_energy = float(valid_daily["energy_kwh"].sum())
            total_irradiation = float(valid_daily["irradiation_kwh_m2"].sum())
            overall_pr = (
                total_energy / (capacity * total_irradiation)
                if capacity and total_irradiation > 0
                else np.nan
            )
            imputed_fraction = (
                float(
                    valid_daily["imputed_fraction"].fillna(0).mean()
                )
                if len(valid_daily)
                else np.nan
            )
            summary_rows.append(
                {
                    "installation_id": installation_id,
                    "capacity_kw": capacity,
                    "n_days": int(len(valid_daily)),
                    "n_months": int(len(valid_monthly)),
                    "mean_daily_pr": float(valid_daily["performance_ratio"].mean()),
                    "median_daily_pr": float(
                        valid_daily["performance_ratio"].median()
                    ),
                    "mean_monthly_pr": float(
                        valid_monthly["performance_ratio"].mean()
                    )
                    if len(valid_monthly)
                    else np.nan,
                    "overall_pr": overall_pr,
                    "total_energy_kwh": total_energy,
                    "total_irradiation_kwh_m2": total_irradiation,
                    "mean_coverage": float(valid_daily["coverage"].mean()),
                    "imputed_fraction": imputed_fraction,
                    "status": "ok",
                }
            )

        if not valid_daily.empty:
            imputed_share = float(valid_daily["imputed_fraction"].fillna(0).mean())
            if imputed_share > 0.10:
                warnings.append(
                    f"{installation_id}: {imputed_share:.1%} of PR input intervals "
                    "were imputed; interpret PR with caution."
                )
            skipped = int((pr_daily["quality_flag"] != "ok").sum())
            if len(pr_daily) and skipped / len(pr_daily) > 0.25:
                warnings.append(
                    f"{installation_id}: PR could not be calculated for "
                    f"{skipped}/{len(pr_daily)} days (coverage or input constraints)."
                )

        # Linear trend on monthly PR.
        trend_status = "ok"
        if len(valid_monthly) < settings.analytics.min_months_for_trend:
            trend_status = "insufficient_data"
            trend_row = {
                "installation_id": installation_id,
                "n_months": int(len(valid_monthly)),
                "status": trend_status,
            }
        else:
            first = valid_monthly["period"].min()
            x = (
                (valid_monthly["period"] - first).dt.total_seconds()
                / (365.25 * 24.0 * 3600.0)
            ).to_numpy(dtype=float)
            y = valid_monthly["performance_ratio"].to_numpy(dtype=float)
            regression = scipy_stats.linregress(x, y)
            trend_row = {
                "installation_id": installation_id,
                "n_months": int(len(valid_monthly)),
                "first_period": first,
                "last_period": valid_monthly["period"].max(),
                "slope_per_year": float(regression.slope),
                "intercept": float(regression.intercept),
                "r_squared": float(regression.rvalue**2),
                "p_value": float(regression.pvalue),
                "stderr": float(regression.stderr),
                "status": trend_status,
            }
        trend_rows.append(trend_row)

        # Months below mean - one standard deviation.
        if len(valid_monthly) >= 3:
            mean_pr = float(valid_monthly["performance_ratio"].mean())
            std_pr = float(valid_monthly["performance_ratio"].std(ddof=1))
            threshold = mean_pr - std_pr
            reduced = valid_monthly[
                valid_monthly["performance_ratio"] < threshold
            ].copy()
            if not reduced.empty:
                reduced["installation_mean_pr"] = mean_pr
                reduced["threshold"] = threshold
                reduced["deviation_from_mean"] = (
                    reduced["performance_ratio"] - mean_pr
                )
                reduced_frames.append(
                    reduced[
                        [
                            "installation_id",
                            "period",
                            "performance_ratio",
                            "installation_mean_pr",
                            "threshold",
                            "deviation_from_mean",
                        ]
                    ]
                )

        if not valid_daily.empty:
            seasonal = valid_daily.copy()
            seasonal["calendar_month"] = seasonal["period"].dt.month
            seasonal["season"] = seasonal["calendar_month"].map(SEASONS)
            seasonal = (
                seasonal.groupby(
                    ["installation_id", "calendar_month", "season"], as_index=False
                )
                .agg(
                    mean_pr=("performance_ratio", "mean"),
                    median_pr=("performance_ratio", "median"),
                    n_days=("performance_ratio", "count"),
                )
                .sort_values(["installation_id", "calendar_month"])
            )
            seasonal_frames.append(seasonal)

    daily = (
        pd.concat(daily_frames, ignore_index=True)
        if daily_frames
        else _empty(PERIOD_COLUMNS)
    )
    monthly = (
        pd.concat(monthly_frames, ignore_index=True)
        if monthly_frames
        else _empty(PERIOD_COLUMNS)
    )
    pr_daily = (
        pd.concat(pr_daily_frames, ignore_index=True)
        if pr_daily_frames
        else _empty(PR_COLUMNS)
    )
    pr_monthly = (
        pd.concat(pr_monthly_frames, ignore_index=True)
        if pr_monthly_frames
        else _empty(PR_COLUMNS)
    )
    pr_summary = pd.DataFrame(summary_rows, columns=PR_SUMMARY_COLUMNS)
    trends = pd.DataFrame(trend_rows, columns=TREND_COLUMNS)
    reduced_periods = (
        pd.concat(reduced_frames, ignore_index=True)
        if reduced_frames
        else _empty(REDUCED_COLUMNS)
    )
    seasonal = (
        pd.concat(seasonal_frames, ignore_index=True)
        if seasonal_frames
        else _empty(SEASONAL_COLUMNS)
    )

    if not pr_monthly.empty:
        pr_comparison = pr_monthly.pivot_table(
            index="period",
            columns="installation_id",
            values="performance_ratio",
            aggfunc="mean",
        ).sort_index()
    else:
        pr_comparison = pd.DataFrame()

    return AnalyticsResult(
        statistics=pd.DataFrame(stat_rows, columns=STAT_COLUMNS),
        daily=daily,
        monthly=monthly,
        pr_daily=pr_daily,
        pr_monthly=pr_monthly,
        pr_summary=pr_summary,
        pr_comparison=pr_comparison,
        trends=trends,
        reduced_periods=reduced_periods,
        seasonal=seasonal,
        installations=_installation_frame(installations, installation_ids),
        warnings=warnings,
        notes=notes,
        wide=wide,
    )
