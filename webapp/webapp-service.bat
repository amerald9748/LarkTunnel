@echo off
rem Supervisor loop for the LarkTunnel webapp (Task Scheduler entry point).
rem Prod server on 127.0.0.1:8787; output -> logs\webapp-task.log
cd /d "%~dp0\.."
if not exist "logs" mkdir "logs"
:loop
echo [service] %date% %time% starting webapp >> "logs\webapp-task.log"
python webapp\server.py >> "logs\webapp-task.log" 2>&1
echo [service] %date% %time% webapp exited (code %errorlevel%) - restart in 10s >> "logs\webapp-task.log"
timeout /t 10 /nobreak >nul
goto loop
