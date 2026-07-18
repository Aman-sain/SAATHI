# SAATHI start_llama (A6): start the pinned llama.cpp server for the EVENT PC.
#
# Exposes the OpenAI-compatible /v1 API on 127.0.0.1:8080. The hub's message
# engine (LLM_BASE in .env) talks to THIS — it replaces the dev-laptop ollama
# path. NO hub code changes: the hub already speaks OpenAI /v1 (Phase 4).
#
# Binary + weights are staged by scripts\download_models.py (llama-server.exe +
# llama-3.2-3b-q4.gguf). If either is missing, this is a no-op and the hub falls
# back to deterministic template text (offline-first: never fetches at launch).
#
# Ownership: scripts\** is M1's; verified on the event PC (Mac can't run .ps1).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

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

$llamaPort = '8080'
# Model id the server advertises on /v1/models must equal .env LLM_MODEL so the
# hub's OpenAI client matches — we pass it as --alias below.
$model = Get-EnvValue 'LLM_MODEL'; if (-not $model) { $model = 'llama-3.2-3b-instruct' }

# Locate the binary: download_models.py (A5) extracts the pinned release zip into
# hub\models\llama-arm64\ and hub\models\llama-x64\ (both staged pre-event, since
# the event PC's x64-vs-arm64 Python decision is made at hour 0 - STATUS). Prefer
# the variant matching THIS machine's actual architecture; fall back to whichever
# is present if only one was staged.
$arch = $env:PROCESSOR_ARCHITECTURE  # 'ARM64' or 'AMD64' on Windows
$preferred = if ($arch -eq 'ARM64') { 'llama-arm64' } else { 'llama-x64' }
$fallback = if ($preferred -eq 'llama-arm64') { 'llama-x64' } else { 'llama-arm64' }
$bin = @(
    (Join-Path $root "hub\models\$preferred\llama-server.exe"),
    (Join-Path $root "hub\models\$fallback\llama-server.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
$gguf = Join-Path $root 'hub\models\llama-3.2-3b-q4.gguf'

if (-not $bin) {
    Write-Warning 'llama-server.exe not staged (scripts\download_models.py --only llama-server) — hub stays on template text.'
    return
}
if (-not (Test-Path $gguf)) {
    Write-Warning "GGUF missing at $gguf (scripts\download_models.py --only llama) — hub stays on template text."
    return
}

# Guard: never start a second server.
if (Get-NetTCPConnection -LocalPort ([int]$llamaPort) -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "llama server      : already listening on 127.0.0.1:$llamaPort"
    return
}

# WorkingDirectory = the binary's folder so it finds its sibling DLLs (win-arm64
# release ships llama-server.exe alongside .dll files — stage the whole folder).
# PS 5.1 joins -ArgumentList array elements UNQUOTED, so a spaced path
# ("d:\Qualcomm Hackathon\…") splits and the server dies instantly — quote the
# path-like values inside the elements (Ansh's port-8099 repro, 2026-07-17;
# same class as the D-009 start_all quoting fix).
Start-Process -FilePath $bin -ArgumentList @(
    '-m', "`"$gguf`"",
    '--host', '127.0.0.1',
    '--port', $llamaPort,
    '--alias', "`"$model`"",
    '-c', '2048'
) -WorkingDirectory (Split-Path $bin) -WindowStyle Minimized

# Wait for the model to load (1.9 GB can take a while) and confirm the model id
# matches LLM_MODEL — up to ~2 min.
$modelsUrl = "http://127.0.0.1:$llamaPort/v1/models"
for ($i = 0; $i -lt 120; $i++) {
    try {
        $id = (Invoke-RestMethod -Uri $modelsUrl -TimeoutSec 2).data[0].id
        if ($id -eq $model) {
            Write-Host "llama server      : up on 127.0.0.1:$llamaPort (model id '$id' matches LLM_MODEL)"
        } else {
            Write-Warning "llama server up but /v1/models id '$id' != LLM_MODEL '$model' — align --alias or .env so the hub's model id matches."
        }
        return
    } catch { }
    Start-Sleep -Milliseconds 1000
}
Write-Warning "llama server did not answer $modelsUrl within ~2 min — check its window / logs."
