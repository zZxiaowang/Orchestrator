# 每日开机自动提交的实际逻辑（由 auto-commit.cmd 调用）。
#
#   -Push local | push   是否在提交后推送（默认 local）
#   -LogFile <path>      日志文件
#
# 设计取舍：开机时只提交到本地，不主动推送——推送可能弹出凭证窗口，影响开机体验；
# 需要"提交并推送"就把计划任务改成 `auto-commit.cmd push`（面板上的开关支持这个选项）。

param(
    [string]$Push = "local",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Continue"
$repo = if ($env:ORCHESTRATOR_AUTOCOMMIT_REPO) {
    $env:ORCHESTRATOR_AUTOCOMMIT_REPO
} else {
    Split-Path -Parent $PSScriptRoot
}
Set-Location $repo
if (-not $LogFile) { $LogFile = Join-Path $repo ".logs\auto-commit.log" }
$logDir = Split-Path -Parent $LogFile
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

function Write-Log([string]$message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

if (-not (Test-Path (Join-Path $repo ".git"))) {
    Write-Log "跳过：$repo 不是 git 仓库"
    exit 1
}

$changes = git status --porcelain
if (-not $changes) {
    Write-Log "无改动，跳过提交"
    exit 0
}

$count = ($changes -split "`n" | Where-Object { $_.Trim() }).Count
git add -A | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "chore(auto): 每日自动提交 $stamp（$count 个文件）" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Log "提交失败（退出码 $LASTEXITCODE）"
    exit 1
}
Write-Log "已提交 $count 个文件"

if ($Push -eq "push") {
    git push | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Log "已推送到远端"
    } else {
        Write-Log "推送失败（退出码 $LASTEXITCODE）——提交已保存到本地"
    }
}
exit 0
