@echo off
REM Build the LarkTunnel desktop app (PyInstaller, one-folder).
REM   Output: webapp\dist\LarkTunnel\LarkTunnel.exe  (zip that folder to distribute)
REM   Build venv: ..\.venv-build  (created on first run; pyinstaller + pywebview)
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

echo [build] running unit tests first
"%VENV%\Scripts\python.exe" -m unittest discover tests -p "test_*.py" >nul 2>&1
if errorlevel 1 (
  echo [build] TESTS FAILED - run: python -m unittest discover webapp\tests -v
  exit /b 1
)

echo [build] pyinstaller LarkTunnel.spec
REM workpath goes to ..\.tmp so no half-assembled LarkTunnel.exe is left in webapp\build
REM (that copy has no _internal\ beside it and fails with "Failed to load Python DLL").
"%VENV%\Scripts\pyinstaller.exe" --noconfirm --clean --workpath "..\.tmp\pyi-build" --distpath "dist" LarkTunnel.spec || exit /b 1
if exist build rmdir /s /q build

echo.
echo [build] done: %~dp0dist\LarkTunnel\LarkTunnel.exe   ^(run THIS one - not anything under build\^)
echo [build] distribute: zip the whole dist\LarkTunnel folder; teammates enter
echo         App ID / Secret + their 授权码 in ⚙ 设置 on first launch.
endlocal
