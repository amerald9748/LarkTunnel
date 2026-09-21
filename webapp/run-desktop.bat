@echo off
REM LarkTunnel desktop shell from a source checkout (no build needed):
REM attaches to a running server on the configured port, else starts one
REM in-process, then opens a native window (pywebview / Edge --app / browser).
REM Uses the build venv when present (has pywebview); falls back to python.
setlocal
cd /d "%~dp0"
set "PY=..\.venv-build\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" desktop.py --console %*
endlocal
