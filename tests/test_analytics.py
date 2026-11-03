"""Analytics tests: statistics, Energy, PR and trend guards."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analytics import (
    compute_analytics,
    missing_intervals_from_wide,
    prepare_wide,
)
from src.config import AnalyticsConfig, Settings
from tests.conftest import day_rows, measurement_row, run_file_pipeline, write_csv


def _analytics(settings: Settings, path: Path, capacity: float = 10.0):
    _, _, cleaned = run_file_pipeline(
        path,
        settings,
        {
            "INST_A": type(
                "Meta",
                (),
                {
                    "installation_id": "INST_A",
                    "name": "North Roof Array",
                    "location": "Research Campus North",
                    "capacity_kw": capacity,
                },
            )()
        },
    )
    return compute_analytics(
        cleaned.long,
        settings,
        {
            "INST_A": type(
                "Meta",
                (),
                {
                    "installation_id": "INST_A",
                    "name": "North Roof Array",
                    "location": "Research Campus North",
                    "capacity_kw": capacity,
                },
            )()
        },
    )


def test_performance_ratio_formula(settings: Settings, tmp_path: Path) -> None:
    # 5 kW for 24 h with 1000 W/m2 for 24 h and 10 kWp:
    #   Yf = 120 kWh / 10 kWp = 12 h
    #   Yr = 24 kWh/m2 / 1 kW/m2 = 24 h
    #   PR = 0.5
    path = write_csv(tmp_path / "day.csv", day_rows(power=5.0, irradiance=1000.0))
    analytics = _analytics(settings, path)

    daily = analytics.pr_daily[analytics.pr_daily["performance_ratio"].notna()]
    assert len(daily) == 1
    row = daily.iloc[0]
    assert row["energy_kwh"] == pytest.approx(120.0)
    assert row["irradiation_kwh_m2"] == pytest.approx(24.0)
    assert row["final_yield_h"] == pytest.approx(12.0)
    assert row["reference_yield_h"] == pytest.approx(24.0)
    assert row["performance_ratio"] == pytest.approx(0.5)
    assert row["quality_flag"] == "ok"

    summary = analytics.pr_summary.iloc[0]
    assert summary["status"] == "ok"
    assert summary["overall_pr"] == pytest.approx(0.5)
    assert summary["total_energy_kwh"] == pytest.approx(120.0)


def test_pr_skipped_without_capacity(settings: Settings, tmp_path: Path) -> None:
    path = write_csv(tmp_path / "day.csv", day_rows())
    analytics = _analytics(settings, path, capacity=float("nan"))

    summary = analytics.pr_summary.iloc[0]
    assert summary["status"] == "missing_capacity"
    assert pd.isna(summary["mean_daily_pr"])
    assert analytics.pr_daily["performance_ratio"].isna().all()
    assert any("capacity" in warning for warning in analytics.warnings)


def test_pr_skipped_for_insufficient_coverage(
    settings: Settings, tmp_path: Path
) -> None:
    # Only 10 of 96 expected slots, all valid -> coverage ~10.4 %.
    rows = day_rows(periods=10)
    path = write_csv(tmp_path / "partial.csv", rows)
    analytics = _analytics(settings, path)

    daily = analytics.pr_daily
    assert not daily.empty
    assert daily["performance_ratio"].isna().all()
    assert set(daily["quality_flag"]) == {"insufficient_coverage"}
    assert analytics.pr_summary.iloc[0]["status"] == "no_valid_periods"


def test_pr_skipped_without_irradiance(settings: Settings, tmp_path: Path) -> None:
    rows = day_rows(irradiance=0.0)
    path = write_csv(tmp_path / "dark.csv", rows)
    analytics = _analytics(settings, path)

    daily = analytics.pr_daily
    assert not daily.empty
    assert set(daily["quality_flag"]) == {"no_irradiance"}
    assert daily["performance_ratio"].isna().all()


def test_statistics_count_missing_and_imputed(
    settings: Settings, tmp_path: Path
) -> None:
    rows = day_rows()
    # Blank one power value to create an interpolated sample.
    rows[10]["active_power_kw"] = ""
    path = write_csv(tmp_path / "stats.csv", rows)
    analytics = _analytics(settings, path)

    power = analytics.statistics[
        analytics.statistics["metric"] == "active_power_kw"
    ].iloc[0]
    assert power["count"] == 96
    assert power["missing_count"] == 0
    assert power["imputed_count"] == 1
    assert power["imputed_fraction"] == pytest.approx(1 / 96, abs=1e-4)
    assert power["mean"] == pytest.approx(5.0)


def test_trend_requires_enough_months(settings: Settings, tmp_path: Path) -> None:
    path = write_csv(tmp_path / "one_day.csv", day_rows())
    analytics = _analytics(settings, path)

    trend = analytics.trends.iloc[0]
    assert trend["status"] == "insufficient_data"
    assert trend["n_months"] == 0
    assert pd.isna(trend["slope_per_year"])


def test_trend_is_calculated_from_monthly_pr(
    settings: Settings, tmp_path: Path
) -> None:
    rows: list[dict] = []
    for month, power in ((1, 5.0), (2, 4.5), (3, 4.0)):
        rows.extend(
            day_rows(
                date=f"2024-{month:02d}-01",
                power=power,
                irradiance=1000.0,
                periods=96,
            )
        )
    path = write_csv(tmp_path / "season.csv", rows)
    relaxed = Settings(
        **{
            **settings.__dict__,
            "analytics": AnalyticsConfig(pr_min_coverage=0.0),
        }
    )
    analytics = _analytics(relaxed, path)

    trend = analytics.trends.iloc[0]
    assert trend["status"] == "ok"
    assert trend["n_months"] == 3
    assert trend["slope_per_year"] < 0
    assert 0 <= trend["r_squared"] <= 1
    assert analytics.pr_monthly["performance_ratio"].notna().sum() == 3


def test_monthly_pr_excluded_for_partial_month(
    settings: Settings, tmp_path: Path
) -> None:
    path = write_csv(tmp_path / "one_day.csv", day_rows())
    analytics = _analytics(settings, path)

    monthly = analytics.pr_monthly.iloc[0]
    assert monthly["quality_flag"] == "insufficient_coverage"
    assert pd.isna(monthly["performance_ratio"])


def test_prepare_wide_and_missing_intervals() -> None:
    timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=4, freq="15min")
    long = pd.DataFrame(
        {
            "installation_id": ["INST_A"] * 8,
            "timestamp": list(timestamps) * 2,
            "metric": ["active_power_kw"] * 4 + ["irradiance_w_m2"] * 4,
            "processed_value": [1.0, 1.0, np.nan, np.nan]
            + [2.0, 2.0, np.nan, np.nan],
            "is_imputed": [False] * 8,
        }
    )
    wide = prepare_wide(long)
    frame = wide["INST_A"]
    assert frame.loc[timestamps[2], "active_power_kw"] is np.nan or pd.isna(
        frame.loc[timestamps[2], "active_power_kw"]
    )

    intervals = missing_intervals_from_wide(wide, 15)
    assert len(intervals) == 1
    assert intervals.iloc[0]["missing_slots"] == 2
    assert intervals.iloc[0]["duration_minutes"] == pytest.approx(30.0)


def test_empty_analytics_is_explicit(settings: Settings) -> None:
    analytics = compute_analytics(
        pd.DataFrame(
            columns=[
                "installation_id",
                "timestamp",
                "metric",
                "processed_value",
                "is_imputed",
            ]
        ),
        settings,
    )
    assert analytics.statistics.empty
    assert analytics.pr_summary.empty
    assert analytics.warnings == ["No processed measurements available for analytics."]
