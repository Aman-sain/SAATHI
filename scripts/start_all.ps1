# SAATHI start_all (§5/§22): broker check → ntfy push server (Option D, D-006)
# → local LLM (pinned llama.cpp on the event PC, else ollama on a dev laptop)
# → hub. Prints the phone URLs from .env.
#
# Ownership note: scripts/** is M1's area — drafted by M3 under D-006
# (lead-approved 2026-07-12). Event-PC llama.cpp path wired in by Aman (A6).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'hub\venv\Scripts\python.exe'

# --- helpers ---------------------------------------------------------------
# Read a single key from .env. The banner MUST reflect .env, never an auto-picked
# adapter: on the phone-hosted hotspot (D-009) HUB_LAN_IP changes every session,
# and auto-detect kept printing the wrong NIC (e.g. a VirtualBox adapter).
function Get-EnvValue([string]$Key) {
    $envFile = Join-Path $root '.env'
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#') -or ($t -notmatch '=')) { continue }
        $i = $t.IndexOf('=')
        $k = $t.Substring(0, $i).Trim()
        if ($k -eq $Key) { return (($t.Substring($i + 1) -split '#', 2)[0]).Trim() }
    }
    return $null
}

# Is anything already listening on this local TCP port? (dup-hub guard)
function Test-PortListening([int]$Port) {
    return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

$hubIp = Get-EnvValue 'HUB_LAN_IP'
$hubPort = Get-EnvValue 'HTTP_PORT'
if (-not $hubPort) { $hubPort = '8000' }
if (-not $hubIp) {
    Write-Warning ".env HUB_LAN_IP is not set - phone URLs/buttons will be wrong. Set it to this session's LAN IP."
    $hubIp = '127.0.0.1'
}

# 1. MQTT broker — installed as an auto-start Windows service 2026-07-09
$svc = Get-Service mosquitto -ErrorAction SilentlyContinue
if ($null -eq $svc) {
    Write-Warning 'mosquitto service not found — winget install EclipseFoundation.Mosquitto'
} elseif ($svc.Status -ne 'Running') {
    try { Start-Service mosquitto } catch { Write-Warning "mosquitto stopped and needs admin to start: $_" }
}

# 2. ntfy push server (Option D): real lock-screen phone alerts, zero internet.
#    Phone needs inbound TCP 2586 allowed once (elevated):
#    New-NetFirewallRule -DisplayName "SAATHI ntfy (TCP 2586)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 2586 -Profile Any
$ntfy = Join-Path $root 'hub\tools\ntfy\ntfy.exe'
if (Test-Path $ntfy) {
    if (-not (Get-Process ntfy -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $ntfy -ArgumentList 'serve', '--listen-http', ':2586', '--cache-file', "$env:TEMP\ntfy-cache.db" -WindowStyle Minimized
    }
    Write-Host 'ntfy push server  : http://0.0.0.0:2586'
} else {
    Write-Warning 'hub\tools\ntfy\ntfy.exe missing — phone push disabled (pinned URL+sha256 in docs/DEVIATIONS.md D-006)'
}

# 3. local LLM — event PC = pinned llama.cpp server (A6); dev laptop = ollama;
#    neither present = deterministic template text (by design, §6.11).
$llamaBin = @(
    (Join-Path $root 'hub\models\llama-arm64\llama-server.exe'),
    (Join-Path $root 'hub\models\llama-x64\llama-server.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($llamaBin) {
    & (Join-Path $PSScriptRoot 'start_llama.ps1')
} elseif (Get-Command ollama -ErrorAction SilentlyContinue) {
    if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
        Start-Process ollama -ArgumentList 'serve' -WindowStyle Minimized
    }
    Write-Host 'ollama            : http://127.0.0.1:11434 (set LLM_BASE/LLM_MODEL in .env)'
} else {
    Write-Host 'no local LLM found — alerts use template text (by design, §6.11)'
}

# 4. hub — refuse a second instance (the recurring two-hubs bug, STATUS), then
#    wait for it to actually answer before the banner claims it is ready.
if (-not (Test-Path $py)) { throw 'hub venv missing — run scripts\setup_hub.ps1 first' }
if (Test-PortListening ([int]$hubPort)) {
    Write-Warning "port $hubPort already has a listener — NOT starting a second hub (two-hubs bug). Reusing the running one."
} else {
    # Path must be quoted: Start-Process joins ArgumentList unquoted, and the
    # repo path contains a space ("Qualcomm Hackathon") — unquoted, python gets
    # two broken args and dies instantly (verified 2026-07-12).
    Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $root 'hub\run_hub.py')`"" -WorkingDirectory $root -WindowStyle Minimized
}

# 4b. health gate — poll /api/health so success is real, not just "Start-Process
#     didn't throw". Up to ~15 s (30 x 500 ms).
$healthUrl = "http://127.0.0.1:$hubPort/api/health"
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        if ((Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) {
            $ready = $true; break
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if ($ready) {
    Write-Host "hub              : healthy on port $hubPort"
} else {
    Write-Warning "hub did not answer $healthUrl within ~15s — check logs\hub.log (URLs below may not work yet)"
}

# 5. banner — URLs come straight from .env (HUB_LAN_IP + HTTP_PORT), NOT an
#    auto-picked adapter. The phone reaches the hub at exactly this address.
Write-Host ''
Write-Host "  caregiver PWA     : http://${hubIp}:${hubPort}/app"
Write-Host "  judge dashboard   : http://${hubIp}:${hubPort}/dash"
Write-Host "  ntfy subscription : http://${hubIp}:2586/<REMOTE_NOTIFY_TOPIC from .env>"
Write-Host ''
Start-Process "http://127.0.0.1:${hubPort}/dash"
