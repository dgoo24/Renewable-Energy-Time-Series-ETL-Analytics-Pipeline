"""Reporting tests: CSV, PNG and Markdown generation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.analytics import compute_analytics
from src.config import InstallationMeta, Settings
from src.reports import generate_reports
from src.validation import quality_report_frame
from tests.conftest import day_rows, run_file_pipeline, write_csv


def _small_report(settings: Settings, tmp_path: Path):
    rows = day_rows()
    # One physically invalid irradiance value so the quality-issue aggregation
    # and per-file quality table contain real content.
    rows[5]["irradiance_w_m2"] = -5.0
    path = write_csv(tmp_path / "report_day.csv", rows)
    ingestion, validation, cleaned = run_file_pipeline(
        path,
        settings,
        {
            "INST_A": InstallationMeta(
                installation_id="INST_A",
                name="North Roof Array",
                location="Research Campus North",
                capacity_kw=10.0,
            )
        },
    )
    analytics = compute_analytics(
        cleaned.long,
        settings,
        {
            "INST_A": InstallationMeta(
                installation_id="INST_A",
                name="North Roof Array",
                location="Research Campus North",
                capacity_kw=10.0,
            )
        },
    )
    quality = quality_report_frame(validation.report)
    file_summary = pd.DataFrame(
        [
            {
                "source_file": ingestion.source_file,
                "status": "processed",
                "total_rows": ingestion.stats["total_rows"],
                "rejected_rows": ingestion.stats["rejected_rows"],
                "duplicate_rows": ingestion.stats["duplicate_rows"],
                "message": "test",
            }
        ]
    )
    outputs = generate_reports(
        analytics,
        quality,
        cleaned.missing_intervals,
        settings,
        file_summary=file_summary,
        processing_warnings=cleaned.warnings,
        processed_long=cleaned.long,
    )
    return outputs, analytics


def test_reports_are_written(settings: Settings, tmp_path: Path) -> None:
    outputs, analytics = _small_report(settings, tmp_path)

    assert outputs.report_path.exists()
    assert outputs.report_path.name == "summary_report.md"
    assert len(outputs.csv_paths) >= 10
    assert len(outputs.plot_paths) >= 3
    for path in outputs.csv_paths:
        assert path.exists() and path.stat().st_size > 0
    for path in outputs.plot_paths:
        assert path.is_file() and path.suffix == ".png"
        assert path.stat().st_size > 0

    # A processed-measurement export is included when requested.
    assert (settings.csv_dir / "processed_measurements.csv").exists()
    assert (settings.csv_dir / "quality_issues.csv").exists()
    assert (settings.csv_dir / "statistics.csv").exists()
    assert (settings.csv_dir / "pr_daily.csv").exists()
    assert (settings.csv_dir / "trends.csv").exists()


def test_markdown_contains_provenance_and_caveats(
    settings: Settings, tmp_path: Path
) -> None:
    outputs, _ = _small_report(settings, tmp_path)
    text = outputs.report_path.read_text(encoding="utf-8")

    assert "SYNTHETIC EXAMPLE DATA" in text
    assert "not a record of measurements from a real" in text
    assert "## 6. Assumptions and limitations" in text
    assert "not proof of physical" in text
    assert "Performance Ratio" in text
    assert "INST_A" in text
    assert "output/csv/statistics.csv" in text


def test_quality_issue_aggregation(settings: Settings, tmp_path: Path) -> None:
    outputs, _ = _small_report(settings, tmp_path)
    issues = pd.read_csv(settings.csv_dir / "quality_issues.csv")
    assert not issues.empty
    assert {"code", "severity", "count", "description"}.issubset(issues.columns)

    report_text = outputs.report_path.read_text(encoding="utf-8")
    assert "Data quality" in report_text
