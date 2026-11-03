"""CSV, PNG and Markdown reporting.

The reporting layer is deliberately separate from analytics and database code.
It receives an :class:`~src.analytics.AnalyticsResult` plus quality/missingness
tables and writes human- and machine-readable artefacts.

All plots use the non-interactive ``Agg`` backend and are saved as PNG files.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from src.analytics import AnalyticsResult  # noqa: E402
from src.config import METRIC_COLUMNS, METRIC_LABELS, METRIC_UNITS, Settings  # noqa: E402


@dataclass
class ReportOutputs:
    report_path: Path
    csv_paths: list[Path] = field(default_factory=list)
    plot_paths: list[Path] = field(default_factory=list)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "installation"


def _provenance_short(settings: Settings) -> str:
    if "synthetic" in settings.data_provenance.lower():
        return "SYNTHETIC EXAMPLE DATA - NOT REAL MEASUREMENTS"
    return settings.data_provenance


def _add_footer(fig: plt.Figure, settings: Settings) -> None:
    fig.text(
        0.995,
        0.005,
        _provenance_short(settings),
        ha="right",
        va="bottom",
        fontsize=7,
        color="#777777",
    )


def _save_figure(fig: plt.Figure, path: Path, settings: Settings) -> Path:
    _add_footer(fig, settings)
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=settings.plot_dpi, bbox_inches="tight")
    plt.close(fig)
    return path


def _format_date_axis(ax: plt.Axes) -> None:
    locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))


def _installation_label(analytics: AnalyticsResult, installation_id: str) -> str:
    if analytics.installations.empty:
        return installation_id
    row = analytics.installations[
        analytics.installations["installation_id"] == installation_id
    ]
    if row.empty:
        return installation_id
    name = row.iloc[0].get("name")
    if isinstance(name, str) and name.strip():
        return f"{installation_id} - {name}"
    return installation_id


def _q(value: Any, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return ""
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    if isinstance(value, (float, np.floating)):
        return f"{value:,.{digits}f}"
    return str(value)


def _md_table(
    frame: pd.DataFrame,
    columns: Sequence[str] | None = None,
    digits: int = 3,
    max_rows: int | None = None,
) -> str:
    if frame is None or frame.empty:
        return "_No data._"
    if columns is not None:
        available = [column for column in columns if column in frame.columns]
        frame = frame[available]
    if max_rows is not None and len(frame) > max_rows:
        frame = frame.head(max_rows)
    headers = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for _, row in frame.iterrows():
        cells = []
        for value in row:
            text = _q(value, digits).replace("|", "\\|")
            if len(text) > 90:
                text = text[:87] + "..."
            cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _load_issues(quality_reports: pd.DataFrame) -> pd.DataFrame:
    aggregate: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"severity": "", "count": 0, "description": ""}
    )
    if quality_reports is None or quality_reports.empty or "issues" not in quality_reports:
        return pd.DataFrame(columns=["code", "severity", "count", "description"])
    for value in quality_reports["issues"]:
        if not isinstance(value, str):
            continue
        try:
            issues = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code", "unknown"))
            entry = aggregate[code]
            entry["severity"] = issue.get("severity", entry["severity"])
            entry["description"] = issue.get("description", entry["description"])
            entry["count"] += int(issue.get("count", 0))
    rows = [{"code": code, **values} for code, values in aggregate.items()]
    frame = pd.DataFrame(rows, columns=["code", "severity", "count", "description"])
    if not frame.empty:
        frame = frame.sort_values(["severity", "count"], ascending=[True, False])
    return frame


def _quality_totals(quality_reports: pd.DataFrame) -> dict[str, int]:
    if quality_reports is None or quality_reports.empty:
        return {}
    columns = [
        "total_records",
        "total_measurements",
        "valid_measurements",
        "invalid_measurements",
        "suspicious_measurements",
        "missing_measurements",
        "missing_values",
        "duplicate_records",
        "duplicate_measurements",
        "missing_intervals",
        "missing_slots",
        "irregular_intervals",
        "rejected_records",
    ]
    return {
        column: int(pd.to_numeric(quality_reports[column], errors="coerce").fillna(0).sum())
        for column in columns
        if column in quality_reports
    }


def _processed_counts(analytics: AnalyticsResult) -> dict[str, int]:
    values = 0
    slots = 0
    imputed = 0
    for frame in analytics.wide.values():
        slots += len(frame) * len(METRIC_COLUMNS)
        for metric in METRIC_COLUMNS:
            if metric in frame:
                values += int(frame[metric].notna().sum())
            flag = f"{metric}__imputed"
            if flag in frame:
                imputed += int(frame[flag].sum())
    return {
        "processed_values": values,
        "total_slots": slots,
        "imputed_values": imputed,
        "missing_values": max(0, slots - values),
    }


def _period(analytics: AnalyticsResult) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    first: pd.Timestamp | None = None
    last: pd.Timestamp | None = None
    for frame in analytics.wide.values():
        if frame.empty:
            continue
        current_first = frame.index.min()
        current_last = frame.index.max()
        first = current_first if first is None else min(first, current_first)
        last = current_last if last is None else max(last, current_last)
    return first, last


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _plot_power_or_irradiance(
    analytics: AnalyticsResult,
    installation_id: str,
    metric: str,
    path: Path,
    settings: Settings,
) -> Path | None:
    frame = analytics.wide.get(installation_id)
    if frame is None or metric not in frame or frame[metric].notna().sum() == 0:
        return None
    fig, ax = plt.subplots(figsize=(11, 3.8))
    ax.plot(frame.index, frame[metric], linewidth=0.7, color="#1f77b4")
    ax.set_title(
        f"{_installation_label(analytics, installation_id)} - "
        f"{METRIC_LABELS[metric]} time series"
    )
    ax.set_ylabel(f"{METRIC_LABELS[metric]} [{METRIC_UNITS[metric]}]")
    ax.set_xlabel("UTC time")
    ax.grid(True, alpha=0.25)
    _format_date_axis(ax)
    return _save_figure(fig, path, settings)


def _plot_temperatures(
    analytics: AnalyticsResult,
    installation_id: str,
    path: Path,
    settings: Settings,
) -> Path | None:
    frame = analytics.wide.get(installation_id)
    if frame is None:
        return None
    available = [
        metric
        for metric in ("module_temperature_c", "ambient_temperature_c")
        if metric in frame and frame[metric].notna().any()
    ]
    if not available:
        return None
    fig, ax = plt.subplots(figsize=(11, 3.8))
    colors = {"module_temperature_c": "#d62728", "ambient_temperature_c": "#2ca02c"}
    for metric in available:
        ax.plot(
            frame.index,
            frame[metric],
            linewidth=0.7,
            label=METRIC_LABELS[metric],
            color=colors[metric],
        )
    ax.set_title(f"{_installation_label(analytics, installation_id)} - temperatures")
    ax.set_ylabel("Temperature [degC]")
    ax.set_xlabel("UTC time")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.25)
    _format_date_axis(ax)
    return _save_figure(fig, path, settings)


def _plot_daily_energy(
    analytics: AnalyticsResult,
    installation_id: str,
    path: Path,
    settings: Settings,
) -> Path | None:
    table = analytics.daily[
        (analytics.daily["installation_id"] == installation_id)
        & (analytics.daily["metric"] == "active_power_kw")
    ].copy()
    if table.empty:
        return None
    fig, ax = plt.subplots(figsize=(11, 3.8))
    ax.bar(table["period"], table["energy_kwh"], width=0.8, color="#ff7f0e")
    ax.set_title(f"{_installation_label(analytics, installation_id)} - daily energy")
    ax.set_ylabel("Energy [kWh/day]")
    ax.set_xlabel("UTC date")
    ax.grid(True, axis="y", alpha=0.25)
    _format_date_axis(ax)
    return _save_figure(fig, path, settings)


def _plot_monthly_pr(
    analytics: AnalyticsResult,
    installation_id: str,
    path: Path,
    settings: Settings,
) -> Path | None:
    table = analytics.pr_monthly[
        (analytics.pr_monthly["installation_id"] == installation_id)
        & analytics.pr_monthly["performance_ratio"].notna()
    ].copy()
    if table.empty:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.bar(table["period"], table["performance_ratio"], width=20, color="#2ca02c")
    ax.set_title(
        f"{_installation_label(analytics, installation_id)} - monthly Performance Ratio"
    )
    ax.set_ylabel("Performance Ratio [-]")
    ax.set_xlabel("UTC month")
    ax.set_ylim(0, max(1.0, float(table["performance_ratio"].max()) * 1.1))
    ax.grid(True, axis="y", alpha=0.25)
    _format_date_axis(ax)
    return _save_figure(fig, path, settings)


def _plot_quality_overview(
    quality_reports: pd.DataFrame, path: Path, settings: Settings
) -> Path | None:
    if quality_reports is None or quality_reports.empty:
        return None
    reports = quality_reports.drop_duplicates(subset=["source_file"], keep="first")
    reports = reports.sort_values("source_file")
    labels = [str(name)[:34] for name in reports["source_file"]]
    categories = [
        ("valid_measurements", "valid", "#2ca02c"),
        ("suspicious_measurements", "suspicious", "#ffbf00"),
        ("invalid_measurements", "invalid", "#d62728"),
        ("missing_measurements", "missing", "#7f7f7f"),
    ]
    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.1), 4.2))
    bottom = np.zeros(len(reports))
    positions = np.arange(len(reports))
    for column, label, color in categories:
        series = (
            reports[column]
            if column in reports
            else pd.Series(0, index=reports.index)
        )
        values = pd.to_numeric(series, errors="coerce").fillna(0).to_numpy()
        ax.bar(positions, values, bottom=bottom, label=label, color=color)
        bottom += values
    ax.set_xticks(positions, labels, rotation=35, ha="right", fontsize=8)
    ax.set_ylabel("Measurements")
    ax.set_title("Data quality by source file")
    ax.legend(fontsize=8)
    ax.grid(True, axis="y", alpha=0.25)
    return _save_figure(fig, path, settings)


def _plot_missing_heatmap(
    analytics: AnalyticsResult, path: Path, settings: Settings
) -> Path | None:
    rows: list[pd.Series] = []
    labels: list[str] = []
    for installation_id, frame in analytics.wide.items():
        metrics = [metric for metric in METRIC_COLUMNS if metric in frame]
        if not metrics or frame.empty:
            continue
        fraction = frame[metrics].isna().mean(axis=1).resample("MS").mean()
        rows.append(fraction)
        labels.append(installation_id)
    if not rows:
        return None
    table = pd.concat(rows, axis=1).T
    table.index = labels
    values = table.to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(max(8, len(table.columns) * 0.55), max(2.5, len(rows) * 0.8)))
    image = ax.imshow(values, aspect="auto", cmap="YlOrRd", vmin=0, vmax=1)
    ax.set_yticks(np.arange(len(labels)), labels)
    month_labels = [pd.Timestamp(column).strftime("%Y-%m") for column in table.columns]
    ax.set_xticks(np.arange(len(month_labels)), month_labels, rotation=45, ha="right", fontsize=8)
    ax.set_title("Missing measurement fraction by installation and month")
    fig.colorbar(image, ax=ax, label="fraction missing")
    if values.size <= 250:
        for row in range(values.shape[0]):
            for column in range(values.shape[1]):
                if np.isfinite(values[row, column]):
                    ax.text(
                        column,
                        row,
                        f"{values[row, column]:.2f}",
                        ha="center",
                        va="center",
                        fontsize=6,
                        color="black",
                    )
    return _save_figure(fig, path, settings)


def _plot_missing_by_metric(
    analytics: AnalyticsResult, path: Path, settings: Settings
) -> Path | None:
    if not analytics.wide:
        return None
    rows = []
    for installation_id, frame in analytics.wide.items():
        for metric in METRIC_COLUMNS:
            if metric in frame:
                rows.append(
                    {
                        "installation": installation_id,
                        "metric": metric,
                        "missing_fraction": float(frame[metric].isna().mean()),
                    }
                )
    table = pd.DataFrame(rows)
    if table.empty:
        return None
    pivot = table.pivot(index="metric", columns="installation", values="missing_fraction")
    pivot = pivot.reindex([metric for metric in METRIC_COLUMNS if metric in pivot.index])
    fig, ax = plt.subplots(figsize=(10, 4.0))
    pivot.plot(kind="bar", ax=ax, color=plt.cm.tab10.colors, width=0.8)
    ax.set_ylabel("Fraction missing")
    ax.set_xlabel("")
    ax.set_title("Missing fraction by measurement channel")
    ax.set_xticklabels(
        [METRIC_LABELS.get(metric, metric) for metric in pivot.index],
        rotation=20,
        ha="right",
    )
    ax.legend(fontsize=8, title="installation")
    ax.grid(True, axis="y", alpha=0.25)
    return _save_figure(fig, path, settings)


def _plot_pr_comparison(
    analytics: AnalyticsResult, path: Path, settings: Settings
) -> Path | None:
    if analytics.pr_comparison is None or analytics.pr_comparison.empty:
        return None
    fig, ax = plt.subplots(figsize=(10, 4.0))
    for column in analytics.pr_comparison.columns:
        series = analytics.pr_comparison[column].dropna()
        if series.empty:
            continue
        ax.plot(series.index, series.values, marker="o", markersize=3, label=str(column))
    ax.set_title("Monthly Performance Ratio comparison")
    ax.set_ylabel("Performance Ratio [-]")
    ax.set_xlabel("UTC month")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    _format_date_axis(ax)
    return _save_figure(fig, path, settings)


def _plot_pr_trend(
    analytics: AnalyticsResult, path: Path, settings: Settings
) -> Path | None:
    if analytics.pr_monthly.empty:
        return None
    frame = analytics.pr_monthly[analytics.pr_monthly["performance_ratio"].notna()]
    if frame.empty:
        return None
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for installation_id, group in frame.groupby("installation_id", sort=True):
        group = group.sort_values("period")
        ax.plot(
            group["period"],
            group["performance_ratio"],
            marker="o",
            markersize=3,
            label=str(installation_id),
        )
        if len(group) >= 3:
            first = group["period"].min()
            x = (
                (group["period"] - first).dt.total_seconds()
                / (365.25 * 24.0 * 3600.0)
            ).to_numpy(dtype=float)
            y = group["performance_ratio"].to_numpy(dtype=float)
            slope, intercept = np.polyfit(x, y, 1)
            ax.plot(
                group["period"],
                intercept + slope * x,
                linestyle="--",
                linewidth=1.2,
                color=ax.lines[-1].get_color(),
                alpha=0.85,
            )
    ax.set_title("Monthly Performance Ratio and linear trend (statistical only)")
    ax.set_ylabel("Performance Ratio [-]")
    ax.set_xlabel("UTC month")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    _format_date_axis(ax)
    return _save_figure(fig, path, settings)


def _plot_seasonal(
    analytics: AnalyticsResult, path: Path, settings: Settings
) -> Path | None:
    if analytics.seasonal.empty:
        return None
    pivot = analytics.seasonal.pivot_table(
        index="calendar_month",
        columns="installation_id",
        values="mean_pr",
        aggfunc="mean",
    )
    if pivot.empty:
        return None
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    fig, ax = plt.subplots(figsize=(10, 4.0))
    pivot.plot(kind="bar", ax=ax, width=0.8, color=plt.cm.tab10.colors)
    ax.set_xticks(
        np.arange(len(pivot.index)),
        [month_names[int(month) - 1] for month in pivot.index],
    )
    ax.set_title("Seasonal Performance Ratio by calendar month")
    ax.set_ylabel("Mean daily Performance Ratio [-]")
    ax.set_xlabel("")
    ax.legend(fontsize=8, title="installation")
    ax.grid(True, axis="y", alpha=0.25)
    return _save_figure(fig, path, settings)


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


def generate_reports(
    analytics: AnalyticsResult,
    quality_reports: pd.DataFrame,
    missing_intervals: pd.DataFrame,
    settings: Settings,
    *,
    file_summary: pd.DataFrame | None = None,
    processing_warnings: Iterable[str] | None = None,
    processed_long: pd.DataFrame | None = None,
) -> ReportOutputs:
    """Generate all CSV, PNG and Markdown outputs."""

    settings.ensure_directories()
    csv_paths: list[Path] = []
    plot_paths: list[Path] = []

    # Analytical tables.
    tables: list[tuple[str, pd.DataFrame]] = [
        ("statistics.csv", analytics.statistics),
        ("daily_summary.csv", analytics.daily),
        ("monthly_summary.csv", analytics.monthly),
        ("pr_daily.csv", analytics.pr_daily),
        ("pr_monthly.csv", analytics.pr_monthly),
        ("pr_summary.csv", analytics.pr_summary),
        ("trends.csv", analytics.trends),
        ("reduced_performance_periods.csv", analytics.reduced_periods),
        ("seasonal_pr.csv", analytics.seasonal),
        ("installations.csv", analytics.installations),
    ]
    if not analytics.pr_comparison.empty:
        comparison = analytics.pr_comparison.copy()
        comparison.index.name = "period"
        tables.append(("pr_comparison.csv", comparison.reset_index()))
    if quality_reports is not None and not quality_reports.empty:
        tables.append(("quality_reports.csv", quality_reports))
        issues = _load_issues(quality_reports)
        tables.append(("quality_issues.csv", issues))
    if missing_intervals is not None and not missing_intervals.empty:
        tables.append(("missing_intervals.csv", missing_intervals))
    if file_summary is not None and not file_summary.empty:
        tables.append(("file_processing_summary.csv", file_summary))
    if processed_long is not None and not processed_long.empty:
        export_columns = [
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
        available = [column for column in export_columns if column in processed_long]
        tables.append(
            ("processed_measurements.csv", processed_long[available].copy())
        )

    for filename, frame in tables:
        if frame is None:
            continue
        csv_paths.append(_write_csv(frame, settings.csv_dir / filename))

    # Per-installation plots.
    for installation_id, frame in sorted(analytics.wide.items()):
        safe = _safe_name(installation_id)
        power = _plot_power_or_irradiance(
            analytics,
            installation_id,
            "active_power_kw",
            settings.plots_dir / f"{safe}_active_power.png",
            settings,
        )
        if power:
            plot_paths.append(power)
        irradiance = _plot_power_or_irradiance(
            analytics,
            installation_id,
            "irradiance_w_m2",
            settings.plots_dir / f"{safe}_irradiance.png",
            settings,
        )
        if irradiance:
            plot_paths.append(irradiance)
        temperature = _plot_temperatures(
            analytics,
            installation_id,
            settings.plots_dir / f"{safe}_temperatures.png",
            settings,
        )
        if temperature:
            plot_paths.append(temperature)
        daily = _plot_daily_energy(
            analytics,
            installation_id,
            settings.plots_dir / f"{safe}_daily_energy.png",
            settings,
        )
        if daily:
            plot_paths.append(daily)
        monthly_pr = _plot_monthly_pr(
            analytics,
            installation_id,
            settings.plots_dir / f"{safe}_monthly_pr.png",
            settings,
        )
        if monthly_pr:
            plot_paths.append(monthly_pr)

    # Combined quality and comparison plots.
    for maybe_path in (
        _plot_quality_overview(
            quality_reports, settings.plots_dir / "quality_overview.png", settings
        ),
        _plot_missing_heatmap(
            analytics, settings.plots_dir / "missing_values_heatmap.png", settings
        ),
        _plot_missing_by_metric(
            analytics, settings.plots_dir / "missing_values_by_metric.png", settings
        ),
        _plot_pr_comparison(
            analytics, settings.plots_dir / "pr_comparison.png", settings
        ),
        _plot_pr_trend(analytics, settings.plots_dir / "pr_trend.png", settings),
        _plot_seasonal(analytics, settings.plots_dir / "seasonal_pr.png", settings),
    ):
        if maybe_path:
            plot_paths.append(maybe_path)

    report_path = _write_markdown_report(
        analytics,
        quality_reports,
        missing_intervals,
        settings,
        csv_paths=csv_paths,
        plot_paths=plot_paths,
        file_summary=file_summary,
        processing_warnings=list(processing_warnings or []),
    )
    return ReportOutputs(report_path=report_path, csv_paths=csv_paths, plot_paths=plot_paths)


def _write_markdown_report(
    analytics: AnalyticsResult,
    quality_reports: pd.DataFrame,
    missing_intervals: pd.DataFrame,
    settings: Settings,
    *,
    csv_paths: list[Path],
    plot_paths: list[Path],
    file_summary: pd.DataFrame | None,
    processing_warnings: list[str],
) -> Path:
    generated_at = datetime.now(timezone.utc)
    first, last = _period(analytics)
    counts = _processed_counts(analytics)
    quality_totals = _quality_totals(quality_reports)
    issues = _load_issues(quality_reports)
    synthetic = "synthetic" in settings.data_provenance.lower()

    lines: list[str] = []
    lines.append("# Renewable Energy Time-Series ETL & Analytics Report")
    lines.append("")
    lines.append(f"> **Data provenance:** {settings.data_provenance}")
    lines.append("")
    if synthetic:
        lines.append(
            "> **Important:** this report uses synthetic example data. It is not a "
            "record of measurements from a real photovoltaic facility."
        )
        lines.append("")
    lines.append(f"**Generated (UTC):** {generated_at.isoformat(timespec='seconds')}")
    lines.append("")

    lines.append("## 1. Scope and processing period")
    lines.append("")
    if first is not None and last is not None:
        lines.append(
            f"- Processing period: **{first.isoformat()}** to **{last.isoformat()}** (UTC)."
        )
    else:
        lines.append("- No processed time period is available.")
    lines.append(f"- Installations analysed: **{len(analytics.wide)}**.")
    lines.append(
        f"- Processed measurement values: **{counts['processed_values']:,}** "
        f"of {counts['total_slots']:,} grid cells "
        f"({counts['processed_values'] / counts['total_slots']:.2%} coverage)."
        if counts["total_slots"]
        else "- Processed measurement values: 0."
    )
    lines.append(f"- Imputed values: **{counts['imputed_values']:,}**.")
    if quality_totals:
        lines.append(
            f"- Source records processed: **{quality_totals.get('total_records', 0):,}**; "
            f"rejected rows: **{quality_totals.get('rejected_records', 0):,}**."
        )
    lines.append("")

    if not analytics.installations.empty:
        lines.append("### Installations")
        lines.append("")
        installations = analytics.installations.copy()
        installations["first_timestamp"] = installations["installation_id"].map(
            lambda code: analytics.wide[code].index.min()
            if code in analytics.wide and not analytics.wide[code].empty
            else None
        )
        installations["last_timestamp"] = installations["installation_id"].map(
            lambda code: analytics.wide[code].index.max()
            if code in analytics.wide and not analytics.wide[code].empty
            else None
        )
        lines.append(
            _md_table(
                installations,
                columns=[
                    "installation_id",
                    "name",
                    "location",
                    "capacity_kw",
                    "first_timestamp",
                    "last_timestamp",
                ],
                digits=2,
            )
        )
        lines.append("")

    lines.append("## 2. Input files")
    lines.append("")
    if file_summary is not None and not file_summary.empty:
        lines.append(
            _md_table(
                file_summary,
                columns=[
                    "source_file",
                    "status",
                    "total_rows",
                    "rejected_rows",
                    "duplicate_rows",
                    "message",
                ],
                digits=0,
            )
        )
    elif quality_reports is not None and not quality_reports.empty:
        lines.append(
            _md_table(
                quality_reports.drop_duplicates("source_file"),
                columns=[
                    "source_file",
                    "total_records",
                    "valid_measurements",
                    "invalid_measurements",
                    "suspicious_measurements",
                    "missing_measurements",
                    "duplicate_records",
                    "rejected_records",
                ],
                digits=0,
            )
        )
    else:
        lines.append("_No input-file information available._")
    lines.append("")

    lines.append("## 3. Data quality")
    lines.append("")
    if quality_totals:
        summary = pd.DataFrame(
            [
                {
                    "records": quality_totals.get("total_records", 0),
                    "measurements": quality_totals.get("total_measurements", 0),
                    "valid": quality_totals.get("valid_measurements", 0),
                    "suspicious": quality_totals.get("suspicious_measurements", 0),
                    "invalid": quality_totals.get("invalid_measurements", 0),
                    "missing": quality_totals.get("missing_measurements", 0),
                    "duplicate_records": quality_totals.get("duplicate_records", 0),
                    "missing_intervals": quality_totals.get("missing_intervals", 0),
                    "rejected_rows": quality_totals.get("rejected_records", 0),
                }
            ]
        )
        lines.append(_md_table(summary, digits=0))
        lines.append("")
    if not issues.empty:
        lines.append("**Detected quality problems (aggregated over files):**")
        lines.append("")
        lines.append(_md_table(issues, columns=["code", "severity", "count", "description"], digits=0))
        lines.append("")
    if missing_intervals is not None and not missing_intervals.empty:
        lines.append(
            f"**Fully missing intervals:** {len(missing_intervals)} interval(s), "
            f"{int(pd.to_numeric(missing_intervals['missing_slots'], errors='coerce').fillna(0).sum()):,} "
            "estimated missing grid slots. See `missing_intervals.csv`."
        )
        lines.append("")
    lines.append(
        "Invalid values are retained in `measurements_raw`; they are never silently "
        "deleted. Suspicious values remain available in processed data with a "
        "`suspicious` status."
    )
    lines.append("")

    lines.append("## 4. Analytical results")
    lines.append("")
    lines.append("### Basic statistics")
    lines.append("")
    lines.append(
        _md_table(
            analytics.statistics,
            columns=[
                "installation_id",
                "metric",
                "unit",
                "count",
                "mean",
                "median",
                "min",
                "max",
                "std",
                "imputed_fraction",
            ],
            digits=3,
        )
    )
    lines.append("")

    lines.append("### Performance Ratio")
    lines.append("")
    pr_available = (
        not analytics.pr_summary.empty
        and "mean_daily_pr" in analytics.pr_summary
        and analytics.pr_summary["mean_daily_pr"].notna().any()
    )
    if not pr_available:
        lines.append(
            "_Performance Ratio was not calculated. It requires capacity, active "
            "power, irradiance and sufficient coverage._"
        )
    else:
        lines.append(
            _md_table(
                analytics.pr_summary,
                columns=[
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
                    "status",
                ],
                digits=3,
            )
        )
    lines.append("")
    lines.append(
        "PR is only reported for periods with at least "
        f"{settings.analytics.pr_min_coverage:.0%} paired power/irradiance coverage. "
        "Irradiance is assumed to be plane-of-array."
    )
    lines.append("")

    if not analytics.pr_monthly.empty:
        monthly = analytics.pr_monthly[
            analytics.pr_monthly["performance_ratio"].notna()
        ].copy()
        if not monthly.empty:
            lines.append("### Monthly Performance Ratio")
            lines.append("")
            monthly["period"] = monthly["period"].dt.strftime("%Y-%m")
            lines.append(
                _md_table(
                    monthly,
                    columns=[
                        "installation_id",
                        "period",
                        "energy_kwh",
                        "irradiation_kwh_m2",
                        "performance_ratio",
                        "coverage",
                        "imputed_fraction",
                        "quality_flag",
                    ],
                    digits=3,
                    max_rows=60,
                )
            )
            lines.append("")

    lines.append("### Statistical trend")
    lines.append("")
    if analytics.trends.empty:
        lines.append("_Not enough monthly PR values for a trend._")
    else:
        lines.append(
            _md_table(
                analytics.trends,
                columns=[
                    "installation_id",
                    "n_months",
                    "slope_per_year",
                    "r_squared",
                    "p_value",
                    "status",
                ],
                digits=4,
            )
        )
    lines.append("")
    lines.append(
        "A slope is a statistical association in this dataset, not proof of physical "
        "module degradation. Seasonal effects, soiling, outages and missing data can "
        "all produce trends."
    )
    lines.append("")

    if not analytics.reduced_periods.empty:
        lines.append("### Periods of reduced monthly performance")
        lines.append("")
        reduced = analytics.reduced_periods.copy()
        reduced["period"] = reduced["period"].dt.strftime("%Y-%m")
        lines.append(
            _md_table(
                reduced,
                columns=[
                    "installation_id",
                    "period",
                    "performance_ratio",
                    "installation_mean_pr",
                    "threshold",
                    "deviation_from_mean",
                ],
                digits=3,
            )
        )
        lines.append("")

    if not analytics.seasonal.empty:
        lines.append("### Seasonal comparison")
        lines.append("")
        lines.append(
            _md_table(
                analytics.seasonal,
                columns=[
                    "installation_id",
                    "calendar_month",
                    "season",
                    "mean_pr",
                    "median_pr",
                    "n_days",
                ],
                digits=3,
                max_rows=48,
            )
        )
        lines.append("")

    if analytics.warnings or processing_warnings:
        lines.append("### Warnings")
        lines.append("")
        for warning in [*analytics.warnings, *processing_warnings]:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## 5. Generated output files")
    lines.append("")
    lines.append("### CSV tables")
    lines.append("")
    for path in csv_paths:
        lines.append(f"- `{_relative(path, settings.project_root)}`")
    lines.append("")
    lines.append("### Plots")
    lines.append("")
    for path in plot_paths:
        lines.append(f"- `{_relative(path, settings.project_root)}`")
    lines.append("")

    lines.append("## 6. Assumptions and limitations")
    lines.append("")
    lines.extend(
        [
            "- Timestamps are normalised to UTC. Naive input timestamps are interpreted "
            f"in `{settings.input_timezone}`.",
            "- Raw files and raw values are never overwritten. Processed data is derived "
            "and can be rebuilt from `measurements_raw`.",
            "- Duplicate measurements are resolved in the processed layer by keeping the "
            f"`{settings.cleaning.duplicate_keep}` occurrence after deterministic sorting; "
            "raw duplicates remain stored.",
            "- Imputation fills only internal gaps no longer than "
            f"{settings.cleaning.interpolation_max_gap_minutes} minutes, using the "
            f"`{settings.cleaning.missing_strategy}` strategy. Imputed values are flagged; "
            "no extrapolation is performed.",
            "- Energy uses rectangular integration with the configured sampling interval "
            f"({settings.cleaning.resample_frequency or settings.cleaning.expected_interval_minutes}). "
            "Missing samples contribute no energy and reduce reported coverage.",
            "- Performance Ratio uses AC power, nameplate capacity from installation "
            "metadata, and irradiance assumed to be measured in the plane of the array. "
            "It is not calculated when capacity, power, irradiance or coverage is "
            "insufficient.",
            "- Statistical trends are descriptive only and must not be reported as "
            "scientific evidence of module degradation.",
            "- Partial first/last days or months may be excluded from PR because their "
            "coverage is below the configured threshold.",
            "- If multiple source sensors exist for one metric, values are averaged per "
            "timestamp. This is reported as a cleaning warning.",
            "- The included example datasets are synthetic and must not be presented as "
            "real research measurements.",
        ]
    )
    lines.append("")

    report_path = settings.reports_dir / "summary_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
