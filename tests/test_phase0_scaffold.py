"""Phase 0 guards: env template completeness, contract examples validity, model pins."""

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# §15 — the complete variable set; config.py (Phase 1) will be built against this
ENV_KEYS = [
    "HTTP_PORT", "MQTT_HOST", "MQTT_PORT", "HUB_LAN_IP",
    "DB_PATH", "MODELS_DIR", "LOG_DIR",
    "MOCK_NODE", "MOCK_VISION", "MOCK_ASR", "MOCK_LLM", "MOCK_CLOUD", "USE_NPU",
    "CAMERA_INDEX", "MIC_DEVICE",
    "GAS_WARN", "GAS_CRIT", "SOUND_SPIKE", "ESCALATE_SECONDS", "CAMERA_VERIFY_SECONDS",
    "LLM_BASE", "LLM_MODEL", "LLM_TIMEOUT_S",
    "CLOUD_API_BASE", "CLOUD_API_KEY", "CLOUD_MODEL", "CLOUD_TIMEOUT_S",
]


def test_env_example_has_every_section_15_key():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    defined = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    missing = [k for k in ENV_KEYS if k not in defined]
    assert not missing, f".env.example is missing §15 keys: {missing}"


def test_env_example_defaults_to_mock_cloud():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^MOCK_CLOUD=(\S+)", text, re.MULTILINE)
    assert match and match.group(1).startswith("1"), "MOCK_CLOUD must default ON until Phase 8 (§15)"


def test_contracts_json_examples_are_valid_json():
    text = (REPO_ROOT / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    assert len(blocks) >= 4, "expected telemetry, event, alert and error examples"
    for block in blocks:
        json.loads(block)  # raises on any drift from valid JSON


def _load_download_models():
    spec = importlib.util.spec_from_file_location(
        "download_models", REPO_ROOT / "scripts" / "download_models.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolves string annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_model_registry_is_sane():
    mod = _load_download_models()
    names = [m.name for m in mod.MODELS]
    filenames = [m.filename for m in mod.MODELS]
    assert len(set(names)) == len(names), "duplicate model names"
    assert len(set(filenames)) == len(filenames), "duplicate target filenames"
    for m in mod.MODELS:
        if m.url is not None:
            assert m.url.startswith("https://"), f"{m.name}: URL must be https"
        if m.sha256 is not None:
            assert re.fullmatch(r"[0-9a-f]{64}", m.sha256), f"{m.name}: malformed sha256 pin"
        assert m.phase >= 0
