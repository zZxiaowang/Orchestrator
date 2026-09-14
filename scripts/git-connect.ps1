# 把本项目连接到你的远端仓库（GitHub / Gitee / GitLab / 自建都行）。
#
#   .\scripts\git-connect.ps1 -RemoteUrl https://github.com/<你的账号>/orchestrator.git `
#       -UserName "你的名字" -UserEmail "you@example.com"
#
#   SSH 形式也行：-RemoteUrl git@github.com:<你的账号>/orchestrator.git
#
# 它会做三件事：
#   1) （可选）把你的名字/邮箱写进本仓库配置，并把首次提交的作者改成你（占位作者是 Codex <codex@localhost>）
#   2) 配置远端 origin（已存在则更新地址）
#   3) 推送 main 分支并建立跟踪

param(
    [Parameter(Mandatory = $true)][string]$RemoteUrl,
    [string]$UserName = "",
    [string]$UserEmail = "",
    [string]$Branch = "main",
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot | Split-Path -Parent
Set-Location $root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if (-not (Test-Path ".git")) {
    throw "当前目录还不是 git 仓库：$root"
}

if ($UserName) { git config user.name $UserName; Write-Host "已设置提交者姓名：$UserName" }
if ($UserEmail) { git config user.email $UserEmail; Write-Host "已设置提交者邮箱：$UserEmail" }

if ($UserName -or $UserEmail) {
    $dirty = git status --porcelain
    if ($dirty) {
        Write-Host "工作区有未提交改动，先提交再重写作者更安全；这里跳过作者重写。"
    } else {
        git commit --amend --reset-author --no-edit | Out-Null
        Write-Host "已把首次提交的作者改成：$(git log -1 --pretty='%an <%ae>')"
    }
}

# 注意：PowerShell 在 $ErrorActionPreference='Stop' 时会把 git 的 stderr 当成错误抛出，
# 所以这里用 `git remote` 列表判断，而不是 `git remote get-url`（远端不存在时会报错）。
$remotes = @(git remote)
if ($remotes -contains "origin") {
    $existing = git remote get-url origin
    git remote set-url origin $RemoteUrl
    Write-Host "已更新 origin：$existing -> $RemoteUrl"
} else {
    git remote add origin $RemoteUrl
    Write-Host "已添加 origin：$RemoteUrl"
}

if ($NoPush) {
    Write-Host "（-NoPush）已跳过推送。手动推送：git push -u origin $Branch"
    exit 0
}

Write-Host "开始推送（首次会弹出登录窗口，Git Credential Manager 会记住凭证）..."
git push -u origin $Branch

Write-Host ""
Write-Host "完成。远端：$(git remote get-url origin)"
Write-Host "提示：本仓库已通过 .gitignore 排除 data\（含 API Key）、dist\、build\、.logs\，"
Write-Host "      推送内容只有源码（70 个文件）。"
