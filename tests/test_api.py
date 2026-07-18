"""§19 API tests: every endpoint's success + main error (CONTRACTS.md §2),
the §16 error schema, and DB restart-safety through full app lifecycles."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import Alert, Telemetry
from app.main import create_app


def _settings(tmp_path) -> Settings:
    # mqtt_port=1: hermetic — the dev laptop runs a REAL mosquitto service, and
    # paho sometimes won the connect race before the health GET, flipping the
    # broker chip "up" and flaking the subsystem-map assertion below.
    # models_dir=tmp_path: same idea — dev machines stage the real whisper pair,
    # which would otherwise load (396 MB) and open the real mic mid-test
    return Settings(
        _env_file=None, db_path=tmp_path / "saathi.db", log_dir=tmp_path / "logs",
        mqtt_port=1, models_dir=tmp_path,
    )


def test_health_returns_subsystem_map(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:  # `with` runs lifespan
        body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["subsystems"]["storage"] == "up"
    assert body["subsystems"]["bus"] == "up"
    for pending in ("node", "broker", "vision", "asr", "llm", "cloud"):
        assert body["subsystems"][pending] == "down"  # §26: never claim what isn't running
    assert body["internet"] is False
    assert body["ep"] == "cpu"


def test_db_survives_full_app_restart(tmp_path):
    settings = _settings(tmp_path)
    # RESOLVED: a final state, so Phase 2's §14 restart rule (stale OPEN/ANNOUNCED
    # alerts escalate on startup) leaves it untouched — this test is about the DB
    alert = Alert(
        id="a-rest", kind="GAS", level=2, state="RESOLVED", title="Gas warning",
        message="restart survivor", created_ts=1.0, updated_ts=1.0,
    )

    app1 = create_app(settings)
    with TestClient(app1):
        app1.state.repo.upsert_alert(alert)
        app1.state.db.flush()

    app2 = create_app(settings)  # simulated hub restart, same DB file
    with TestClient(app2):
        assert app2.state.repo.get_alert("a-rest") == alert


# --- contract #2: GET /api/status ---


def test_status_starts_quiet(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        body = client.get("/api/status").json()
    assert body["node_online"] is False
    assert body["telemetry"] is None
    assert body["active_alert"] is None
    assert body["active_alert_level"] == 0
    assert body["camera_state"] == "SLEEPING"
    assert body["internet"] is False
    assert body["ep"] == "cpu"
    assert body["subsystems"]["storage"] == "up"


def test_status_reflects_telemetry_and_active_alert(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.ingest.latest_telemetry = Telemetry(
            node_id="node1", ts=5.0, gas_raw=300, gas_norm=0.4,
            temp_c=31.0, motion=False, sound_rms=0.1,
        )
        app.state.ingest._node_online = True
        # restore() = in-memory adopt, no bus publish — loop-affinity safe from
        # the test thread (the engine does the same after a restart, §14)
        app.state.fusion.alerts.restore(Alert(
            id="a-st1", kind="GAS", level=2, state="ANNOUNCED", title="Gas warning",
            message="m", created_ts=1.0, updated_ts=1.0,
        ))
        body = client.get("/api/status").json()
    assert body["node_online"] is True
    assert body["telemetry"]["gas_norm"] == 0.4
    assert body["active_alert"]["id"] == "a-st1"
    assert body["active_alert_level"] == 2


# --- contract #3: GET /api/alerts ---


def _alert(i: int, **kw) -> Alert:
    base = dict(
        id=f"a-l{i}", kind="GAS", level=2, state="RESOLVED", title="t",
        message="m", created_ts=float(i), updated_ts=float(i),
    )
    return Alert(**{**base, **kw})


def test_alerts_history_newest_first_with_filters(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        for i in range(3):
            app.state.repo.upsert_alert(_alert(i))
        app.state.repo.mark_alert_delivered("a-l2")

        ids = [a["id"] for a in client.get("/api/alerts").json()["alerts"]]
        assert ids == ["a-l2", "a-l1", "a-l0"]

        assert len(client.get("/api/alerts?limit=1").json()["alerts"]) == 1

        undelivered = client.get("/api/alerts?undelivered=1").json()["alerts"]
        assert [a["id"] for a in undelivered] == ["a-l1", "a-l0"]


def test_alerts_bad_params_use_error_schema(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.get("/api/alerts?limit=0")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert "limit" in err["message"]
    assert err["request_id"] == resp.headers["X-Request-ID"]


# --- §7 frontends served same-origin ---


def test_pwa_shell_served_at_app(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.get("/app/")
        assert resp.status_code == 200
        assert "SAATHI" in resp.text
        for asset in ("app.js", "style.css", "manifest.json", "sw.js"):
            assert client.get(f"/app/{asset}").status_code == 200


def test_dashboard_shell_served_at_dash(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.get("/dash/")
        assert resp.status_code == 200
        assert "SAATHI" in resp.text
        for asset in ("dash.js", "skeleton.js", "style.css"):
            assert client.get(f"/dash/{asset}").status_code == 200


# --- contract #4: POST /api/alerts/{id}/ack ---


def test_ack_round_trips_and_persists(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.fusion.alerts.restore(_alert(9, id="a-ack", state="ANNOUNCED"))

        resp = client.post("/api/alerts/a-ack/ack", json={"by": "caregiver"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "RESOLVED"  # ack chain: ACKED → RESOLVED

        app.state.db.flush()
        assert app.state.repo.get_alert("a-ack").state == "RESOLVED"
        # no longer active — the status ring goes back to ALL OK
        assert client.get("/api/status").json()["active_alert_level"] == 0


def test_ack_unknown_id_404(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.post("/api/alerts/a-nope/ack", json={"by": "caregiver"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ALERT_NOT_FOUND"


def test_ack_already_resolved_409(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.repo.upsert_alert(_alert(9, id="a-done", state="RESOLVED"))
        app.state.db.flush()
        resp = client.post("/api/alerts/a-done/ack", json={"by": "caregiver"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ALERT_ALREADY_RESOLVED"


# --- contract #6: GET /api/events ---


def test_events_since_ts_filter(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        for ts, typ in ((1.0, "NODE_BOOT"), (2.0, "GAS_HIGH")):
            app.state.repo.insert_event(ts=ts, source="node1", type=typ)
        events = client.get("/api/events?since_ts=1.5").json()["events"]
        assert [e["type"] for e in events] == ["GAS_HIGH"]

        assert client.get("/api/events?limit=-1").status_code == 422
