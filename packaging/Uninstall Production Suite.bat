@echo off
REM Double-click to remove Production Suite from this server. Records in MariaDB are not touched.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\uninstall.ps1"
