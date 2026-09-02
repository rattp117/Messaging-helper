# Full shutdown of the Telegram habit-assistant (elevated).
# Stops the task, kills the bot's python tree, disables the task so it
# stays off across reboots. Nightly backup task is left ENABLED on purpose
# (still protects the database). Reverse with: Enable-ScheduledTask + Start.
$result = @()
try {
    Stop-ScheduledTask -TaskName "Habit Assistant" -ErrorAction SilentlyContinue
    $result += "task stop: ok"

    $repo = "C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging AI assistant"
    $killed = 0
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
        $cmd = $_.CommandLine
        $exe = $_.ExecutablePath
        if (($cmd -and $cmd -like "*habit_assistant*") -or ($exe -and $exe.StartsWith($repo))) {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $killed++
        }
    }
    $result += "bot processes killed: $killed"

    Disable-ScheduledTask -TaskName "Habit Assistant" | Out-Null
    $result += "task disabled: $((Get-ScheduledTask -TaskName 'Habit Assistant').State)"
} catch {
    $result += "ERROR: $($_.Exception.Message)"
}
$result -join "`n" | Out-File -FilePath "$repo\data\shutdown-result.txt" -Encoding ascii
