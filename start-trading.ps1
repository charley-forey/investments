# Starts the trading daemon, fill stream, and dashboard as detached background
# processes with logs in data\. Safe to re-run: skips anything already running.

$root = "C:\Users\charl\Desktop\trading"
$exe = Join-Path $root ".venv\Scripts\trading.exe"
$py = Join-Path $root ".venv\Scripts\python.exe"

# A daemon can be alive and doing nothing: on 2026-07-26 the process sat at 4.7s
# CPU for 14 hours after its scheduler stopped executing jobs, and this script
# skipped it every 15 minutes because the process existed. Existence is not
# liveness -- the daemon writes a watchdog heartbeat every 30m, so a stale
# heartbeat on a process that has been up long enough to write one means wedged.
$StaleMinutes = 45   # one missed 30m watchdog tick plus slack

function Get-HeartbeatAgeMinutes {
    # -1 when there is no heartbeat or the journal cannot be read; callers treat
    # that as "no opinion" rather than "wedged", so we never restart on a bad read.
    try {
        $out = & $py -c @"
from datetime import datetime, timezone
from trading.config import get_config
from trading.data.journal import Journal
r = Journal(get_config().settings.paths.journal_db).last_heartbeat()
print(-1 if not r else (datetime.now(timezone.utc) - datetime.fromisoformat(r['ts'])).total_seconds() / 60)
"@ 2>$null
        if ($LASTEXITCODE -ne 0) { return -1 }
        return [double]$out
    } catch { return -1 }
}

function Stop-IfWedged($name) {
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like '*\.venv\Scripts\trading.exe*' -and
        ($_.CommandLine -match 'trading\.exe\W+(\w+)') -and $Matches[1] -eq $name
    }
    if (-not $procs) { return }

    # Do not judge a daemon that has not had time to write its first heartbeat,
    # or a fresh start would be killed on every run and never come up.
    $uptime = ((Get-Date) - ($procs | Sort-Object CreationDate | Select-Object -First 1).CreationDate).TotalMinutes
    if ($uptime -lt $StaleMinutes) { return }

    $age = Get-HeartbeatAgeMinutes
    if ($age -lt 0 -or $age -le $StaleMinutes) { return }

    Write-Host ("$name WEDGED: up {0:N0}m, last heartbeat {1:N0}m ago (> {2}m). Restarting." -f $uptime, $age, $StaleMinutes)
    $procs | Sort-Object ProcessId -Descending | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}

function Start-IfNotRunning($name, $procArgs, $outLog, $errLog) {
    # Match the subcommand exactly, the same way Stop-IfWedged does. A substring
    # match would see "marketstream" as "stream" and silently never restart the
    # fill websocket once both exist.
    $running = Get-CimInstance Win32_Process -Filter "Name = 'trading.exe'" |
        Where-Object {
            ($_.CommandLine -match 'trading\.exe\W+(\w+)') -and $Matches[1] -eq $name
        }
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

# Only the daemon writes heartbeats, so it is the only one we can judge this way.
Stop-IfWedged "daemon"
Start-IfNotRunning "daemon" "daemon" "data\daemon.log" "data\daemon.err.log"
Start-IfNotRunning "stream" "stream" "data\stream.log" "data\stream.err.log"
# Tick-level detection. If this dies the daemon's 15m cron still runs, so the
# failure mode is the latency we had before it existed, not a blind system.
Start-IfNotRunning "marketstream" "marketstream" "data\marketstream.log" "data\marketstream.err.log"
Start-IfNotRunning "dashboard" "dashboard" "data\dashboard.log" "data\dashboard.err.log"

Write-Host ""
Write-Host "Verify:  .venv\Scripts\trading.exe status"
Write-Host "Watch:   Get-Content data\daemon.log -Wait -Tail 20"
Write-Host "Web UI:  http://127.0.0.1:8787"
