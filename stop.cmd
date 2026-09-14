@echo off
REM Stops the processes started by start.cmd / start-demo.cmd / run.ps1
REM Only kills python processes whose command line belongs to THIS tool.
setlocal
powershell -NoProfile -Command ^
  "$targets = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'app\.main:app|scripts\.fake_relay' }; if ($targets) { $targets | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host ('Stopped {0} process(es).' -f $targets.Count) } else { Write-Host 'No orchestrator process found.' }"
exit /b 0
