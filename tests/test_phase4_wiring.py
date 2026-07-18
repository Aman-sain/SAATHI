"""Phase 4 wiring tests, end-to-end through the real app: a synthetic gas
trigger escalates and its message upgrades from template to LLM text over the
existing WS push (§13); with the llama server dead the same flow ships and
keeps the template (the §20 completion criterion); speak.request reaches TTS
playback; MOCK_LLM=1 skips the client entirely."""

import time

import httpx
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.main import create_app
from app.pipelines import tts as tts_module
from app.pipelines.llm import LlmClient


def _settings(tmp_path, **kw) -> Settings:
    kw.setdefault("db_path", tmp_path / "saathi.db")
    kw.setdefault("log_dir", tmp_path / "logs")
    kw.setdefault("escalate_seconds", 0.2)  # test-speed escalation timer
    # hermetic: dev machines stage the real whisper pair, which would
    # otherwise load and open the real mic during the lifespan
    kw.setdefault("models_dir", tmp_path)
    return Settings(_env_file=None, **kw)


def _trigger_gas(client) -> None:
    r = client.post("/api/demo/trigger", json={"scenario": "gas", "synthetic": True})
    assert r.status_code == 200


def _receive_until(ws, predicate, max_messages=40):
    for _ in range(max_messages):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected WS message never arrived")


def _patch_llm_transport(monkeypatch, handler) -> None:
    """main.py builds its own LlmClient; swap in one that talks to a fake server."""

    def fake(settings, on_health=None, transport=None):
        return LlmClient(settings, on_health, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(main_module, "LlmClient", fake)


def test_escalated_alert_upgrades_to_llm_text_over_ws(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content":
                "Gas levels rose at home and no movement was seen; please check in."}}]},
        )

    _patch_llm_transport(monkeypatch, handler)
    with TestClient(create_app(_settings(tmp_path))) as client:
        with client.websocket_connect("/ws/caregiver") as ws:
            assert ws.receive_json()["type"] == "status"
            _trigger_gas(client)
            upgraded = _receive_until(
                ws,
                lambda m: m["type"] == "alert.updated"
                and m["alert"]["message_engine"] == "local-llm",
            )
        assert upgraded["alert"]["level"] == 3
        assert "please check in" in upgraded["alert"]["message"]
        # upgrade was persisted, not just broadcast
        stored = client.get("/api/alerts").json()["alerts"][0]
        assert stored["message_engine"] == "local-llm"


def test_llama_server_killed_everything_still_flows_template(tmp_path):
    """§20 verbatim: 'with llama server killed, everything still flows (template)'.
    No patching: the real client points at localhost:8080 where nothing listens.
    (short llm_timeout_s: a dead port on this box times out, not refuses)"""
    with TestClient(create_app(_settings(tmp_path, llm_timeout_s=0.5))) as client:
        with client.websocket_connect("/ws/caregiver") as ws:
            assert ws.receive_json()["type"] == "status"
            _trigger_gas(client)
            escalated = _receive_until(
                ws,
                lambda m: m["type"] == "alert.updated"
                and m["alert"]["state"] == "ESCALATED",
            )
        assert escalated["alert"]["level"] == 3
        assert escalated["alert"]["message_engine"] == "template"
        assert escalated["alert"]["message"].startswith("[GAS]")
        assert client.app.state.llm is not None  # client exists, server just dead


def test_speak_request_reaches_tts_playback(tmp_path, monkeypatch):
    played = []
    monkeypatch.setattr(tts_module, "_start_play_async", lambda p: played.append(p.name))
    monkeypatch.setattr(tts_module, "_wav_seconds", lambda p: 0.0)
    monkeypatch.setattr(tts_module, "winsound", object())

    with TestClient(create_app(_settings(tmp_path, mock_llm=True))) as client:
        _trigger_gas(client)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not played:
            time.sleep(0.02)
    assert "gas_warning_hi.wav" in played  # J1.3: the announcement is audible


def test_mock_llm_skips_client_and_upgrader(tmp_path):
    with TestClient(create_app(_settings(tmp_path, mock_llm=True))) as client:
        assert client.app.state.llm is None
        assert client.app.state.llm_upgrader is None
