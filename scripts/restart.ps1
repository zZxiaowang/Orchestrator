# Helper for "rebuild + restart" (started detached by Orchestrator).
#
# Why a separate helper: packaging kills Orchestrator.exe first (Windows locks the
# running file), and code after "killing yourself" never runs. So the sequence
# wait for exit -> repackage -> launch the new instance must live out of process.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restart.ps1 -Request <json>
#
# NOTE: keep this file ASCII-only. Windows PowerShell reads .ps1 as ANSI, so
# non-ASCII comments turn into garbage commands on Chinese Windows.

param(
    [Parameter(Mandatory = $true)][string]$Request
)

$ErrorActionPreference = "Continue"

function Write-Log([string]$Message, [string]$LogFile) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    if ($LogFile) { Add-Content -Path $LogFile -Value $line -Encoding utf8 }
    Write-Host $line
}

if (-not (Test-Path $Request)) {
    Write-Log "request file not found: $Request" ""
    exit 1
}

$spec = Get-Content -Raw -Path $Request | ConvertFrom-Json
$log = $spec.log_file
Write-Log "restart requested: pid=$($spec.pid) rebuild=$($spec.rebuild)" $log

# 1) Wait for the target process (Orchestrator itself) to exit. Up to 60 seconds.
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    $alive = Get-Process -Id $spec.pid -ErrorAction SilentlyContinue
    if (-not $alive) { break }
    Start-Sleep -Milliseconds 400
}
if (Get-Process -Id $spec.pid -ErrorAction SilentlyContinue) {
    Write-Log "target process still alive; stopping it" $log
    Stop-Process -Id $spec.pid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# 2) Optional: repackage (the packaging script also stops leftover processes).
if ($spec.rebuild) {
    if (Test-Path $spec.package_script) {
        Write-Log "running package script: $($spec.package_script)" $log
        & powershell -NoProfile -ExecutionPolicy Bypass -File $spec.package_script *>> $log
        Write-Log "package script exit code: $LASTEXITCODE" $log
    }
    else {
        Write-Log "package script missing: $($spec.package_script)" $log
    }
}

# 3) Launch the new instance (hidden window: no console flash).
try {
    foreach ($item in $spec.env.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($item.Name, [string]$item.Value, "Process")
    }
    $launchArgs = @()
    if ($spec.launch_args) { $launchArgs = @($spec.launch_args) }
    Start-Process -FilePath $spec.launch_file -ArgumentList $launchArgs `
        -WorkingDirectory $spec.cwd -WindowStyle Hidden
    Write-Log "launched: $($spec.launch_file) $($launchArgs -join ' ')" $log
}
catch {
    Write-Log "launch failed: $($_.Exception.Message)" $log
    exit 1
}

Remove-Item -LiteralPath $Request -Force -ErrorAction SilentlyContinue
Write-Log "restart finished" $log
exit 0
