@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Renewable Energy ETL - Startmenue

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python wurde nicht gefunden.
    echo Bitte Python 3.11 oder neuer installieren und zum PATH hinzufuegen.
    echo.
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo   Renewable Energy Time-Series ETL ^& Analytics Pipeline
echo ============================================================
echo.
echo   [1] Historie anzeigen          (history)
echo   [2] Daten verarbeiten          (process, mit PostgreSQL)
echo   [3] Daten verarbeiten          (process --skip-db, ohne DB)
echo   [4] Berichte neu erzeugen      (report)
echo   [5] Dashboard im Browser oeffnen
echo   [6] Tests ausfuehren           (pytest)
echo   [7] output-Ordner oeffnen
echo   [0] Beenden
echo.
echo   Hinweis: Alle Beispieldaten sind synthetisch.
echo.
set "choice="
set /p "choice=Auswahl: "

if "%choice%"=="1" goto history
if "%choice%"=="2" goto process
if "%choice%"=="3" goto process_skip_db
if "%choice%"=="4" goto report
if "%choice%"=="5" goto dashboard
if "%choice%"=="6" goto tests
if "%choice%"=="7" goto open_output
if "%choice%"=="0" goto end
if "%choice%"=="" goto end

echo.
echo Ungueltige Auswahl: %choice%
timeout /t 2 >nul
goto menu

:history
cls
echo [history] Gespeicherte Anlagen, Importe und Qualitaetsberichte
echo.
python -m src.main history
goto finish

:process
cls
echo [process] CSV-Dateien einlesen, validieren, bereinigen und speichern
echo.
python -m src.main process
goto finish

:process_skip_db
cls
echo [process --skip-db] Verarbeitung ohne PostgreSQL
echo.
python -m src.main process --skip-db
goto finish

:report
cls
echo [report] Berichte aus gespeicherten Daten neu erzeugen
echo.
python -m src.main report
goto finish

:dashboard
cls
echo [dashboard] Statische HTML-Uebersicht
echo.
if not exist "output\dashboard.html" (
    echo dashboard.html fehlt, wird aus den vorhandenen Ausgaben erzeugt ...
    python -m src.main dashboard
    if errorlevel 1 goto finish
)
start "" "output\dashboard.html"
echo Dashboard wurde im Standardbrowser geoeffnet.
goto finish

:tests
cls
echo [tests] Testsuite ausfuehren
echo.
echo Hinweis: Der PostgreSQL-Integrationstest wird uebersprungen,
echo solange TEST_DATABASE_URL nicht gesetzt ist.
echo.
python -m pytest -q
goto finish

:open_output
cls
if not exist "output" mkdir "output"
start "" "output"
goto menu

:finish
set "rc=%ERRORLEVEL%"
echo.
echo Exit-Code: %rc%
echo.
pause
goto menu

:end
endlocal
exit /b 0
