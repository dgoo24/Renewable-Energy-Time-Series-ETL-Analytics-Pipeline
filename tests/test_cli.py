"""CLI tests that do not require a database."""

from __future__ import annotations

from pathlib import Path

from src.main import main
from tests.conftest import day_rows, write_csv


def test_process_skip_db_writes_reports(monkeypatch, tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "output"
    config_dir = tmp_path / "config"
    raw_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "installations.csv").write_text(
        "installation_id,name,location,capacity_kw\n"
        "INST_A,North Roof Array,Research Campus North,10.0\n",
        encoding="utf-8",
    )
    write_csv(raw_dir / "day.csv", day_rows())

    monkeypatch.setenv("INPUT_DIR", str(raw_dir))
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("INSTALLATIONS_FILE", str(config_dir / "installations.csv"))
    monkeypatch.setenv(
        "DATA_PROVENANCE", "SYNTHETIC EXAMPLE DATA - generated for tests"
    )

    assert main(["process", "--skip-db"]) == 0

    report = output_dir / "reports" / "summary_report.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "SYNTHETIC EXAMPLE DATA" in text
    assert "skip-db" in text
    assert (output_dir / "csv" / "pr_daily.csv").exists()
    assert list((output_dir / "plots").glob("*.png"))

    dashboard = output_dir / "dashboard.html"
    assert dashboard.exists()
    dashboard_text = dashboard.read_text(encoding="utf-8")
    assert "Renewable Energy Dashboard" in dashboard_text
    assert "Synthetische Beispieldaten" in dashboard_text
    assert "plots/" in dashboard_text

    # The dashboard can be rebuilt from the existing outputs without a database.
    dashboard.unlink()
    assert main(["dashboard", "--output-dir", str(output_dir)]) == 0
    assert dashboard.exists()

    # skip-db mode is explicitly non-idempotent: a second run re-ingests.
    assert main(["process", "--skip-db"]) == 0


def test_process_fails_without_input(monkeypatch, tmp_path: Path) -> None:
    raw_dir = tmp_path / "empty_raw"
    raw_dir.mkdir()
    monkeypatch.setenv("INPUT_DIR", str(raw_dir))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv(
        "INSTALLATIONS_FILE", str(tmp_path / "config" / "missing.csv")
    )
    assert main(["process", "--skip-db"]) == 1
