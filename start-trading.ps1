# Starts the trading daemon, fill stream, and dashboard as detached background
# processes with logs in data\. Safe to re-run: skips anything already running.

$root = "C:\Users\charl\Desktop\trading"
$exe = Join-Path $root ".venv\Scripts\trading.exe"

function Start-IfNotRunning($name, $procArgs, $outLog, $errLog) {
    $running = Get-CimInstance Win32_Process -Filter "Name = 'trading.exe'" |
        Where-Object { $_.CommandLine -match $name }
    if ($running) {
        Write-Host "$name already running (pid $($running.ProcessId))"
        return
    }
    Start-Process -FilePath $exe -ArgumentList $procArgs -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root $outLog) `
        -RedirectStandardError (Join-Path $root $errLog)
    Write-Host "$name started (logs: $outLog, $errLog)"
}

Start-IfNotRunning "daemon" "daemon" "data\daemon.log" "data\daemon.err.log"
Start-IfNotRunning "stream" "stream" "data\stream.log" "data\stream.err.log"
Start-IfNotRunning "dashboard" "dashboard" "data\dashboard.log" "data\dashboard.err.log"

Write-Host ""
Write-Host "Verify:  .venv\Scripts\trading.exe status"
Write-Host "Watch:   Get-Content data\daemon.log -Wait -Tail 20"
Write-Host "Web UI:  http://127.0.0.1:8787"
