"""Static HTML dashboard generated from the report outputs.

The dashboard is a single static HTML file that references the PNG and CSV files
next to it.  It needs no web server, no JavaScript build step and no extra
dependency: open ``output/dashboard.html`` in a browser.

All data shown here is read back from the generated CSV files, so the dashboard
always matches the last successful ``process`` or ``report`` run.
"""

from __future__ import annotations

import base64
import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

import numpy as np
import pandas as pd

from src.config import Settings

PERCENT_COLUMNS = {
    "coverage",
    "imputed_fraction",
    "mean_coverage",
    "imputed_share",
}

BADGE_CLASSES = {
    "ok": "ok",
    "valid": "ok",
    "suspicious": "warn",
    "warning": "warn",
    "invalid": "bad",
    "failed": "bad",
    "missing": "muted",
    "imputed": "info",
    "insufficient_coverage": "muted",
    "no_irradiance": "muted",
    "no_daylight": "muted",
    "missing_inputs": "muted",
    "missing_capacity": "warn",
    "no_valid_periods": "warn",
    "insufficient_data": "muted",
}

PROVENANCE_WARNING = (
    "Alle Beispieldaten sind synthetisch. Sie sind keine Messungen einer realen "
    "Photovoltaikanlage und dürfen nicht als reale Forschungsdaten präsentiert werden."
)

PLOT_TITLES = {
    "quality_overview": "Datenqualität pro Quelldatei",
    "missing_values_heatmap": "Fehlende Werte pro Anlage und Monat",
    "missing_values_by_metric": "Fehlende Werte pro Messkanal",
    "pr_comparison": "Performance Ratio im Anlagenvergleich",
    "pr_trend": "Performance-Ratio-Trend",
    "seasonal_pr": "Saisonale Performance Ratio",
}

PLOT_SUFFIXES = {
    "active_power": "Wirkleistung",
    "irradiance": "Einstrahlung",
    "temperatures": "Temperaturen",
    "daily_energy": "Tagesenergie",
    "monthly_pr": "Monatliche Performance Ratio",
}


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return pd.DataFrame()
        return pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (float, np.floating)):
        return not np.isfinite(value)
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _format_number(value: float, digits: int = 2) -> str:
    text = f"{value:,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def _format_cell(column: str, value: Any) -> str:
    if _is_missing(value):
        return "–"
    if column in PERCENT_COLUMNS:
        return _format_number(float(value) * 100.0, 2) + " %"
    if isinstance(value, (bool, np.bool_)):
        return "Ja" if bool(value) else "Nein"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}".replace(",", ".")
    if isinstance(value, (float, np.floating)):
        rounded = column in {"mean_daily_pr", "overall_pr", "performance_ratio"}
        return _format_number(float(value), 3 if rounded else 2)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value)
    if text.endswith("+00:00"):
        text = text[:-6] + " UTC"
    return html.escape(text)


def _badge(value: Any) -> str:
    text = str(value)
    css = BADGE_CLASSES.get(text.lower(), "info")
    return f'<span class="badge {css}">{html.escape(text)}</span>'


