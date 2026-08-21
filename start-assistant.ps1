# Launcher for Windows Task Scheduler (or manual double-click / `powershell -File`).
# Runs the habit assistant using the project's venv. Keep this script free of
# heavy dependencies -- it only resolves paths and execs python.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "venv not found at $Python. Run 'uv venv --python 3.12' and 'uv pip install -e .[dev]' in $RepoRoot first."
    exit 1
}

Set-Location $RepoRoot

# Single-instance guard: kill leftover bot pythons before launching. A task
# stop orphans the python tree (it outlives the task's powershell), and a
# leftover poller fights the new one with Telegram 409s. This runs in the same
# S4U session as those orphans, so it has the rights an unelevated shell lacks.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like "*habit_assistant*" -or $_.ExecutablePath -like "$RepoRoot*"
} | ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 1

# Append stdout/stderr to the log files ourselves — Task Scheduler discards
# console output, so without this a scheduled run is invisible. cmd handles
# the redirection to avoid PowerShell 5.1 stderr ErrorRecord wrapping.
$OutLog = Join-Path $RepoRoot "data\assistant.out.log"
$ErrLog = Join-Path $RepoRoot "data\assistant.err.log"
& cmd.exe /c "`"$Python`" -m habit_assistant.main >> `"$OutLog`" 2>> `"$ErrLog`""
exit $LASTEXITCODE
