@echo off
REM ============================================================
REM  每日开机自动提交（由计划任务 Orchestrator-DailyAutoCommit 调用）
REM
REM  用法：auto-commit.cmd           只提交到本地
REM        auto-commit.cmd push      提交后同时推送
REM        auto-commit.cmd local     同上（显式本地）
REM
REM  没有改动时不做任何事；日志写到 .logs\auto-commit.log
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set DO_PUSH=%1
if "%DO_PUSH%"=="" set DO_PUSH=local

if not exist ".logs" mkdir ".logs"
set LOG=%~dp0..\.logs\auto-commit.log

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0auto-commit.ps1" -Push:"%DO_PUSH%" -LogFile "%LOG%"
exit /b %ERRORLEVEL%
