# 打包成 Windows 可执行文件（PyInstaller）。
#
#   .\scripts\package.ps1              # 单文件桌面客户端 Orchestrator.exe（默认，无控制台窗口）
#   .\scripts\package.ps1 -OneDir      # 目录版 dist\Orchestrator\（启动更快）
#   .\scripts\package.ps1 -Console     # 保留控制台窗口（排查启动问题时用）
#   .\scripts\package.ps1 -SkipTests   # 跳过打包前的测试与静态检查
#
# 产物：
#   dist\Orchestrator.exe            （单文件版）
#   dist\Orchestrator\Orchestrator.exe（目录版）

param(
    [switch]$OneDir,
    [switch]$Console,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot | Split-Path -Parent
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 打包前清理残留进程：Windows 上正在运行的 exe 会被锁定，导致覆盖失败（WinError 5）
$stale = Get-Process -Name Orchestrator -ErrorAction SilentlyContinue
if ($stale) {
    Write-Host "发现 $($stale.Count) 个正在运行的 Orchestrator 进程，先结束它们（否则 exe 文件被占用无法覆盖）"
    $stale | Stop-Process -Force
    Start-Sleep -Seconds 2
}

if (-not $SkipTests) {
    Write-Host "[1/3] 跑测试与静态检查 ..."
    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "测试未通过，已中止打包。" }
    python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "ruff 未通过，已中止打包。" }
} else {
    Write-Host "[1/3] 跳过测试"
}

Write-Host "[2/3] 调用 PyInstaller（$(if ($OneDir) { '目录版' } else { '单文件版' })）..."
$env:ORCHESTRATOR_PACKAGE_ONEDIR = if ($OneDir) { "1" } else { "0" }
$env:ORCHESTRATOR_PACKAGE_CONSOLE = if ($Console) { "1" } else { "0" }
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\orchestrator.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }

$target = if ($OneDir) { "dist\Orchestrator\Orchestrator.exe" } else { "dist\Orchestrator.exe" }
if (-not (Test-Path $target)) { throw "未找到产物：$target" }
$size = [math]::Round((Get-Item $target).Length / 1MB, 1)

Write-Host "[3/3] 完成"
Write-Host ""
Write-Host "产物：$target  ($size MB)"
Write-Host "用法：双击运行 = 打开原生桌面窗口（WebView2，不是浏览器标签页）"
Write-Host "     服务器形态（浏览器访问）：.\$target --server"
Write-Host "     自检（开窗数秒后自动关闭）：.\$target --selftest 5"
Write-Host "  `$env:RELAY_BASE_URL='https://your-relay/v1'; `$env:RELAY_API_KEY='sk-...'; .\$target"
Write-Host ""
Write-Host "数据目录：EXE 同级的 data\（若该位置不可写则用 %LOCALAPPDATA%\Orchestrator\data），"
Write-Host "也可用环境变量 ORCHESTRATOR_DATA_DIR 指定。"