def _table(
    frame: pd.DataFrame | None,
    columns: Sequence[str],
    labels: Mapping[str, str] | None = None,
    *,
    max_rows: int | None = None,
    empty: str = "Keine Daten verfügbar.",
    badge_columns: Iterable[str] = ("status", "quality_flag", "severity"),
) -> str:
    if frame is None or frame.empty:
        return f'<p class="empty">{html.escape(empty)}</p>'
    available = [column for column in columns if column in frame.columns]
    if not available:
        return f'<p class="empty">{html.escape(empty)}</p>'
    labels = labels or {}
    badge_columns = set(badge_columns)
    rows = frame.head(max_rows) if max_rows else frame

    head = "".join(
        f"<th>{html.escape(labels.get(column, column))}</th>" for column in available
    )
    body_rows: list[str] = []
    for _, row in rows.iterrows():
        cells = []
        for column in available:
            value = row[column]
            if column in badge_columns:
                cells.append(f"<td>{_badge(value)}</td>")
            else:
                cells.append(f"<td>{_format_cell(column, value)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _kpi(label: str, value: str, hint: str, tone: str = "") -> str:
    return (
        f'<article class="kpi {tone}">'
        f'<div class="kpi-label">{html.escape(label)}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'<div class="kpi-hint">{html.escape(hint)}</div>'
        "</article>"
    )


def _section(anchor: str, icon: str, title: str, subtitle: str, body: str) -> str:
    return (
        f'<section id="{anchor}" class="section">'
        '<div class="section-head">'
        f"<h2><span>{icon}</span> {html.escape(title)}</h2>"
        f'<p>{html.escape(subtitle)}</p>'
        "</div>"
        f"{body}</section>"
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _plot_title(path: Path) -> str:
    stem = path.stem
    if stem in PLOT_TITLES:
        return PLOT_TITLES[stem]
    for suffix, label in PLOT_SUFFIXES.items():
        ending = f"_{suffix}"
        if stem.endswith(ending):
            return f"{stem[: -len(ending)]} – {label}"
    return stem.replace("_", " ").capitalize()


def _plot_cards(settings: Settings, *, embed_images: bool = False) -> str:
    plots = sorted(settings.plots_dir.glob("*.png"))
    if not plots:
        return '<p class="empty">Noch keine Diagramme vorhanden.</p>'
    cards: list[str] = []
    for path in plots:
        relative = _relative(path, settings.output_dir)
        title = _plot_title(path)
        if embed_images:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            source = f"data:image/png;base64,{encoded}"
            image = (
                f'<img src="{source}" alt="{html.escape(title, quote=True)}" '
                'loading="lazy">'
            )
        else:
            source = quote(relative)
            image = (
                f'<a href="{source}" target="_blank" rel="noopener">'
                f'<img src="{source}" alt="{html.escape(title, quote=True)}" '
                'loading="lazy">'
                "</a>"
            )
        cards.append(
            '<figure class="plot-card" data-title="'
            + html.escape(title.lower(), quote=True)
            + '">'
            + image
            + f"<figcaption>{html.escape(title)}</figcaption>"
            "</figure>"
        )
    return (
        '<div class="toolbar">'
        '<input id="plot-search" type="search" '
        'placeholder="Diagramm filtern …" aria-label="Diagramme filtern">'
        "</div>"
        '<div class="plot-grid" id="plot-grid">' + "".join(cards) + "</div>"
    )


def _file_cards(settings: Settings, *, include_links: bool = True) -> str:
    if not include_links:
        return (
            '<p class="empty">Die CSV-Exporte sind in der Online-Demo nicht '
            "enthalten. Sie entstehen lokal im Ordner <code>output/csv/</code>.</p>"
        )
    files = sorted(settings.csv_dir.glob("*.csv"))
    if not files:
        return '<p class="empty">Noch keine CSV-Dateien vorhanden.</p>'
    rows: list[str] = []
    for path in files:
        relative = _relative(path, settings.output_dir)
        size_kb = max(1, round(path.stat().st_size / 1024))
        size_text = f"{size_kb:,}".replace(",", ".")
        rows.append(
            "<tr>"
            f'<td><a href="{quote(relative)}">{html.escape(path.name)}</a></td>'
            f'<td class="num">{size_text} KB</td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        "<th>Datei</th><th>Größe</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _installations_table(
    installations: pd.DataFrame, pr_summary: pd.DataFrame
) -> pd.DataFrame:
    if installations.empty:
        return pd.DataFrame()
    table = installations.copy()
    if not pr_summary.empty:
        subset = pr_summary[
            [
                column
                for column in (
                    "installation_id",
                    "mean_daily_pr",
                    "overall_pr",
                    "mean_coverage",
                    "status",
                )
                if column in pr_summary.columns
            ]
        ].drop_duplicates("installation_id", keep="first")
        table = table.merge(subset, on="installation_id", how="left")
    return table


def _format_periods(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    for column in columns:
        if column in result.columns:
            parsed = pd.to_datetime(result[column], errors="coerce", utc=True)
            result[column] = parsed.dt.strftime("%Y-%m").where(parsed.notna(), "")
    return result


def generate_dashboard(
    settings: Settings,
    *,
    embed_images: bool = False,
    include_csv_links: bool = True,
    path: Path | None = None,
) -> Path:
    """Build a static dashboard from the generated CSV and PNG files.

    With ``embed_images`` the plots are inlined as base64 data URIs and with
    ``include_csv_links=False`` the CSV export list is replaced by a note.  This
    produces a portable, single-file dashboard suitable for GitHub Pages.
    """

    settings.ensure_directories()
    installations = _read_csv(settings.csv_dir / "installations.csv")
    statistics = _read_csv(settings.csv_dir / "statistics.csv")
    pr_summary = _read_csv(settings.csv_dir / "pr_summary.csv")
    pr_monthly = _read_csv(settings.csv_dir / "pr_monthly.csv")
    trends = _read_csv(settings.csv_dir / "trends.csv")
    quality = _read_csv(settings.csv_dir / "quality_reports.csv")
    issues = _read_csv(settings.csv_dir / "quality_issues.csv")
    reduced = _read_csv(settings.csv_dir / "reduced_performance_periods.csv")

    installation_count = len(installations)
    if not installation_count and not pr_summary.empty:
        installation_count = int(pr_summary["installation_id"].nunique())

    measurement_count = (
        int(pd.to_numeric(statistics["count"], errors="coerce").fillna(0).sum())
        if not statistics.empty and "count" in statistics.columns
        else 0
    )
    imputed_count = (
        int(pd.to_numeric(statistics["imputed_count"], errors="coerce").fillna(0).sum())
        if not statistics.empty and "imputed_count" in statistics.columns
        else 0
    )
    mean_pr = (
        float(pd.to_numeric(pr_summary["overall_pr"], errors="coerce").mean())
        if not pr_summary.empty and "overall_pr" in pr_summary.columns
        else float("nan")
    )
    issue_count = (
        int(pd.to_numeric(issues["count"], errors="coerce").fillna(0).sum())
        if not issues.empty and "count" in issues.columns
        else 0
    )

    kpis = "".join(
        [
            _kpi(
                "Anlagen",
                f"{installation_count:,}".replace(",", "."),
                "analysierte Installationen",
            ),
            _kpi(
                "Messwerte",
                f"{measurement_count:,}".replace(",", "."),
                "verarbeitete Werte",
            ),
            _kpi(
                "Ø Performance Ratio",
                _format_cell("overall_pr", mean_pr),
                "über alle Anlagen",
            ),
            _kpi(
                "Imputiert",
                f"{imputed_count:,}".replace(",", "."),
                "gefüllte Messlücken",
                "info",
            ),
            _kpi(
                "Qualitätsprobleme",
                f"{issue_count:,}".replace(",", "."),
                "gemeldete Auffälligkeiten",
                "warn",
            ),
        ]
    )

    installations_table = _installations_table(installations, pr_summary)
    quality_files = (
        quality.drop_duplicates("source_file", keep="first")
        if not quality.empty and "source_file" in quality.columns
        else quality
    )
    monthly = pr_monthly.copy()
    if not monthly.empty:
        if "performance_ratio" in monthly.columns:
            monthly = monthly[monthly["performance_ratio"].notna()]
    monthly = _format_periods(monthly, ("period",))
    reduced = _format_periods(reduced, ("period",))
    trends_table = trends.copy()
    trends_table = _format_periods(trends_table, ("first_period", "last_period"))

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    provenance = html.escape(settings.data_provenance)
    synthetic = "synthetic" in settings.data_provenance.lower()
    notice = (
        f'<div class="notice"><strong>Synthetische Beispieldaten.</strong> '
        f"{html.escape(PROVENANCE_WARNING)}</div>"
        if synthetic
        else f'<div class="notice"><strong>Datenherkunft:</strong> {provenance}</div>'
    )
    footer_note = (
        "<strong>Synthetische Beispieldaten – keine realen Messungen.</strong>"
        if synthetic
        else html.escape(settings.data_provenance)
    )

    assumptions = [
        "Die Einstrahlung wird als Modul-/Plane-of-Array-Einstrahlung angenommen.",
        "Die Leistung wird als AC-Leistung am Wechselrichterausgang angenommen.",
        "Energie wird rechteckig mit dem konfigurierten Messintervall integriert.",
        "Imputation füllt nur kurze interne Lücken; es wird nie extrapoliert.",
        "Statistische Trends belegen keine physikalische Moduldegradation.",
    ]
    if synthetic:
        assumptions.insert(
            0, "Die Beispieldaten sind synthetisch und keine realen Messungen."
        )
    assumptions_body = (
        '<ul class="assumptions">'
        + "".join(f"<li>{html.escape(item)}</li>" for item in assumptions)
        + "</ul>"
    )

    anlagen_body = _table(
        installations_table,
        [
            "installation_id",
            "name",
            "location",
            "capacity_kw",
            "mean_daily_pr",
            "overall_pr",
            "mean_coverage",
            "status",
        ],
        {
            "installation_id": "Anlage",
            "name": "Name",
            "location": "Standort",
            "capacity_kw": "Leistung (kWp)",
            "mean_daily_pr": "Ø Tages-PR",
            "overall_pr": "Gesamt-PR",
            "mean_coverage": "Abdeckung",
            "status": "Status",
        },
        empty="Keine Anlagenmetadaten gefunden.",
    )

    quality_body = _table(
        quality_files,
        [
            "source_file",
            "total_records",
            "valid_measurements",
            "suspicious_measurements",
            "invalid_measurements",
            "missing_measurements",
            "duplicate_records",
            "rejected_records",
        ],
        {
            "source_file": "Quelldatei",
            "total_records": "Datensätze",
            "valid_measurements": "Gültig",
            "suspicious_measurements": "Auffällig",
            "invalid_measurements": "Ungültig",
            "missing_measurements": "Fehlend",
            "duplicate_records": "Duplikate",
            "rejected_records": "Abgelehnt",
        },
        max_rows=25,
        empty="Keine Qualitätsberichte vorhanden.",
    )

    issues_body = _table(
        issues,
        ["code", "severity", "count", "description"],
        {
            "code": "Code",
            "severity": "Schwere",
            "count": "Anzahl",
            "description": "Beschreibung",
        },
        max_rows=30,
        empty="Keine Qualitätsprobleme gemeldet.",
    )

    pr_body = _table(
        pr_summary,
        [
            "installation_id",
            "capacity_kw",
            "n_days",
            "n_months",
            "mean_daily_pr",
            "overall_pr",
            "total_energy_kwh",
            "total_irradiation_kwh_m2",
            "mean_coverage",
            "status",
        ],
        {
            "installation_id": "Anlage",
            "capacity_kw": "Leistung (kWp)",
            "n_days": "Tage",
            "n_months": "Monate",
            "mean_daily_pr": "Ø Tages-PR",
            "overall_pr": "Gesamt-PR",
            "total_energy_kwh": "Energie (kWh)",
            "total_irradiation_kwh_m2": "Einstrahlung (kWh/m²)",
            "mean_coverage": "Abdeckung",
            "status": "Status",
        },
        empty="Performance Ratio wurde nicht berechnet.",
    )

    monthly_body = _table(
        monthly,
        [
            "installation_id",
            "period",
            "energy_kwh",
            "irradiation_kwh_m2",
            "performance_ratio",
            "coverage",
            "quality_flag",
        ],
        {
            "installation_id": "Anlage",
            "period": "Monat",
            "energy_kwh": "Energie (kWh)",
            "irradiation_kwh_m2": "Einstrahlung (kWh/m²)",
            "performance_ratio": "PR",
            "coverage": "Abdeckung",
            "quality_flag": "Qualität",
        },
        max_rows=48,
        empty="Keine gültigen Monatswerte vorhanden.",
    )

    trends_body = _table(
        trends_table,
        [
            "installation_id",
            "n_months",
            "slope_per_year",
            "r_squared",
            "p_value",
            "status",
        ],
        {
            "installation_id": "Anlage",
            "n_months": "Monate",
            "slope_per_year": "Steigung / Jahr",
            "r_squared": "R²",
            "p_value": "p-Wert",
            "status": "Status",
        },
        empty="Zu wenige Monatswerte für einen Trend.",
    )

    reduced_body = _table(
        reduced,
        [
            "installation_id",
            "period",
            "performance_ratio",
            "installation_mean_pr",
            "threshold",
            "deviation_from_mean",
        ],
        {
            "installation_id": "Anlage",
            "period": "Monat",
            "performance_ratio": "PR",
            "installation_mean_pr": "Mittelwert",
            "threshold": "Schwelle",
            "deviation_from_mean": "Abweichung",
        },
        max_rows=30,
        empty="Keine Monate unterhalb der Schwelle.",
    )

    body = "".join(
        [
            notice,
            f'<div class="kpi-grid">{kpis}</div>',
            _section(
                "anlagen",
                "🏭",
                "Anlagen",
                "Installationen, Nennleistung und Performance Ratio.",
                anlagen_body,
            ),
            _section(
                "qualitaet",
                "🧪",
                "Datenqualität",
                "Qualitätskennzahlen pro Quelldatei und aggregierte Auffälligkeiten.",
                quality_body + issues_body,
            ),
            _section(
                "pr",
                "📐",
                "Performance Ratio",
                "PR nur bei ausreichender Abdeckung; Einstrahlung als Plane-of-Array angenommen.",
                pr_body,
            ),
            _section(
                "monatlich",
                "📅",
                "Monatliche Werte",
                "Energie, Einstrahlung und Performance Ratio pro Monat.",
                monthly_body,
            ),
            _section(
                "trends",
                "📈",
                "Trends und Auffälligkeiten",
                "Statistische Trends sind deskriptiv und kein Beweis für Moduldegradation.",
                trends_body + reduced_body,
            ),
            _section(
                "diagramme",
                "🖼",
                "Diagramme",
                "Alle erzeugten PNG-Diagramme; mit dem Suchfeld filterbar.",
                _plot_cards(settings, embed_images=embed_images),
            ),
            _section(
                "dateien",
                "📦",
                "CSV-Exporte",
                (
                    "Alle Tabellen zum Öffnen in Excel, LibreOffice oder pandas."
                    if include_csv_links
                    else "Die Online-Demo enthält nur die Diagramme und Tabellen "
                    "dieser Seite; die CSV-Exporte entstehen lokal."
                ),
                _file_cards(settings, include_links=include_csv_links),
            ),
            _section(
                "annahmen",
                "📌",
                "Annahmen und Grenzen",
                "Wichtige Hinweise zur Interpretation der Ergebnisse.",
                assumptions_body,
            ),
        ]
    )

    document = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Renewable Energy Dashboard</title>
<style>
:root {{
  --bg: #f4f6fb;
  --card: #ffffff;
  --ink: #0f172a;
  --muted: #64748b;
  --border: #e2e8f0;
  --accent: #2563eb;
  --accent-soft: #dbeafe;
  --ok: #15803d;
  --ok-soft: #dcfce7;
  --warn: #b45309;
  --warn-soft: #fef3c7;
  --bad: #b91c1c;
  --bad-soft: #fee2e2;
  --info: #1d4ed8;
  --info-soft: #e0e7ff;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  line-height: 1.55;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.hero {{
  background:
    radial-gradient(circle at 15% 20%, rgba(59,130,246,.35), transparent 38%),
    radial-gradient(circle at 85% 10%, rgba(16,185,129,.25), transparent 40%),
    linear-gradient(135deg, #0f172a 0%, #172554 55%, #1e3a8a 100%);
  color: #fff;
  padding: 58px 0 46px;
}}
.wrap {{ max-width: 1240px; margin: 0 auto; padding: 0 22px; }}
.eyebrow {{
  text-transform: uppercase;
  letter-spacing: .18em;
  font-size: .72rem;
  color: #93c5fd;
  font-weight: 700;
}}
.hero h1 {{
  margin: 10px 0 8px;
  font-size: clamp(1.9rem, 3.4vw, 3rem);
  line-height: 1.1;
  letter-spacing: -.02em;
}}
.hero p {{
  margin: 0;
  max-width: 780px;
  color: #dbeafe;
  font-size: 1.03rem;
}}
.hero-meta {{
  margin-top: 20px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  color: #bfdbfe;
  font-size: .85rem;
}}
.pill {{
  border: 1px solid rgba(255,255,255,.25);
  background: rgba(255,255,255,.08);
  padding: 4px 10px;
  border-radius: 999px;
}}
main {{ padding: 30px 0 60px; }}
.notice {{
  background: #fff7ed;
  border: 1px solid #fdba74;
  color: #7c2d12;
  border-radius: 14px;
  padding: 14px 18px;
  margin-bottom: 22px;
  font-size: .94rem;
}}
.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 14px;
  margin-bottom: 28px;
}}
.kpi {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 18px;
  box-shadow: 0 8px 24px rgba(15,23,42,.05);
}}
.kpi-label {{
  color: var(--muted);
  font-size: .78rem;
  text-transform: uppercase;
  letter-spacing: .08em;
  font-weight: 700;
}}
.kpi-value {{
  font-size: 1.9rem;
  font-weight: 750;
  margin: 6px 0 2px;
  letter-spacing: -.02em;
}}
.kpi-hint {{ color: var(--muted); font-size: .8rem; }}
.kpi.info {{ border-left: 4px solid #6366f1; }}
.kpi.warn {{ border-left: 4px solid #f59e0b; }}
.section {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 24px;
  margin-bottom: 22px;
  box-shadow: 0 10px 30px rgba(15,23,42,.045);
}}
.section-head h2 {{
  margin: 0 0 4px;
  font-size: 1.25rem;
  letter-spacing: -.01em;
}}
.section-head h2 span {{ margin-right: 6px; }}
.section-head p {{
  margin: 0 0 18px;
  color: var(--muted);
  font-size: .9rem;
}}
.table-wrap {{
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 12px;
  margin-bottom: 18px;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: .85rem;
  min-width: 620px;
}}
thead th {{
  position: sticky;
  top: 0;
  background: #f8fafc;
  color: #334155;
  text-align: left;
  font-size: .71rem;
  text-transform: uppercase;
  letter-spacing: .07em;
  padding: 11px 12px;
  border-bottom: 1px solid var(--border);
}}
tbody td {{
  padding: 10px 12px;
  border-bottom: 1px solid #f1f5f9;
  vertical-align: top;
}}
tbody tr:nth-child(even) {{ background: #fbfdff; }}
tbody tr:hover {{ background: #f1f5ff; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.badge {{
  display: inline-block;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: .75rem;
  font-weight: 700;
  white-space: nowrap;
}}
.badge.ok {{ background: var(--ok-soft); color: var(--ok); }}
.badge.warn {{ background: var(--warn-soft); color: var(--warn); }}
.badge.bad {{ background: var(--bad-soft); color: var(--bad); }}
.badge.info {{ background: var(--info-soft); color: var(--info); }}
.badge.muted {{ background: #f1f5f9; color: #475569; }}
.empty {{
  color: var(--muted);
  background: #f8fafc;
  border: 1px dashed var(--border);
  border-radius: 12px;
  padding: 18px;
  margin: 0 0 16px;
}}
.toolbar {{ margin-bottom: 14px; }}
.toolbar input {{
  width: min(360px, 100%);
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  font: inherit;
  background: #f8fafc;
}}
.toolbar input:focus {{ outline: 2px solid #bfdbfe; border-color: #93c5fd; }}
.plot-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}}
.plot-card {{
  margin: 0;
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  background: #fff;
}}
.plot-card img {{
  display: block;
  width: 100%;
  height: auto;
  background: #fff;
}}
.plot-card figcaption {{
  padding: 9px 12px;
  font-size: .8rem;
  color: #334155;
  border-top: 1px solid var(--border);
  text-transform: capitalize;
}}
.assumptions {{
  margin: 0;
  padding-left: 20px;
  color: #334155;
}}
.assumptions li {{ margin-bottom: 6px; }}
footer {{
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: .8rem;
  padding: 22px 0 36px;
  text-align: center;
}}
@media (max-width: 640px) {{
  .hero {{ padding: 40px 0 32px; }}
  .section {{ padding: 18px; }}
  table {{ min-width: 560px; }}
}}
@media print {{
  .hero {{ background: #0f172a !important; }}
  .toolbar {{ display: none; }}
  .section, .kpi {{ box-shadow: none; }}
  .plot-card {{ break-inside: avoid; }}
}}
</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <div class="eyebrow">ETL · Analytik · Reporting</div>
    <h1>Renewable Energy Dashboard</h1>
    <p>Photovoltaische Zeitreihen: Datenqualität, Energie, Performance Ratio und
    statistische Trends auf einen Blick.</p>
    <div class="hero-meta">
      <span class="pill">Erzeugt: {generated_at}</span>
      <span class="pill">{installation_count} Anlagen</span>
      <span class="pill">{html.escape(settings.input_timezone)} → UTC</span>
    </div>
  </div>
</header>

<main class="wrap">
{body}
</main>

<footer>
  <div class="wrap">
    <p>Erzeugt von der Renewable Energy Time-Series ETL &amp; Analytics Pipeline.</p>
    <p>{footer_note}</p>
  </div>
</footer>

<script>
(function () {{
  var search = document.getElementById("plot-search");
  if (!search) return;
  search.addEventListener("input", function () {{
    var query = search.value.toLowerCase();
    document.querySelectorAll(".plot-card").forEach(function (card) {{
      var title = card.getAttribute("data-title") || "";
      card.hidden = query !== "" && title.indexOf(query) === -1;
    }});
  }});
}})();
</script>
</body>
</html>
"""

    path = path or settings.output_dir / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path
