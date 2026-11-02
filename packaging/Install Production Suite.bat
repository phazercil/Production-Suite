@echo off
REM Opens the Production Suite setup window (same as Setup.exe).
REM For a text-only install, run:  powershell -ExecutionPolicy Bypass -File installer\install.ps1
cd /d "%~dp0"
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0installer\setup-gui.ps1"
