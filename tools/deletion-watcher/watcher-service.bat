@echo off
rem Supervisor loop for the deletion watcher (Task Scheduler entry point).
rem Restarts the watcher if it ever exits/crashes; output -> logs\watcher-task.log
cd /d "%~dp0"
if not exist "..\..\logs" mkdir "..\..\logs"
:loop
echo [service] %date% %time% starting watcher >> "..\..\logs\watcher-task.log"
python watcher.py >> "..\..\logs\watcher-task.log" 2>&1
echo [service] %date% %time% watcher exited (code %errorlevel%) - restart in 15s >> "..\..\logs\watcher-task.log"
timeout /t 15 /nobreak >nul
goto loop
