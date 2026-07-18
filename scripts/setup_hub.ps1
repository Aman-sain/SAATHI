# SAATHI hub setup - idempotent (safe to re-run). MASTER_ARCHITECTURE.md sec. 20, Phase 0.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\setup_hub.ps1 [-MaxPhase 0]
#   -MaxPhase N  only download models needed up to phase N (0 = skip all models;
#                default 99 = everything with a pinned URL, ~1.9 GB for the LLM)
# NOTE: keep this file pure ASCII - PowerShell 5.1 misreads BOM-less UTF-8 as ANSI.
param(
    [int]$MaxPhase = 99
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
Write-Host "=== SAATHI hub setup ($RepoRoot) ===" -ForegroundColor Cyan

# --- 1. pick a Python: 3.11 preferred (hub NPU runtime pin, sec. 4); 3.12 fallback for
# mock-mode dev (deviation D-002). Anything older is refused - the codebase targets 3.11.
# Probes run through cmd /c: PS 5.1 + ErrorActionPreference=Stop turns a PS-side stderr
# redirect of a native exe into a terminating error.
$PyLauncher = $null
foreach ($v in @("3.11", "3.12")) {
    cmd /c "py -$v --version 2>nul" | Out-Null
    if ($LASTEXITCODE -eq 0) { $PyLauncher = $v; break }
}
if ($PyLauncher) {
    $PyExe = (& py "-$PyLauncher" -c "import sys;print(sys.executable)").Trim()
} else {
    cmd /c "python --version 2>nul" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FATAL: no Python found. Install 3.11 from python.org." -ForegroundColor Red; exit 1
    }
    $PyExe = (& python -c "import sys;print(sys.executable)").Trim()
    $okFloor = (& $PyExe -c "import sys;print(1 if sys.version_info>=(3,11) else 0)").Trim()
    if ($okFloor -ne "1") {
        Write-Host "FATAL: Python at $PyExe is older than 3.11. Install 3.11 (or 3.12) from python.org." -ForegroundColor Red; exit 1
    }
}
$ver = & $PyExe --version
Write-Host "Using $ver at $PyExe"
if ($ver -notmatch "3\.11\.") {
    Write-Host "WARN: not Python 3.11 - fine for mock-mode dev; the Snapdragon hub itself needs 3.11 (onnxruntime-qnn, sec. 4 / D-002)." -ForegroundColor Yellow
}

# --- 2. venv (create once; delete hub\venv to force a rebuild)
$VenvPy = Join-Path $RepoRoot "hub\venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating venv at hub\venv ..."
    & $PyExe -m venv (Join-Path $RepoRoot "hub\venv")
    if ($LASTEXITCODE -ne 0) { Write-Host "FATAL: venv creation failed." -ForegroundColor Red; exit 1 }
} else {
    Write-Host "venv exists - skipping creation"
}

# --- 3. dependencies (single manifest, sec. 4)
Write-Host "Installing dependencies from hub\requirements.txt ..."
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $RepoRoot "hub\requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "FATAL: pip install failed. If a wheel is missing on win-arm64, record it in docs\DEVIATIONS.md and apply the named fallback from MASTER_ARCHITECTURE.md sec. 4." -ForegroundColor Red
    exit 1
}
Write-Host "Dependencies OK"

# --- 4. runtime directories (gitignored; recreated here on every fresh clone)
foreach ($dir in @("data", "logs", "hub\models")) {
    $p = Join-Path $RepoRoot $dir
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}
Write-Host "Runtime dirs OK (data\, logs\, hub\models\)"

# --- 5. mosquitto check (needed from Phase 2; sec. 4 names the fallback broker)
$mosq = Get-Command mosquitto -ErrorAction SilentlyContinue
if ($null -eq $mosq) {
    Write-Host "WARN: mosquitto not on PATH. Needed from Phase 2 - install from mosquitto.org (x64 build runs under WoA emulation), or use the fallback broker script (sec. 4)." -ForegroundColor Yellow
} else {
    Write-Host "mosquitto found: $($mosq.Source)"
}

# --- 6. ntfy.exe (D-006, pinned URL+sha256): ONE-TIME fetch here, never at demo
# launch (start_all.ps1 only STARTS an already-verified local ntfy.exe - it must
# never need internet to launch the demo, offline-first). Idempotent: skipped if
# the sha256 already matches.
$NtfyExe = Join-Path $RepoRoot "hub\tools\ntfy\ntfy.exe"
$NtfySha256 = "A613EF841F248C8BA6195D811ED0EA3B9D114255775B020EB70D1F1C14536CEC"
$NtfyOk = (Test-Path $NtfyExe) -and ((Get-FileHash $NtfyExe -Algorithm SHA256).Hash -eq $NtfySha256)
if ($NtfyOk) {
    Write-Host "ntfy.exe present + verified: $NtfyExe"
} else {
    Write-Host "Fetching ntfy v2.26.0 (D-006 pin) ..."
    $NtfyUrl = "https://github.com/binwiederhier/ntfy/releases/download/v2.26.0/ntfy_2.26.0_windows_amd64.zip"
    $NtfyZip = Join-Path $RepoRoot "hub\tools\ntfy.zip.part"
    $NtfyDir = Join-Path $RepoRoot "hub\tools\ntfy"
    New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "hub\tools") | Out-Null
    Invoke-WebRequest -Uri $NtfyUrl -OutFile $NtfyZip
    $ZipHash = (Get-FileHash $NtfyZip -Algorithm SHA256).Hash
    if ($ZipHash -ne $NtfySha256) {
        Remove-Item $NtfyZip -Force
        Write-Host "FATAL: ntfy.zip CHECKSUM MISMATCH - expected $NtfySha256, got $ZipHash. Verify the D-006 pin (sec. 27.18) before retrying." -ForegroundColor Red
        exit 1
    }
    if (Test-Path $NtfyDir) { Remove-Item $NtfyDir -Recurse -Force }
    Expand-Archive -Path $NtfyZip -DestinationPath $NtfyDir -Force
    Remove-Item $NtfyZip -Force
    # the release zip nests the exe in a versioned subfolder - find and flatten it
    $Found = Get-ChildItem -Path $NtfyDir -Filter "ntfy.exe" -Recurse | Select-Object -First 1
    if (-not $Found) {
        Write-Host "FATAL: ntfy.exe not found after extraction under $NtfyDir." -ForegroundColor Red
        exit 1
    }
    Move-Item -Path $Found.FullName -Destination $NtfyExe -Force
    if ($Found.DirectoryName -ne $NtfyDir) { Remove-Item $Found.DirectoryName -Recurse -Force }
    Write-Host "ntfy.exe downloaded + verified: $NtfyExe"
}

# --- 7. models (sha256-verified; heavy - see -MaxPhase)
Write-Host "Checking models (max phase $MaxPhase) ..."
& $VenvPy (Join-Path $RepoRoot "scripts\download_models.py") --max-phase $MaxPhase
if ($LASTEXITCODE -ne 0) { Write-Host "FATAL: model download/verification failed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "=== Setup complete. Next steps ===" -ForegroundColor Green
Write-Host "  Copy-Item .env.example .env        (first time only; then edit values)"
Write-Host "  .\hub\venv\Scripts\Activate.ps1"
Write-Host "  python scripts\preflight.py        (must be green)"
