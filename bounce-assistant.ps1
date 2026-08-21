# Clean bounce of the "Habit Assistant" scheduled task. MUST run elevated:
# Stop-ScheduledTask does not kill the python tree (it survives in the S4U
# session), and an unelevated Stop-Process on it is Access-denied — a leftover
# poller then fights the new one with Telegram 409s.
#
# Unelevated caller pattern:
#   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<this script>'
# then check bounce-result.txt next to this script.
$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$resultFile = Join-Path $RepoRoot "data\bounce-result.txt"
$log = @()
try { Stop-ScheduledTask -TaskName "Habit Assistant" -ErrorAction Stop; $log += "task stopped" } catch { $log += "stop task: $($_.Exception.Message)" }
Start-Sleep -Seconds 2
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @("python.exe","cmd.exe","powershell.exe") -and
    ($_.CommandLine -like "*habit_assistant*" -or $_.CommandLine -like "*start-assistant*")
}
foreach ($p in $procs) {
    try { Stop-Process -Id $p.ProcessId -Force -Confirm:$false -ErrorAction Stop; $log += "killed $($p.Name) $($p.ProcessId)" }
    catch { $log += "kill $($p.ProcessId): $($_.Exception.Message)" }
}
Start-Sleep -Seconds 2
# uv-venv python runs as shim + child interpreter; sweep by executable path too
$venvPy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -like "$RepoRoot*" }
foreach ($p in $venvPy) {
    try { Stop-Process -Id $p.ProcessId -Force -Confirm:$false -ErrorAction Stop; $log += "killed venv python $($p.ProcessId)" }
    catch { $log += "kill venv $($p.ProcessId): $($_.Exception.Message)" }
}
Start-Sleep -Seconds 2
try { Start-ScheduledTask -TaskName "Habit Assistant" -ErrorAction Stop; $log += "task started" } catch { $log += "start task: $($_.Exception.Message)" }
Set-Content -Path $resultFile -Value ($log -join "`r`n")
