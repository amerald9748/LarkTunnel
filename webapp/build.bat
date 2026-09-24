@echo off
REM Build the LarkTunnel desktop app (PyInstaller, one-folder).
REM   Output: webapp\dist\LarkTunnel\LarkTunnel.exe  (zip that folder to distribute)
REM   Build venv: ..\.venv-build  (created on first run; pyinstaller + pywebview)
REM   Run ONLY dist\LarkTunnel\LarkTunnel.exe. PyInstaller scratch goes to ..\.tmp\pyi-build
REM   (an exe left under webapp\build\ has no _internal\ and fails with "Failed to load Python DLL").
REM   Keep this file CRLF + ASCII: LF-only cmd blocks mis-parse, and echo has no codepage.
setlocal
cd /d "%~dp0"

set "VENV=..\.venv-build"
if not exist "%VENV%\Scripts\python.exe" (
  echo [build] creating build venv %VENV%
  python -m venv "%VENV%" || exit /b 1
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip >nul
  "%VENV%\Scripts\python.exe" -m pip install pyinstaller pywebview openpyxl || exit /b 1
)
"%VENV%\Scripts\python.exe" -c "import openpyxl" 2>nul || "%VENV%\Scripts\python.exe" -m pip install openpyxl

tasklist /FI "IMAGENAME eq LarkTunnel.exe" 2>nul | find /I "LarkTunnel.exe" >nul
if not errorlevel 1 goto :running

echo [build] running unit tests first
"%VENV%\Scripts\python.exe" -m unittest discover tests -p "test_*.py" >nul 2>&1
if errorlevel 1 goto :tests_failed

findstr /C:"authTable: ''" ..\config\config.js >nul
if not errorlevel 1 echo [build] WARNING: config.js authTable is EMPTY - the exe will run in single-user mode, no team gating

echo [build] bundling App credentials into the build - .tmp\bundled.bin
"%VENV%\Scripts\python.exe" bundle_secrets.py
if errorlevel 1 goto :no_bundle

echo [build] pyinstaller LarkTunnel.spec
"%VENV%\Scripts\pyinstaller.exe" --noconfirm --clean --workpath "..\.tmp\pyi-build" --distpath "dist" LarkTunnel.spec
if errorlevel 1 goto :pyi_failed
if exist build rmdir /s /q build
del /q "..\.tmp\bundled.bin" >nul 2>&1

echo.
echo [build] done: %~dp0dist\LarkTunnel\LarkTunnel.exe   -- run THIS one, nothing under build\
echo [build] distribute: zip the whole dist\LarkTunnel folder. Teammates only enter
echo         their access key on first launch; App credentials are built in.
endlocal
exit /b 0

:running
echo [build] LarkTunnel.exe is running - close the app first, then re-run build.bat
exit /b 1

:tests_failed
echo [build] TESTS FAILED - run: python -m unittest discover webapp\tests -v
exit /b 1

:no_bundle
echo [build] no credentials to bundle - members would have to type them; aborting
del /q "..\.tmp\bundled.bin" >nul 2>&1
exit /b 1

:pyi_failed
echo [build] PyInstaller failed
del /q "..\.tmp\bundled.bin" >nul 2>&1
exit /b 1
