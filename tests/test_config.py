"""§6.1 config tests: defaults load without .env, env overrides win, fail-fast names
the bad variable, secret stays out of repr; §17 logging format check."""

import logging
import re

import pytest

from app.config import REPO_ROOT, Settings, load
from app.main import setup_logging


def _no_env(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)  # hermetic: ignore any real .env


def test_defaults_load_without_env_file():
    s = _no_env()
    assert s.http_port == 8000
    assert s.mock_cloud is True  # §15: MOCK_CLOUD defaults ON until Phase 8
    assert s.gas_warn == 0.35 and s.gas_crit == 0.60
    assert s.ep == "cpu"
    assert s.cloud_configured is False


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("HTTP_PORT", "9000")
    monkeypatch.setenv("GAS_WARN", "0.5")
    monkeypatch.setenv("USE_NPU", "1")
    s = _no_env()
    assert s.http_port == 9000 and s.gas_warn == 0.5 and s.ep == "qnn"


def test_relative_paths_anchor_to_repo_root():
    s = _no_env()
    assert s.db_file == REPO_ROOT / "data" / "saathi.db"
    assert s.log_path.is_absolute()


def test_load_fails_fast_naming_the_variable(monkeypatch):
    monkeypatch.setenv("HTTP_PORT", "not-a-port")
    with pytest.raises(SystemExit, match="HTTP_PORT"):
        load()


def test_cloud_key_never_in_repr(monkeypatch):
    monkeypatch.setenv("CLOUD_API_KEY", "sk-super-secret")
    s = _no_env()
    assert "sk-super-secret" not in repr(s) and "sk-super-secret" not in str(s)


def test_log_format_matches_section_17(tmp_path):
    s = _no_env(log_dir=tmp_path / "logs")
    setup_logging(s)
    logging.getLogger("fusion").info("R-GAS fired gas_norm=0.62 thr=0.35")
    line = (s.log_path / "hub.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    assert re.match(
        r"^\d{2}:\d{2}:\d{2}\.\d{3} INFO \[fusion\] R-GAS fired gas_norm=0\.62 thr=0\.35$",
        line,
    )
    # tidy up the tagged handlers so the tmp log file is released on Windows
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_saathi", False)]:
        root.removeHandler(h)
        h.close()
