@echo off
REM Runs the Production Suite web app and restarts it if it ever stops.
REM Started at boot by the "Production Suite Web" scheduled task. Logs are in the logs folder.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
if not exist "%~dp0logs" mkdir "%~dp0logs"
:loop
for %%F in ("%~dp0logs\web-console.log") do if %%~zF GTR 10485760 move /y "%%~fF" "%~dp0logs\web-console.old.log" >nul
"%~dp0.venv\Scripts\python.exe" "%~dp0serve.py" web 1>>"%~dp0logs\web-console.log" 2>&1
echo %date% %time% web app stopped (code %errorlevel%), restarting in 10 seconds>>"%~dp0logs\web-console.log"
ping -n 11 127.0.0.1 >nul
goto loop
