#!/usr/bin/env bash
# SAATHI - Start All Services (Mac / Linux)
# Owned by: Aman (Infrastructure & Networking)
# Usage: bash DevopsTasks/start_all.sh
#        (or: chmod +x DevopsTasks/start_all.sh && ./DevopsTasks/start_all.sh)
#
# What this script does:
#   1. Validates .env exists and reads config
#   2. Checks/starts Mosquitto broker
#   3. Starts the FastAPI Hub
#   4. Prints the caregiver phone URL and dashboard URL
#
# Mac/Linux-specific differences from start_all.ps1 (Windows):
#   - Paths use forward slashes  (/)
#   - venv Python at  hub/venv/bin/python   (not Scripts\python.exe)
#   - Process check via  pgrep               (not Get-Process)
#   - Mosquitto started in background via  mosquitto -d  or  & disown
#   - Colours via ANSI escape codes          (not -ForegroundColor)

set -euo pipefail

# ── colour helpers ───────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; GRAY='\033[0;37m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}$*${RESET}"; }
ok()    { echo -e "${GREEN}[OK] $*${RESET}"; }
warn()  { echo -e "${YELLOW}[WARN] $*${RESET}"; }
err()   { echo -e "${RED}[ERROR] $*${RESET}"; }
dim()   { echo -e "${GRAY}  $*${RESET}"; }

# ── repo root (one level up from DevopsTasks/) ───────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo ""
echo -e "${CYAN}${BOLD}============================================${RESET}"
echo -e "${CYAN}${BOLD}  SAATHI - Start All Services (Mac/Linux)  ${RESET}"
echo -e "${CYAN}${BOLD}============================================${RESET}"
echo ""

# ── 1. Check .env exists ─────────────────────────────────────────────────────
ENV_FILE="$REPO_ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    err ".env file not found."
    echo -e "${YELLOW}  Run: cp .env.example .env${RESET}"
    echo -e "${YELLOW}  Then edit .env and set HUB_LAN_IP to your machine's local IP.${RESET}"
    exit 1
fi

# ── 2. Read HUB_LAN_IP and HTTP_PORT from .env ───────────────────────────────
HUB_LAN_IP="127.0.0.1"   # default fallback (localhost for Mac dev)
HTTP_PORT="8000"          # default fallback

while IFS='=' read -r key val; do
    # strip leading/trailing whitespace
    key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    # skip blank lines and comment lines
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    # strip inline comments from value  (e.g. 8000   # hub port  →  8000)
    val="${val%%#*}"
    val="${val%"${val##*[![:space:]]}"}"
    [[ "$key" == "HUB_LAN_IP" ]] && HUB_LAN_IP="$val"
    [[ "$key" == "HTTP_PORT"  ]] && HTTP_PORT="$val"
done < "$ENV_FILE"

# ── 3. Detect venv Python ────────────────────────────────────────────────────
# Mac/Linux venv layout:  hub/venv/bin/python
# Windows  venv layout:   hub\venv\Scripts\python.exe  (used in start_all.ps1)
VENV_PY="$REPO_ROOT/hub/venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    err "Python venv not found at hub/venv"
    echo -e "${YELLOW}  Run setup first:${RESET}"
    echo -e "${YELLOW}    python3 -m venv hub/venv${RESET}"
    echo -e "${YELLOW}    hub/venv/bin/pip install -r hub/requirements.txt${RESET}"
    exit 1
fi
ok "Python venv found  →  $VENV_PY"

# ── 4. Check / Start Mosquitto ───────────────────────────────────────────────
echo ""
info "Checking Mosquitto MQTT broker..."

# Find mosquitto binary (Homebrew installs it in sbin which is often not in PATH)
MOSQ_CMD=""
if command -v mosquitto > /dev/null 2>&1; then
    MOSQ_CMD="mosquitto"
elif [[ -x "/opt/homebrew/sbin/mosquitto" ]]; then
    MOSQ_CMD="/opt/homebrew/sbin/mosquitto"
elif [[ -x "/usr/local/sbin/mosquitto" ]]; then
    MOSQ_CMD="/usr/local/sbin/mosquitto"
fi

if pgrep -x mosquitto > /dev/null 2>&1; then
    MOSQ_PID=$(pgrep -x mosquitto | head -1)
    ok "Mosquitto is already running (PID $MOSQ_PID)"
else
    if [[ -n "$MOSQ_CMD" ]]; then
        echo "  Starting Mosquitto in background..."
        if "$MOSQ_CMD" -d 2>/dev/null; then
            sleep 1
            MOSQ_PID=$(pgrep -x mosquitto | head -1)
            ok "Mosquitto started (PID $MOSQ_PID)"
        else
            "$MOSQ_CMD" > /tmp/mosquitto.log 2>&1 &
            disown
            sleep 1
            MOSQ_PID=$(pgrep -x mosquitto | head -1 || echo "unknown")
            ok "Mosquitto started in background (PID $MOSQ_PID)"
        fi
    else
        warn "Mosquitto not found on PATH."
        echo -e "${YELLOW}  Mac:   brew install mosquitto${RESET}"
        echo -e "${YELLOW}  Linux: sudo apt install mosquitto${RESET}"
        echo -e "${YELLOW}  The hub will still start; MQTT ingest will retry with backoff.${RESET}"
    fi
fi

# ── 5. Start the FastAPI Hub ─────────────────────────────────────────────────
echo ""
info "Starting SAATHI Hub (FastAPI + Uvicorn)..."
dim "Port : $HTTP_PORT"
dim "DB   : data/saathi.db"
dim "Logs : logs/hub.log"
echo ""

# Print URLs BEFORE starting (visible even when console fills with log output)
echo -e "${GREEN}${BOLD}============================================${RESET}"
echo -e "${GREEN}${BOLD}  CAREGIVER PHONE URL:${RESET}"
echo -e "${YELLOW}${BOLD}  http://${HUB_LAN_IP}:${HTTP_PORT}/${RESET}"
echo ""
echo -e "${GREEN}${BOLD}  JUDGE DASHBOARD:${RESET}"
echo -e "${YELLOW}${BOLD}  http://${HUB_LAN_IP}:${HTTP_PORT}/dash${RESET}"
echo -e "${GREEN}${BOLD}============================================${RESET}"
echo ""
echo -e "${CYAN}  Connect your phone to the same Wi-Fi / hotspot as this machine.${RESET}"
echo -e "${CYAN}  Then open the Caregiver URL in the phone browser.${RESET}"
echo ""
echo -e "${GRAY}  Press Ctrl+C to stop the hub.${RESET}"
echo ""

# Launch the hub via its canonical entry point — blocking, runs in this terminal.
# run_hub.py loads .env config, sets up logging, then uvicorn.run(create_app(settings))
# binding 0.0.0.0 on settings.http_port — the SAME entry the Windows start_all.ps1 uses,
# so both platforms launch the hub identically. (Do NOT call `uvicorn create_app --factory`:
# create_app(settings) requires an argument and the factory form passes none.)
exec "$VENV_PY" "$REPO_ROOT/hub/run_hub.py"
