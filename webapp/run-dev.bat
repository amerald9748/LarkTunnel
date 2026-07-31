@echo off
REM LarkTunnel — DEV MODE: 3.1 / 5.6 resolve to the dev test copies
REM (config.js devTables). Trips still write to the SHARED 5.x tables via
REM the dev-only link columns. Use for testing, never for real operations.
setlocal

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (if exist "D:\software\python\python.exe" set "PY=D:\software\python\python.exe")
if not defined PY (
  echo [LarkTunnel] Python not found on PATH. Install Python or edit run-dev.bat.
  pause
  exit /b 1
)

set "LARK_ENV=dev"
if not defined LARK_PORT set "LARK_PORT=8788"

echo [LarkTunnel] DEV MODE on http://127.0.0.1:%LARK_PORT%
start "" "http://127.0.0.1:%LARK_PORT%"
"%PY%" "%~dp0server.py"
