@echo off
REM ============================================================
REM  Daily auto-commit. Called by the Startup launcher and by the
REM  Git panel's "run now" button.
REM
REM  Usage: auto-commit.cmd          commit to local only
REM         auto-commit.cmd push     commit, then push
REM
REM  The log path follows the repo being operated on
REM  (ORCHESTRATOR_AUTOCOMMIT_REPO), NOT this script's location:
REM  otherwise running the script against a temporary repo (tests,
REM  verification) writes "committed" noise into the real project log.
REM
REM  NOTE: keep this file ASCII + CRLF. cmd.exe reads .cmd as ANSI, so
REM  non-ASCII comments turn into garbage commands on Chinese Windows.
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set DO_PUSH=%1
if "%DO_PUSH%"=="" set DO_PUSH=local

if defined ORCHESTRATOR_AUTOCOMMIT_REPO (
    set "LOG=%ORCHESTRATOR_AUTOCOMMIT_REPO%\.logs\auto-commit.log"
) else (
    set "LOG=%~dp0..\.logs\auto-commit.log"
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0auto-commit.ps1" -Push:"%DO_PUSH%" -LogFile "%LOG%"
exit /b %ERRORLEVEL%
