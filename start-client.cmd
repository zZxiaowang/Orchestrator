@echo off
REM ============================================================
REM  启动桌面客户端（原生窗口，非浏览器页面）
REM
REM  * 用打包好的 dist\Orchestrator.exe
REM  * 数据目录与源码版共用（D:\orchestrator\data），
REM    这样配置、运行记录、插件在两种形态下是同一份
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "dist\Orchestrator.exe" (
    echo [ERROR] 找不到 dist\Orchestrator.exe，请先运行：
    echo         .\scripts\package.ps1
    pause
    exit /b 1
)

set ORCHESTRATOR_DATA_DIR=%~dp0data
start "" "%~dp0dist\Orchestrator.exe"
exit /b 0
