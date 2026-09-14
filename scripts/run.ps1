# 启动编排器（Windows / PowerShell）。加 -Demo 会同时启动离线假中转。
#
#   .\scripts\run.ps1                 # 用真实中转（读取 orchestrator\.env 或界面设置）
#   .\scripts\run.ps1 -Demo           # 离线演示，无需任何 Key
#   .\scripts\run.ps1 -Demo -OpenBrowser
#
# 若提示"禁止运行脚本"，用：
#   powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Demo

param(
    [switch]$Demo,
    [switch]$OpenBrowser,
    [int]$Port = 8787,
    [int]$RelayPort = 8799
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot | Split-Path -Parent
Set-Location $root

# Windows 控制台默认是 GBK，中文日志会乱码
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { chcp 65001 > $null } catch { }

function Resolve-Python {
    foreach ($candidate in @("python", "py")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    throw "未找到 Python，请先安装 Python 3.12 并加入 PATH。"
}
$python = Resolve-Python

$env:PORT = $Port
if ($Demo) {
    $env:RELAY_BASE_URL = "http://127.0.0.1:$RelayPort/v1"
    $env:RELAY_API_KEY = "sk-demo"
    $env:ARCHITECT_MODEL = "gpt-5"
    $env:EDITOR_MODEL = "deepseek-v4"
    $env:ORCHESTRATOR_IGNORE_SAVED_SETTINGS = "1"  # 忽略界面上保存的真实配置
    Write-Host "启动离线假中转 http://127.0.0.1:$RelayPort ..."
    Start-Process -FilePath $python -ArgumentList "-m", "scripts.fake_relay" `
        -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

if ($OpenBrowser) {
    Start-Job { param($url) Start-Sleep -Seconds 4; Start-Process $url } `
        -ArgumentList "http://127.0.0.1:$Port" | Out-Null
}

Write-Host "启动编排器 http://127.0.0.1:$Port ..."
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
