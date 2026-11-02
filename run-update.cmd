@echo off
REM Installs an update or goes back to the previous version (Operator Kiosk > Admin > Updates).
REM Started by the "Production Suite Updater" scheduled task when the suite asks for it; does nothing otherwise.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%~dp0updates\run\updater.py" exit /b 0
"%~dp0.venv\Scripts\python.exe" "%~dp0updates\run\updater.py" --dir "%~dp0." request 1>>"%~dp0logs\update-console.log" 2>&1
