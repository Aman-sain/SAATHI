# SAATHI demo_reset (A4): PROCESS-CONTROL reset between judge runs.
#   stop the hub  ->  clear demo rows in the DB  ->  restart  ->  confirm ALL OK.
#
# Safe by construction: the DB is only touched while the hub is STOPPED, so two
# processes never write data\saathi.db at once (the class of bug behind the
# two-hubs incident in STATUS). .env and every config file are left untouched.
# Idempotent: safe to run twice in a row.
#
# Ownership: scripts\** is M1's. This .ps1 is verified on Ansh's laptop (Mac can't
# run PowerShell). A *live* reset that does NOT restart the hub would need a new
# hub API endpoint — that is hub\app\** (outside M1's fence); flagged to Ansh as an
# option, deliberately NOT built here.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'hub\venv\Scripts\python.exe'
$db = Join-Path $root 'data\saathi.db'

# Read HTTP_PORT from .env (same parser as start_all.ps1) so we target the right hub.
function Get-EnvValue([string]$Key) {
    $envFile = Join-Path $root '.env'
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#') -or ($t -notmatch '=')) { continue }
        $i = $t.IndexOf('=')
        if ($t.Substring(0, $i).Trim() -eq $Key) { return (($t.Substring($i + 1) -split '#', 2)[0]).Trim() }
    }
    return $null
}
$hubPort = Get-EnvValue 'HTTP_PORT'; if (-not $hubPort) { $hubPort = '8000' }

# 1. Stop any hub listening on HTTP_PORT. (None listening = already stopped: fine.)
$conns = Get-NetTCPConnection -LocalPort ([int]$hubPort) -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($procId in ($conns.OwningProcess | Sort-Object -Unique)) {
        try {
            Write-Host "stopping hub (PID $procId) on port $hubPort ..."
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch { Write-Warning "could not stop PID ${procId}: $_" }
    }
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort ([int]$hubPort) -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
} else {
    Write-Host "no hub listening on port $hubPort (already stopped)"
}
# Hard stop: never touch the DB if something might still be writing it.
if (Get-NetTCPConnection -LocalPort ([int]$hubPort) -State Listen -ErrorAction SilentlyContinue) {
    throw "port $hubPort still has a listener after stop attempt — refusing to touch the DB while a hub may be writing it"
}

# 2. Clear the demo rows ONLY (alerts/events/telemetry/digests). The hub is
#    confirmed stopped, so this is the safe window. .env and config are untouched.
#    Robust to a missing table (fresh DB). Piped to the venv python via stdin so
#    there is no fragile inline-quoting; run_hub.py recreates the schema on start.
if (Test-Path $db) {
    if (-not (Test-Path $py)) { throw 'hub venv missing — run scripts\setup_hub.ps1 first' }
    $reset = @'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
deleted = 0
for t in ("alerts", "events", "telemetry", "digests"):
    try:
        deleted += c.execute("DELETE FROM " + t).rowcount
    except sqlite3.OperationalError:
        pass  # table not created yet — nothing to clear
c.commit()
c.close()
print("demo_reset: rows deleted =", deleted)
'@
    $reset | & $py - $db
    if ($LASTEXITCODE -ne 0) { throw 'DB clear failed — see error above' }
} else {
    Write-Host "no DB at $db yet — nothing to clear (the hub creates it on start)"
}

# 3. Restart the hub — the SAME entry start_all.ps1 uses.
Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $root 'hub\run_hub.py')`"" -WorkingDirectory $root -WindowStyle Minimized

# 4. Confirm ALL OK: poll /api/status until active_alert is null (level 0), ~15s.
$statusUrl = "http://127.0.0.1:$hubPort/api/status"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $s = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2
        if ($null -eq $s.active_alert -and [int]$s.active_alert_level -eq 0) { $ok = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if ($ok) {
    Write-Host ''
    Write-Host "RESET OK — hub back up on port $hubPort, no active alert (ALL OK)."
} else {
    throw "hub did not report ALL OK at $statusUrl within ~15s — check logs\hub.log"
}
