@echo off
REM ============================================================
REM  Windows one-click demo: fake relay + orchestrator (no API key).
REM  Double-click this file. This window keeps running the app:
REM  close it (or run stop.cmd) to stop everything.
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set RELAY_BASE_URL=http://127.0.0.1:8799/v1
set RELAY_API_KEY=sk-demo
set ARCHITECT_MODEL=gpt-5
set EDITOR_MODEL=deepseek-v4
REM 演示模式忽略界面上保存过的真实中转配置，保证离线可跑
set ORCHESTRATOR_IGNORE_SAVED_SETTINGS=1

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python 3.12+ first.
    pause
    exit /b 1
)

if not exist "data" mkdir "data"

echo [1/3] Starting offline fake relay on http://127.0.0.1:8799 ...
echo       (demo mode: saved settings in data\settings.json are ignored)
REM NOTE: no "set VAR=1 && cmd" here — the space before && becomes part of the value.
REM The parent environment (PYTHONUTF8 / PYTHONIOENCODING) is inherited as-is.
start "orchestrator-fake-relay" /b cmd /c "python -m scripts.fake_relay"
ping -n 4 127.0.0.1 >nul

echo [2/3] Opening browser in a few seconds ...
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul && start "" http://127.0.0.1:8787"

echo [3/3] Starting orchestrator on http://127.0.0.1:8787 ...
echo       Keep this window open. Press Ctrl+C or close it to stop.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

echo.
echo Orchestrator stopped.
pause
exit /b 0
