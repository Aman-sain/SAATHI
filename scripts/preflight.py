"""SAATHI preflight v1 - the go/no-go health gate (MASTER_ARCHITECTURE.md §20 Phase 0).

Checks imports + ports + runtime dirs + env, and prints the live/mock state of every
subsystem (§12.6) so nobody is ever confused about what's real.

Exit 0 = GREEN (safe to proceed). Exit 1 = at least one FAIL.
WARNs never block: every optional subsystem has a named MOCK_ fallback (§16).

Later phases extend this file with device/model/broker probes; v1 is imports + ports.
"""

from __future__ import annotations

import importlib
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# import name -> pip name (§4). Required = the emergency loop needs them.
REQUIRED_IMPORTS = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "paho.mqtt.client": "paho-mqtt",
    "httpx": "httpx",
    "rich": "rich",
}
# Optional = a MOCK_ flag covers their absence (§12.6); demo narrative unchanged.
OPTIONAL_IMPORTS = {
    "onnxruntime": "AI inference off -> MOCK_VISION=1 MOCK_ASR=1",
    "cv2": "camera off -> MOCK_VISION=1",
    "sounddevice": "mic off -> MOCK_ASR=1",
}
PORTS = {8000: "hub HTTP/WS", 1883: "MQTT broker", 8080: "llama.cpp server (optional)"}
RUNTIME_DIRS = ["data", "logs", "hub/models"]
MOCK_KEYS = ["MOCK_NODE", "MOCK_VISION", "MOCK_ASR", "MOCK_LLM", "MOCK_CLOUD", "USE_NPU"]

failures: list[str] = []
warnings: list[str] = []


def report(tag: str, msg: str) -> None:
    print(f"  [{tag}] {msg}")
    if tag == "FAIL":
        failures.append(msg)
    elif tag == "WARN":
        warnings.append(msg)


def check_python() -> None:
    v = sys.version_info
    label = f"Python {v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 11):
        report("PASS", label)
        if (v.major, v.minor) != (3, 11):
            report("WARN", "hub machine itself must run 3.11 for onnxruntime-qnn (D-002)")
    else:
        report("FAIL", f"{label} - need >= 3.11")


def check_imports() -> None:
    for mod, pip_name in REQUIRED_IMPORTS.items():
        try:
            importlib.import_module(mod)
            report("PASS", f"import {mod}")
        except ImportError as e:
            report("FAIL", f"import {mod} ({pip_name}) -> pip install -r hub/requirements.txt: {e}")
    for mod, fallback in OPTIONAL_IMPORTS.items():
        try:
            importlib.import_module(mod)
            report("PASS", f"import {mod}")
        except ImportError:
            report("WARN", f"import {mod} missing -> {fallback}")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def port_answering(host: str, port: int, timeout: float = 0.5) -> bool:
    """Is something actually accepting connections here? (vs. port_free which only
    checks whether *we* could bind it.)"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def local_ipv4s() -> set[str]:
    """This machine's current IPv4 addresses (stdlib, cross-platform). Two probes:
    the hostname resolver, plus a UDP 'connect' that reveals the primary outbound
    interface IP without sending a packet (works offline while a route exists)."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return ips


def check_ports() -> None:
    # Port semantics are NOT uniform: for the broker, occupied is the healthy
    # state; for the hub, occupied may be a leftover process (the two-hubs bug).
    for port, what in PORTS.items():
        occupied = not port_free(port)
        if port == 1883:
            report("PASS" if occupied else "WARN",
                   f"port 1883 ({what}): " + ("IN USE (broker up)" if occupied
                   else "free — broker not running "
                        "(macOS: brew services start mosquitto | "
                        "Windows: winget install EclipseFoundation.Mosquitto)"))
        elif port == 8000:
            report("WARN" if occupied else "PASS",
                   f"port 8000 ({what}): " + ("IN USE — a hub may already be running "
                   "(two-hubs bug, STATUS); stop it before start_all" if occupied else "free"))
        else:  # 8080 llama.cpp — optional; either state is acceptable
            report("PASS",
                   f"port {port} ({what}): " + ("IN USE (llama server up)" if occupied
                   else "free (optional — off)"))


def check_dirs() -> None:
    for d in RUNTIME_DIRS:
        if (REPO_ROOT / d).is_dir():
            report("PASS", f"dir {d}/")
        else:
            report("WARN", f"dir {d}/ missing -> Windows: scripts\\setup_hub.ps1  |  macOS: mkdir -p {d}")


def read_env() -> tuple[str, dict[str, str]]:
    """Parse .env (or .env.example as documented defaults). Stdlib only: config.py
    owns real settings from Phase 1; preflight must not depend on app code."""
    for name in (".env", ".env.example"):
        path = REPO_ROOT / name
        if path.is_file():
            values: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                values[key.strip()] = raw.split("#", 1)[0].strip()
            return name, values
    return "", {}


def check_env() -> None:
    source, values = read_env()
    if not source:
        report("FAIL", "neither .env nor .env.example found")
        return
    tag = "PASS" if source == ".env" else "WARN"
    note = "" if source == ".env" else " (no .env yet: Copy-Item .env.example .env)"
    report(tag, f"env loaded from {source}{note}")
    print("\n  subsystem mode matrix (1 = mock):")
    for key in MOCK_KEYS:
        val = values.get(key, "?")
        mode = "MOCK" if val == "1" else ("live" if val == "0" else "??")
        print(f"    {key:12s}= {val}  ({mode})")


def check_lan_ip() -> None:
    """The phone reaches the hub at HUB_LAN_IP; on the phone hotspot (D-009) it
    changes every session. If .env's value isn't a current IP, phone buttons fail."""
    _, values = read_env()
    ip = values.get("HUB_LAN_IP", "").strip()
    if not ip:
        report("WARN", "HUB_LAN_IP not set — phone URLs/buttons will be wrong")
        return
    mine = local_ipv4s()
    if ip in mine:
        report("PASS", f"HUB_LAN_IP {ip} matches a current interface")
    else:
        shown = ", ".join(sorted(mine)) or "none found"
        report("WARN", f"HUB_LAN_IP {ip} is NOT a current IPv4 ({shown}) — stale; "
                       "phone buttons will fail. Update .env to this session's LAN IP.")


def check_ntfy() -> None:
    """Local ntfy push server (lock-screen alerts, zero internet). Optional at dev
    time, so a miss is a WARN, never a FAIL."""
    if port_answering("127.0.0.1", 2586):
        report("PASS", "ntfy push server answering on 127.0.0.1:2586")
    else:
        report("WARN", "ntfy not answering on 127.0.0.1:2586 "
                       "(optional in dev; started by scripts/start_all)")


def main() -> int:
    print(f"SAATHI preflight v1  (repo: {REPO_ROOT})")
    print("-" * 60)
    check_python()
    check_imports()
    check_ports()
    check_dirs()
    check_env()
    check_lan_ip()
    check_ntfy()
    print("-" * 60)
    if failures:
        print(f"PREFLIGHT RED: {len(failures)} failure(s), {len(warnings)} warning(s)")
        for f in failures:
            print(f"  !! {f}")
        return 1
    print(f"PREFLIGHT GREEN ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
