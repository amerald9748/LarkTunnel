@echo off
rem Bitable deletion watcher — long-connection listener -> logs\audit.db
rem One-time setup first: see docs\60 Safety\Deletion Tracking.md (activation)
cd /d "%~dp0"
python watcher.py
pause
