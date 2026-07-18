"""Contract #8: /api/demo/trigger injects a synthetic bus event that drives the
REAL pipeline, stamped synthetic:true end-to-end (§26 honesty rule)."""

import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _settings(tmp_path) -> Settings:
    # models_dir=tmp_path: hermetic — dev machines stage the real whisper pair,
    # which would otherwise load and open the real mic during the lifespan
    return Settings(
        _env_file=None, db_path=tmp_path / "saathi.db", log_dir=tmp_path / "logs",
        models_dir=tmp_path,
    )


def _wait_for_alerts(client, timeout_s: float = 3.0) -> list[dict]:
    # the fusion consumer runs on the app's loop between requests — poll briefly
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        alerts = client.get("/api/alerts").json()["alerts"]
        if alerts:
            return alerts
        time.sleep(0.05)
    return []


def test_demo_gas_runs_the_real_pipeline_as_synthetic(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.post(
            "/api/demo/trigger", json={"scenario": "gas", "synthetic": True}
        )
        assert resp.status_code == 200
        assert resp.json()["injected"]["scenario"] == "gas"

        alerts = _wait_for_alerts(client)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["kind"] == "GAS"
        assert alert["state"] == "ANNOUNCED"  # real R-GAS + state machine ran
        assert alert["synthetic"] is True    # taint survived the whole pipeline

        events = client.get("/api/events").json()["events"]
        demo_events = [e for e in events if e["type"] == "DEMO_GAS"]
        assert demo_events and demo_events[0]["synthetic"] is True


def test_demo_help_runs_r_help_as_synthetic(tmp_path):
    # §20 Phase-5 criterion: the MOCK_ASR path fires via the demo endpoint —
    # R-HELP opens straight at L3 (no warning stage for a cry for help)
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.post(
            "/api/demo/trigger", json={"scenario": "help", "synthetic": True}
        )
        assert resp.status_code == 200
        assert resp.json()["injected"]["topic"] == "asr.event"

        alerts = _wait_for_alerts(client)
        assert len(alerts) == 1
        alert = alerts[0]
        assert (alert["kind"], alert["level"]) == ("HELP", 3)
        assert alert["state"] == "ANNOUNCED"
        assert alert["synthetic"] is True
        assert alert["facts"]["keyword"] == "help"

        events = client.get("/api/events").json()["events"]
        demo_events = [e for e in events if e["type"] == "DEMO_HELP"]
        assert demo_events and demo_events[0]["synthetic"] is True


def test_demo_fall_injects_but_no_rule_consumes_yet(tmp_path):
    # vision.event has no consumer until Phase 6: injection logs, no alert opens
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.post(
            "/api/demo/trigger", json={"scenario": "fall", "synthetic": True}
        )
        assert resp.status_code == 200
        assert client.get("/api/alerts").json()["alerts"] == []


def test_demo_unknown_scenario_422(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.post(
            "/api/demo/trigger", json={"scenario": "dance", "synthetic": True}
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_demo_cannot_inject_non_synthetic(tmp_path):
    # the endpoint is incapable of injecting a "real" event — §26 by construction
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.post(
            "/api/demo/trigger", json={"scenario": "gas", "synthetic": False}
        )
    assert resp.status_code == 422
