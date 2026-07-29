@echo off
REM LarkTunnel local query app — starts the read-only server and opens a browser.
setlocal

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (if exist "D:\software\python\python.exe" set "PY=D:\software\python\python.exe")
if not defined PY (
  echo [LarkTunnel] Python not found on PATH. Install Python or edit run.bat.
  pause
  exit /b 1
)

if not defined LARK_PORT set "LARK_PORT=8787"

echo [LarkTunnel] starting on http://127.0.0.1:%LARK_PORT%
start "" "http://127.0.0.1:%LARK_PORT%"
"%PY%" "%~dp0server.py"
