@echo off
REM ============================================================
REM  Windows one-click start with your own relay API key.
REM  Reads orchestrator\.env (created on first run).
REM  Keep this window open. Close it (or run stop.cmd) to stop.
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python 3.12+ first.
    pause
    exit /b 1
)

REM 已经用界面配置过（data\settings.json）就不用再填 .env 了
if not exist ".env" if not exist "data\settings.json" (
    copy /y ".env.example" ".env" >nul
    echo A new file .env was created. Fill in RELAY_BASE_URL and RELAY_API_KEY,
    echo then run start.cmd again.
    echo (Or set everything in the web UI at http://127.0.0.1:8787 - gear icon.)
    start "" notepad ".env"
    pause
    exit /b 0
)

if not exist "data" mkdir "data"

echo Opening browser in a few seconds ...
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul && start "" http://127.0.0.1:8787"

echo Starting orchestrator on http://127.0.0.1:8787 ...
echo Keep this window open. Press Ctrl+C or close it to stop.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

echo.
echo Orchestrator stopped.
pause
exit /b 0
