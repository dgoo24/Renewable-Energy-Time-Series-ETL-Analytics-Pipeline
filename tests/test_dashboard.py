"""Tests for the static HTML dashboard."""

from __future__ import annotations

import re
from dataclasses import replace

from src.config import Settings
from src.dashboard import generate_dashboard


def _write_dashboard_inputs(settings: Settings) -> None:
    settings.csv_dir.mkdir(parents=True, exist_ok=True)
    (settings.csv_dir / "installations.csv").write_text(
        "installation_id,name,location,capacity_kw\n"
        'INST_A,North Roof Array,Research Campus North,10.0\n'
        'INST_B,"Array <script>alert(1)</script>",South Field,5.5\n',
        encoding="utf-8",
    )
    (settings.csv_dir / "statistics.csv").write_text(
        "installation_id,metric,unit,count,mean,median,min,max,std,"
        "imputed_count,imputed_fraction\n"
        "INST_A,active_power_kw,kW,96,5.0,5.0,0.0,10.0,1.0,2,0.0208\n"
        "INST_A,irradiance_w_m2,W/m2,96,500.0,500.0,0.0,900.0,10.0,1,0.0104\n"
        "INST_B,active_power_kw,kW,48,3.0,3.0,0.0,6.0,0.5,0,0.0\n",
        encoding="utf-8",
    )
    (settings.csv_dir / "pr_summary.csv").write_text(
        "installation_id,capacity_kw,n_days,n_months,mean_daily_pr,"
        "median_daily_pr,mean_monthly_pr,overall_pr,total_energy_kwh,"
        "total_irradiation_kwh_m2,mean_coverage,imputed_fraction,status\n"
        "INST_A,10.0,365,12,0.8204,0.8202,0.8194,0.8150,12880.8,1580.5,"
        "0.9990,0.0040,ok\n"
        "INST_B,5.5,180,6,0.7904,0.7902,0.7895,0.7795,5543.8,1293.1,"
        "0.9991,0.0040,ok\n",
        encoding="utf-8",
    )
    (settings.csv_dir / "pr_monthly.csv").write_text(
        "installation_id,period,energy_kwh,irradiation_kwh_m2,performance_ratio,"
        "coverage,imputed_fraction,quality_flag\n"
        "INST_A,2023-01-01 00:00:00+00:00,1000.0,120.0,0.82,0.99,0.004,ok\n"
        "INST_A,2023-02-01 00:00:00+00:00,1100.0,130.0,0.81,0.99,0.004,ok\n",
        encoding="utf-8",
    )
    (settings.csv_dir / "trends.csv").write_text(
        "installation_id,n_months,first_period,last_period,slope_per_year,"
        "intercept,r_squared,p_value,stderr,status\n"
        "INST_A,12,2023-01-01 00:00:00+00:00,2023-12-01 00:00:00+00:00,"
        "-0.012,0.8249,0.0253,0.6215,0.0236,ok\n",
        encoding="utf-8",
    )
    (settings.csv_dir / "quality_reports.csv").write_text(
        "source_file,total_records,valid_measurements,suspicious_measurements,"
        "invalid_measurements,missing_measurements,duplicate_records,"
        "rejected_records,issues\n"
        'day.csv,96,380,1,2,1,0,0,"[{""code"": ""invalid_value"", '
        '""severity"": ""warning"", ""count"": 2, '
        '""description"": ""physically impossible value""}]"\n',
        encoding="utf-8",
    )
    (settings.csv_dir / "quality_issues.csv").write_text(
        "code,severity,count,description\n"
        "invalid_value,warning,2,physically impossible value\n",
        encoding="utf-8",
    )
    plot = settings.plots_dir / "INST_A_active_power.png"
    plot.parent.mkdir(parents=True, exist_ok=True)
    plot.write_bytes(b"\x89PNG\r\n\x1a\n")


def test_dashboard_is_written_and_linked(settings: Settings) -> None:
    _write_dashboard_inputs(settings)

    path = generate_dashboard(settings)

    assert path == settings.output_dir / "dashboard.html"
    assert path.exists() and path.stat().st_size > 0
    text = path.read_text(encoding="utf-8")

    assert text.startswith("<!DOCTYPE html>")
    assert 'lang="de"' in text
    assert "Synthetische Beispieldaten" in text
    assert "Performance Ratio" in text
    assert 'id="plot-search"' in text
    assert "INST_A" in text

    # German number formatting for the KPI cards and tables.
    assert "12.880,80" in text
    assert "99,90 %" in text

    # The dashboard only links to files that exist next to it.
    references = re.findall(r'(?:src|href)="([^"]+)"', text)
    assert "plots/INST_A_active_power.png" in references
    assert "csv/statistics.csv" in references
    assert "INST_A – Wirkleistung" in text
    for reference in references:
        if reference.startswith(("http", "#")):
            continue
        assert (path.parent / reference).exists(), reference

    # User-supplied values are escaped, never injected as markup.
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_dashboard_handles_missing_outputs(settings: Settings) -> None:
    path = generate_dashboard(settings)

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Keine Anlagenmetadaten gefunden." in text or "Keine Daten verfügbar." in text
    assert "Noch keine Diagramme vorhanden." in text
    assert "Noch keine CSV-Dateien vorhanden." in text


def test_standalone_dashboard_embeds_plots(settings: Settings) -> None:
    _write_dashboard_inputs(settings)
    target = settings.output_dir / "site" / "index.html"

    path = generate_dashboard(
        settings,
        embed_images=True,
        include_csv_links=False,
        path=target,
    )

    assert path == target
    text = path.read_text(encoding="utf-8")
    assert "data:image/png;base64," in text
    assert 'href="plots/' not in text
    assert 'href="csv/' not in text
    assert "Online-Demo" in text
    assert "Die CSV-Exporte sind in der Online-Demo nicht enthalten." in text


def test_dashboard_respects_non_synthetic_provenance(settings: Settings) -> None:
    real = replace(settings, data_provenance="MEASURED DATA - internal research")

    text = generate_dashboard(real).read_text(encoding="utf-8")

    assert "Datenherkunft:" in text
    assert "MEASURED DATA - internal research" in text
    assert "Synthetische Beispieldaten" not in text
    assert "keine realen Messungen" not in text
