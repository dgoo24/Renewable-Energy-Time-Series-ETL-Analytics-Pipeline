<div align="center">

# ⚡ Renewable Energy Time-Series ETL & Analytics Pipeline

**ETL- und Analyse-Pipeline für photovoltaische Zeitreihen**

Von rohen CSV-Messdateien über validierte und bereinigte Daten in PostgreSQL
bis hin zu Kennzahlen, Performance Ratio, reproduzierbaren Berichten und einem
statischen Dashboard.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![pandas](https://img.shields.io/badge/pandas-2.x-150458?logo=pandas&logoColor=white)
![NumPy](https://img.shields.io/badge/NumPy-1.26-013243?logo=numpy&logoColor=white)
![pytest](https://img.shields.io/badge/Tests-70%20bestanden-brightgreen)
![Status](https://img.shields.io/badge/Status-abgeschlossen-success)

</div>

---

> [!WARNING]
> **Alle Beispieldaten in diesem Repository sind synthetisch.**
> Sie wurden ausschließlich zu Demonstrations- und Testzwecken erzeugt und sind
> **keine Messungen einer realen Photovoltaikanlage**. Jeder Bericht und jedes
> Diagramm enthält diesen Hinweis. Die Daten dürfen nicht als reale
> Forschungsmessungen präsentiert werden.

---

## 📋 Inhaltsverzeichnis

- [Überblick](#-überblick)
- [Architektur](#-architektur)
- [Eingabeformat](#-eingabeformat)
- [Datenqualität](#-datenqualität)
- [Bereinigung und Imputation](#-bereinigung-und-imputation)
- [Performance Ratio](#-performance-ratio)
- [Schnellstart](#-schnellstart)
- [CLI-Referenz](#-cli-referenz)
- [Dashboard und Windows-Launcher](#-dashboard-und-windows-launcher)
- [Ausgaben](#-ausgaben)
- [Konfiguration](#-konfiguration)
- [Datenbankschema](#-datenbankschema)
- [SQL-Abfragen](#-sql-abfragen)
- [Tests](#-tests)
- [Projektstruktur](#-projektstruktur)
- [Annahmen und Grenzen](#-annahmen-und-grenzen)

---

## 🎯 Überblick

Diese Anwendung verarbeitet PV-Messreihen in einem nachvollziehbaren,
dateibasierten ETL-Prozess:

| Schritt | Aufgabe |
|---|---|
| **1. Ingestion** | CSV-Dateien einlesen, gemischte Zeitstempel parsen, fehlerhafte Zeilen separieren |
| **2. Validierung** | Messwerte in `valid`, `suspicious`, `invalid` und `missing` einordnen |
| **3. Bereinigung** | Duplikate entfernen, auf ein 15-Minuten-UTC-Raster bringen, kurze Lücken füllen |
| **4. Persistenz** | Rohdaten unveränderlich speichern, verarbeitete Daten atomar neu aufbauen |
| **5. Analytik** | Statistik, Tages-/Monatsenergie, Performance Ratio, Trends, Saisonalität |
| **6. Reporting** | CSV-Tabellen, PNG-Diagramme und einen Markdown-Bericht erzeugen |
| **7. Dashboard** | Alles als statische HTML-Seite bündeln – ohne Server und ohne Zusatzabhängigkeit |

**Bewusste Designentscheidungen**

- ❌ Keine Kafka, Spark, Airflow, Kubernetes, FastAPI, React oder Cloud-Abhängigkeiten
- ✅ PostgreSQL 16 als einzige Infrastrukturkomponente
- ✅ Rohdaten werden **niemals** verändert oder stillschweigend gelöscht
- ✅ Verarbeitete Daten sind vollständig aus den Rohdaten rekonstruierbar
- ✅ Identische Dateien werden über ihren SHA-256-Hash erkannt und übersprungen

---

## 🏗 Architektur

```mermaid
flowchart LR
    subgraph Eingabe
        CSV[("CSV-Dateien<br/>data/raw")]
    end

    subgraph ETL
        ING["Ingestion<br/><small>Parsing · Long-Format</small>"]
        VAL["Validierung<br/><small>Qualitätsstatus</small>"]
        CLN["Bereinigung<br/><small>Duplikate · Raster · Imputation</small>"]
    end

    subgraph Speicher
        RAW[("measurements_raw<br/><small>append-only</small>")]
        PROC[("measurements_processed<br/><small>wiederaufbaubar</small>")]
    end

    subgraph Analyse
        ANA["Analytik<br/><small>Energie · PR · Trends</small>"]
        REP["Reporting<br/><small>CSV · PNG · Markdown</small>"]
        DASH["Dashboard<br/><small>statisches HTML</small>"]
    end

    CSV --> ING --> VAL --> CLN
    CLN --> RAW
    RAW --> PROC
    PROC --> ANA --> REP --> DASH
```

**Datenfluss in Worten:** Jede Datei wird zunächst vollständig geparst und validiert.
Erst danach werden die Messwerte in der Rohdatentabelle gespeichert. Die verarbeitete
Tabelle wird für jede betroffene Anlage aus **allen** gespeicherten Rohdaten neu
aufgebaut. Dadurch können neue Dateien weder frühere Daten verlieren noch doppelt
gezählt werden.

---

## 📥 Eingabeformat

Erwartete Spalten (Groß-/Kleinschreibung und Leerzeichen werden normalisiert):

| Spalte | Bedeutung | Einheit |
|---|---|---|
| `timestamp` | Messzeitpunkt | ISO 8601, `DD.MM.YYYY HH:MM[:SS]`, Epoch-Sekunden oder -Millisekunden |
| `installation_id` | Anlagenkennung | — |
| `sensor_id` | Sensor-/Logger-Kanal | — |
| `active_power_kw` | AC-Wirkleistung | kW |
| `irradiance_w_m2` | Einstrahlung (POA angenommen) | W/m² |
| `module_temperature_c` | Modultemperatur | °C |
| `ambient_temperature_c` | Umgebungstemperatur | °C |

- Zeitstempel **mit** Offset werden nach UTC konvertiert.
- Zeitstempel **ohne** Offset werden in `INPUT_TIMEZONE` interpretiert
  (Standard: `Europe/Berlin`).
- Leere Zahlenwerte gelten als `missing`; nicht parsebare Texte als `invalid`.
- Dezimalkomma und Tausendertrennzeichen werden akzeptiert.
- Zusätzliche Spalten werden ignoriert, aber als Ingestion-Hinweis gemeldet.
- **Rohdateien werden ausschließlich gelesen und nie verändert.**

---

## 🧪 Datenqualität

| Status | Bedeutung | Rohdaten | Verarbeiteter Wert |
|---|---|---|---|
| `valid` | innerhalb aller Grenzen | gespeichert | übernommen |
| `suspicious` | physikalisch möglich, aber unplausibel | gespeichert | übernommen, markiert |
| `invalid` | nicht parsebar oder physikalisch unmöglich | gespeichert | `NULL`, Originalwert bleibt erhalten |
| `missing` | leerer Quellwert oder fehlender Raster-Slot | ggf. gespeichert | `NULL`, außer imputiert |

> [!IMPORTANT]
> Ungültige Werte werden **niemals stillschweigend gelöscht**. Sie bleiben in
> `measurements_raw`, in den Qualitätsberichten und in den Rejected-Exporten
> nachvollziehbar. Statistische Ausreißer werden nicht automatisch als ungültig
> eingestuft – es gelten ausschließlich konfigurierte physikalische Grenzen.

---

## 🧹 Bereinigung und Imputation

| Regel | Verhalten |
|---|---|
| **Duplikate** | Gleiche Anlage, Sensor, Metrik und Zeitstempel; deterministische Sortierung, standardmäßig bleibt der erste Eintrag |
| **Raster** | Resampling auf `RESAMPLE_FREQUENCY` (Standard `15min`) |
| **Mehrere Samples pro Rasterzelle** | Mittelwertbildung mit Warnung |
| **Imputation** | Nur interne Lücken, deren Abstand zwischen letztem und nächstem Beobachtungswert ≤ `INTERPOLATION_MAX_GAP_MINUTES` (Standard 30 min) ist |
| **Keine Extrapolation** | Führende und nachlaufende Lücken werden nie gefüllt |
| **Ungültige Werte** | Werden **nicht** imputiert und bleiben als `invalid` erhalten |

Imputierte Werte sind explizit markiert (`is_imputed`, `quality_status = imputed`);
der ursprüngliche Quellwert bleibt in `original_value` verfügbar.

---

## 📐 Performance Ratio

```text
Endertrag        Yf = E_ac [kWh] / P_nom [kWp]          (Stunden)
Referenzertrag   Yr = H_poa [kWh/m²] / G_stc [kW/m²]    (Stunden)
Performance Ratio PR = Yf / Yr
```

mit `G_stc = 1 kW/m²`.

- PR wird nur berechnet, wenn die Nennleistung bekannt ist und die gepaarte
  Leistungs-/Einstrahlungsabdeckung mindestens `PR_MIN_COVERAGE` (Standard 80 %) beträgt.
- Die Einstrahlung wird als **Modul-/Plane-of-Array-Einstrahlung angenommen**.
- Zeiträume ohne Tageslicht, ohne Einstrahlung, mit fehlenden Eingaben oder
  unzureichender Abdeckung erhalten einen `quality_flag` und keinen PR-Wert.
- Die Energieintegration erfolgt rechteckig mit dem konfigurierten Messintervall.

> [!NOTE]
> Trends werden auf Monats-PR-Werten gefittet. **Eine statistische Steigung ist kein
> Beweis für physikalische Moduldegradation.** Saisonalität, Verschmutzung,
> Abschaltungen, Abregelung und fehlende Daten können ebenfalls Trends erzeugen.
> Der Bericht weist ausdrücklich darauf hin.

---

## 🚀 Schnellstart

### 1. Umgebung einrichten

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Konfiguration

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

### 3. PostgreSQL starten

```bash
docker compose up -d
```

### 4. Pipeline ausführen

```bash
python -m src.main init-db
python -m src.main process
python -m src.main history
python -m src.main report
```

### 5. Dashboard öffnen

```powershell
start output\dashboard.html
```

Das Dashboard wird bei jedem `process`- und `report`-Lauf automatisch neu erzeugt.
Unter Windows geht es auch ohne Terminal: `start.bat` doppelklicken und im Menü
**Dashboard im Browser öffnen** wählen.

### 6. Synthetische Beispieldaten erzeugen

```bash
python scripts/generate_synthetic_data.py
```

Das Skript erzeugt neun synthetische CSV-Dateien und kopiert sie nach `data/raw/`.

### Ohne Datenbank testen

```bash
python -m src.main process --skip-db
```

> [!TIP]
> `--skip-db` besitzt kein persistentes Datei-Hash-Register. Ein erneuter Lauf
> verarbeitet dieselben Dateien erneut und überschreibt die generierten Ausgaben.

### Bestehende PostgreSQL-Instanz verwenden

```bash
python -m src.main init-db \
  --database-url "postgresql+psycopg2://benutzer:passwort@host:5432/datenbank"
```

`init-db` ist idempotent: fehlende Tabellen werden angelegt, vorhandene Daten
werden **nie** gelöscht.

---

## 🖥 CLI-Referenz

```bash
python -m src.main init-db  [--database-url URL]

python -m src.main process  [--skip-db] \
                            [--input-dir DIR] \
                            [--output-dir DIR] \
                            [--database-url URL]

python -m src.main history  [--limit N] [--database-url URL]

python -m src.main report   [--output-dir DIR] \
                            [--installation CODE ...] \
                            [--database-url URL]

python -m src.main dashboard [--output-dir DIR]
```

| Befehl | Wirkung |
|---|---|
| `init-db` | Erstellt das Datenbankschema |
| `process` | Ingestion → Validierung → Bereinigung → Speicherung → Analyse → Bericht |
| `process --skip-db` | Gleicher Ablauf ohne PostgreSQL |
| `history` | Zeigt Anlagen, Ingestion-Läufe und Qualitätsberichte |
| `report` | Erzeugt Analyse und Berichte aus gespeicherten verarbeiteten Daten neu |
| `dashboard` | Erzeugt `output/dashboard.html` aus den vorhandenen CSV- und PNG-Ausgaben neu |

---

## 📊 Ausgaben

```text
output/
├── csv/
│   ├── statistics.csv
│   ├── daily_summary.csv
│   ├── monthly_summary.csv
│   ├── pr_daily.csv
│   ├── pr_monthly.csv
│   ├── pr_summary.csv
│   ├── pr_comparison.csv
│   ├── trends.csv
│   ├── reduced_performance_periods.csv
│   ├── seasonal_pr.csv
│   ├── installations.csv
│   ├── quality_reports.csv
│   ├── quality_issues.csv
│   ├── missing_intervals.csv
│   ├── file_processing_summary.csv
│   ├── processed_measurements.csv
│   ├── quality_<datei>.csv
│   └── rejected_<datei>.csv
├── plots/
│   ├── <anlage>_active_power.png
│   ├── <anlage>_irradiance.png
│   ├── <anlage>_temperatures.png
│   ├── <anlage>_daily_energy.png
│   ├── <anlage>_monthly_pr.png
│   ├── quality_overview.png
│   ├── missing_values_heatmap.png
│   ├── missing_values_by_metric.png
│   ├── pr_comparison.png
│   ├── pr_trend.png
│   └── seasonal_pr.png
├── reports/
│   └── summary_report.md
└── dashboard.html
```

Generierte Ausgaben sowie Roh- und Synthesedaten sind per `.gitignore`
ausgeschlossen. Es werden nur Dokumentation und `.gitkeep`-Dateien versioniert.

---

## 🖥 Dashboard und Windows-Launcher

Für alle, die nicht mit der Kommandozeile arbeiten möchten, gibt es zwei
zusätzliche Einstiegspunkte. Beide nutzen dieselbe Pipeline – es kommt keine
weitere Abhängigkeit hinzu.

### Statisches HTML-Dashboard

Nach jedem `process`- oder `report`-Lauf liegt `output/dashboard.html` bereit.
Die Datei wird direkt im Browser geöffnet; es läuft kein Webserver.

| Bereich | Inhalt |
|---|---|
| **Kennzahlen** | Anlagen, verarbeitete Messwerte, durchschnittliche Performance Ratio, imputierte Werte, Qualitätsprobleme |
| **Anlagen** | Nennleistung, Ø Tages-PR, Gesamt-PR, Abdeckung und Status |
| **Datenqualität** | Kennzahlen pro Quelldatei und aggregierte Qualitätsprobleme |
| **Performance Ratio** | Gesamtwerte inklusive Energie, Einstrahlung und Abdeckung |
| **Monatliche Werte** | Energie, Einstrahlung, PR und Qualitätsflag pro Monat |
| **Trends** | Statistische Steigung, R², p-Wert und Monate unterhalb der Schwelle |
| **Diagramme** | Alle PNG-Diagramme als Galerie, mit Suchfeld filterbar |
| **CSV-Exporte** | Direkte Links zu allen Tabellen für Excel, LibreOffice oder pandas |

Eigenschaften:

- **Kein Server, kein Build-Schritt, keine neuen Pakete.** Eine einzelne
  HTML-Datei mit eingebettetem CSS und minimalem JavaScript.
- **Relativ verlinkt.** Diagramme und CSV-Dateien werden aus `output/plots/`
  und `output/csv/` geladen. Die Ordnerstruktur muss daher erhalten bleiben.
- **Deutsche Oberfläche** passend zur Dokumentation.
- **Synthetik-Hinweis** prominent im Kopfbereich.
- **Druck- und Mobilansicht** über Media Queries.
- **Schreibgeschützt:** Das Dashboard visualisiert nur. Verarbeitung startet
  weiterhin über die CLI oder den Launcher.

```powershell
# Dashboard aus vorhandenen Ausgaben neu erzeugen
python -m src.main dashboard

# Im Standardbrowser öffnen
start output\dashboard.html
```

### Windows-Launcher `start.bat`

`start.bat` im Projektordner doppelklicken. Es öffnet ein Menü und führt die
Befehle im richtigen Verzeichnis aus; das Fenster bleibt nach jedem Lauf offen.

| Menüpunkt | Aktion |
|---|---|
| **1** | Historie anzeigen (`history`) |
| **2** | Daten verarbeiten (`process`, mit PostgreSQL) |
| **3** | Daten verarbeiten ohne Datenbank (`process --skip-db`) |
| **4** | Berichte neu erzeugen (`report`) |
| **5** | Dashboard im Browser öffnen (erzeugt es bei Bedarf) |
| **6** | Tests ausführen (`pytest`) |
| **7** | `output/`-Ordner im Explorer öffnen |
| **0** | Beenden |

Der Launcher prüft, ob `python` im `PATH` verfügbar ist, und zeigt bei Fehlern
eine verständliche Meldung statt eines sofort schließenden Fensters.

> [!NOTE]
> `start.bat` ist für Windows gedacht. Unter Linux und macOS genügen die
> CLI-Befehle; das Dashboard lässt sich anschließend mit
> `xdg-open output/dashboard.html` beziehungsweise
> `open output/dashboard.html` öffnen.

---

## ⚙️ Konfiguration

Alle Einstellungen werden aus Umgebungsvariablen und einer optionalen `.env`
gelesen. Die vollständige Liste steht in `.env.example`.

| Variable | Standard | Zweck |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy` | Hauptdatenbank |
| `TEST_DATABASE_URL` | `…/renewable_energy_test` | Nur für Integrationstests |
| `INPUT_DIR` / `OUTPUT_DIR` | `data/raw` / `output` | Ein- und Ausgabeverzeichnis |
| `INSTALLATIONS_FILE` | `config/installations.csv` | Optionale Anlagenmetadaten |
| `INPUT_TIMEZONE` | `Europe/Berlin` | Zeitzone für naive Zeitstempel |
| `EXPECTED_INTERVAL_MINUTES` | `15` | Erwartetes Messintervall |
| `RESAMPLE_FREQUENCY` | `15min` | Rasterfrequenz; leer deaktiviert Resampling |
| `MISSING_STRATEGY` | `interpolate` | `interpolate`, `ffill`, `none`, `drop` |
| `INTERPOLATION_MAX_GAP_MINUTES` | `30` | Maximale interne Lücke für Imputation |
| `DUPLICATE_KEEP` | `first` | `first` oder `last` |
| `PR_MIN_IRRADIANCE_W_M2` | `20` | Tageslichtschwelle für PR-Diagnostik |
| `PR_MIN_COVERAGE` | `0.8` | Mindestabdeckung für PR |
| `DATA_PROVENANCE` | Synthetik-Hinweis | Label in Berichten und Diagrammen |

> [!NOTE]
> Die Anlagenleistung ist optionale Metainformation. Fehlt sie, wird die
> Performance Ratio mit einer expliziten Warnung übersprungen; Ingestion,
> Bereinigung und alle übrigen Analysen laufen weiter.

---

## 🗄 Datenbankschema

| Tabelle | Zweck |
|---|---|
| `installations` | Anlagenkennung, Name, Standort, Nennleistung |
| `sensors` | Sensorkanal pro Anlage (Metrik + Einheit) |
| `ingestion_runs` | Ein Eintrag pro importierter Datei, eindeutiger SHA-256, Zeilenzahlen |
| `measurements_raw` | Append-only-Rohdaten inklusive ungültiger und doppelter Werte |
| `rejected_records` | Abgelehnte Quellzeilen mit Rohdaten als JSON |
| `quality_reports` | Qualitätskennzahlen pro Datei und JSON-Issue-Liste |
| `measurements_processed` | Dedupliziert, gerastert, ggf. imputiert, mit Statusflags |

- `measurements_raw` ist eindeutig über `(ingestion_run_id, source_row, metric)`.
- `measurements_processed` ist eindeutig über `(installation_id, sensor_id, timestamp)`.
- Beim Neuaufbau werden die verarbeiteten Zeilen einer Anlage atomar ersetzt.
- Alle Schreibvorgänge laufen in Transaktionen.

---

## 🧮 SQL-Abfragen

Dokumentierte, reine SQL-Abfragen liegen in `sql/queries/`:

| Datei | Inhalt |
|---|---|
| `measurements_by_installation.sql` | Messwerte einer Anlage |
| `measurements_by_time_range.sql` | Messwerte in einem Zeitfenster |
| `retrieve_processed_measurements.sql` | Verarbeitete Werte inkl. fehlender Zeilen |
| `daily_averages.sql` | Tagesmittel und Tagesenergie |
| `monthly_averages.sql` | Monatsmittel und Monatsenergie |
| `compare_installations.sql` | Anlagenvergleich und spezifischer Ertrag |
| `missing_measurement_intervals.sql` | Zusammenhängende Fehlintervalle |
| `raw_duplicates.sql` | Duplikate in der Rohdatentabelle |
| `quality_reports.sql` | Qualitätsberichte pro Datei |

```bash
psql "$DATABASE_URL" -v installation_code=INST_A \
  -f sql/queries/measurements_by_installation.sql
```

Die psql-Variablen und weitere Beispiele stehen in `sql/queries/README.md`.

---

## ✅ Tests

```bash
pytest
```

Die Suite deckt ab:

- Ingestion, Zeitstempel- und Zahlenparsing, Rejected Rows
- Validierungsstatus und Qualitätsberichte
- Deduplizierung, Rasterung und begrenzte Imputation
- Analytik und Performance Ratio
- CSV-, PNG- und Markdown-Reporting
- Statisches HTML-Dashboard inklusive Escaping, Zahlenformatierung und Linkprüfung
- Datenbankoperationen, Duplikatschutz und Rebuilds
- CLI-Verhalten

### PostgreSQL-Integrationstest

Der Integrationstest wird übersprungen, wenn `TEST_DATABASE_URL` nicht gesetzt ist.
Er führt die **echte CLI** gegen PostgreSQL aus – inklusive `init-db`, mehrfachen
`process`-Läufen, Duplikat-Erkennung, Rebuild nach neuen Daten, `history` und
`report`. Aus Sicherheitsgründen verweigert er die Ausführung, wenn der
Datenbankname nicht `test` enthält.

```powershell
# PowerShell
$env:TEST_DATABASE_URL="postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy_test"
pytest
```

```bash
# bash
TEST_DATABASE_URL="postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy_test" pytest
```

> [!CAUTION]
> Der Integrationstest löscht und erstellt die Tabellen in der Testdatenbank neu.
> Niemals auf eine Produktionsdatenbank zeigen lassen.

---

## 📁 Projektstruktur

```text
.
├── config/
│   └── installations.csv          # Synthetische Anlagenmetadaten
├── data/
│   └── raw/                       # Eingabe-CSVs (git-ignoriert)
├── output/                        # Generierte CSV/PNG/Markdown (git-ignoriert)
├── scripts/
│   └── generate_synthetic_data.py # Deterministischer Synthesedaten-Generator
├── sql/
│   └── queries/                   # Dokumentierte Analyseabfragen
├── src/
│   ├── config.py                  # Einstellungen und Metadaten
│   ├── ingestion.py               # CSV-Parsing und Long-Format
│   ├── validation.py              # Qualitätsstatus und Berichte
│   ├── cleaning.py                # Duplikate, Raster, Imputation
│   ├── models.py                  # SQLAlchemy-Schema
│   ├── database.py                # Transaktionen und Persistenz
│   ├── analytics.py               # Statistik, Energie, PR, Trends
│   ├── reports.py                 # CSV/PNG/Markdown-Ausgaben
│   ├── dashboard.py               # Statisches HTML-Dashboard
│   └── main.py                    # CLI
├── tests/                         # Pytest-Suite
├── docker-compose.yml             # PostgreSQL 16
├── start.bat                      # Windows-Launcher mit Menü
├── requirements.txt
├── .env.example
└── README.md
```

---

## ⚠️ Annahmen und Grenzen

- **Synthetische Daten:** Kein Ergebnis dieses Repositories beschreibt eine reale
  PV-Anlage.
- **Einstrahlung:** Es wird angenommen, dass sie in der Modulebene gemessen wird.
- **Leistung:** Es wird AC-Leistung am Wechselrichterausgang angenommen.
- **Energieintegration:** Rechteckig und konservativ bei fehlenden Samples.
- **Performance Ratio:** Teilweise abgedeckte Tage/Monate unterhalb der Schwelle
  werden ausgeschlossen.
- **Mehrere Sensoren pro Metrik:** Werte werden pro Zeitstempel gemittelt; dies ist
  eine Vereinfachung und wird als Warnung gemeldet.
- **Sommerzeit:** Mehrdeutige lokale Zeitstempel bei der Herbstumstellung werden als
  erstes Vorkommen interpretiert; nicht existierende Frühlingszeiten werden nach
  vorn verschoben.
- **Trends:** Statistische Trends sind deskriptiv und belegen keine
  Moduldegradation.
- **Dashboard:** Die HTML-Seite ist eine reine Visualisierung der zuletzt
  erzeugten CSV- und PNG-Dateien. Sie ersetzt weder die Pipeline noch die
  Datenbank und muss nach neuen Läufen neu erzeugt werden.
- **Betriebsmodell:** Die Pipeline ist bewusst single-process und dateiorientiert.
  Sie eignet sich für periodische CSV-Importe einer Forschungsgruppe, nicht für
  hochfrequentes Streaming.

---

<div align="center">

**Entwickelt für reproduzierbare, nachvollziehbare PV-Datenanalyse.**

*Synthetische Beispieldaten – keine realen Messungen.*

</div>
