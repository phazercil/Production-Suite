@echo off
REM Runs the Production Suite control service (Production Suite Admin, tray icon) and restarts it if it ever stops.
REM Started at boot by the "Production Suite Control" scheduled task. Logs are in the logs folder.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
if not exist "%~dp0logs" mkdir "%~dp0logs"
:loop
for %%F in ("%~dp0logs\control-console.log") do if %%~zF GTR 10485760 move /y "%%~fF" "%~dp0logs\control-console.old.log" >nul
"%~dp0.venv\Scripts\python.exe" "%~dp0serve.py" control 1>>"%~dp0logs\control-console.log" 2>&1
echo %date% %time% control service stopped (code %errorlevel%), restarting in 10 seconds>>"%~dp0logs\control-console.log"
ping -n 11 127.0.0.1 >nul
goto loop
