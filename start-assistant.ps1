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
& $Python -m habit_assistant.main
exit $LASTEXITCODE
