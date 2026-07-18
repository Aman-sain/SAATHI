"""Typed settings from `.env` (§15). Placement rule (§5): the ONLY place .env is read.

Every field has the §15 default, so `Settings()` works with no .env at all (§6.1).
Relative paths anchor to the repo root, not the CWD, so the hub behaves the same
whether launched from repo root, hub/, or a test.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
_PLACEHOLDER = "PLACEHOLDER"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # network
    http_port: int = 8000
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    hub_lan_ip: str = "192.168.137.1"
    # paths
    db_path: Path = Path("data/saathi.db")
    models_dir: Path = Path("hub/models")
    log_dir: Path = Path("logs")
    # pipeline toggles (1 = mock); MOCK_CLOUD defaults ON until Phase 8 (§15)
    mock_node: bool = False
    mock_vision: bool = False
    mock_asr: bool = False
    mock_llm: bool = False
    mock_cloud: bool = True
    use_npu: bool = False
    # devices
    camera_index: int = 0
    mic_device: str = "default"
    # thresholds (calibrated on-site in Phase 7)
    gas_warn: float = 0.35
    gas_crit: float = 0.60
    sound_spike: float = 0.55
    escalate_seconds: float = 30
    camera_verify_seconds: float = 60
    # D-007/D-008: voice re-announces every N s while an alert stays active (a
    # single 9 s phrase is missable by a sleeping elder); 0 = announce once only
    announce_repeat_seconds: float = 15
    # ASR (Phase 5, §6.9/§11.2): keyword lists are config, not literals —
    # comma-separated, matched as substrings of the lowercased transcript.
    # ASR_MODEL_FILE (env key kept for compat) names the optimum whisper export
    # DIRECTORY: encoder_model.onnx + decoder_model.onnx (pinned as
    # whisper-encoder/whisper-decoder in download_models.py) + tokenizer JSONs.
    help_keywords: str = "help,bachao,bachao mujhe,madad"
    ok_keywords: str = "i'm fine,im ok,theek hoon,thik hu"
    asr_model_file: str = "whisper-base-en-onnx"
    # local LLM
    llm_base: str = "http://127.0.0.1:8080/v1"
    llm_model: str = "llama-3.2-3b-instruct"
    llm_timeout_s: float = 6
    # Cloud AI 100 — the key is the project's only secret (§18): repr=False keeps
    # it out of logs and error messages
    cloud_api_base: str = "https://PLACEHOLDER.example"
    cloud_api_key: str = Field(default=_PLACEHOLDER, repr=False)
    cloud_model: str = _PLACEHOLDER
    cloud_timeout_s: float = 20
    # remote push (Phase 3b, D-005) — empty = feature OFF; the topic is a
    # capability (anyone who knows it can subscribe), so it lives only in .env
    remote_notify_url: str = ""
    remote_notify_topic: str = ""
    remote_notify_timeout_s: float = 5
    # D-008: re-page the caregiver while an L3 alert stays unacknowledged —
    # server-side insistence wakes a sleeper. 0 disables re-paging; max_pages
    # caps TOTAL notifications per alert (first page included)
    remote_notify_repeat_s: float = 90
    remote_notify_max_pages: int = 5

    def _anchored(self, p: Path) -> Path:
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def db_file(self) -> Path:
        return self._anchored(self.db_path)

    @property
    def models_path(self) -> Path:
        return self._anchored(self.models_dir)

    @property
    def log_path(self) -> Path:
        return self._anchored(self.log_dir)

    @property
    def ep(self) -> str:
        """§9 health `ep` field — must stay truthful (§26); Phase 9 flips USE_NPU."""
        return "qnn" if self.use_npu else "cpu"

    @property
    def cloud_configured(self) -> bool:
        """§15: placeholder cloud trio = cloud engine auto-disabled (not an error)."""
        return not (
            _PLACEHOLDER in self.cloud_api_base
            or self.cloud_api_key == _PLACEHOLDER
            or self.cloud_model == _PLACEHOLDER
        )

    @property
    def remote_notify_configured(self) -> bool:
        """D-005: both keys set = remote push ON; anything else = provably inert."""
        return bool(self.remote_notify_url and self.remote_notify_topic)

    @staticmethod
    def _keywords(csv: str) -> tuple[str, ...]:
        return tuple(k.strip().lower() for k in csv.split(",") if k.strip())

    @property
    def help_keyword_list(self) -> tuple[str, ...]:
        return self._keywords(self.help_keywords)

    @property
    def ok_keyword_list(self) -> tuple[str, ...]:
        return self._keywords(self.ok_keywords)


def load() -> Settings:
    """§8 entry: fail fast at startup, naming each bad variable (§6.1)."""
    try:
        return Settings()
    except ValidationError as e:
        bad = ", ".join(sorted({str(err["loc"][0]).upper() for err in e.errors()}))
        raise SystemExit(f"config invalid — fix these .env variables: {bad}") from e
