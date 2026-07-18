"""§19 WS tests: connect, receive alert.created after a synthetic trigger,
delivered-flag bookkeeping, and broadcast surviving a dead client (§16)."""

import asyncio

from fastapi.testclient import TestClient

from app.api.ws import WSManager
from app.config import Settings
from app.main import create_app


def _settings(tmp_path) -> Settings:
    # models_dir=tmp_path: hermetic — dev machines stage the real whisper pair,
    # which would otherwise load and open the real mic during the lifespan
    return Settings(
        _env_file=None, db_path=tmp_path / "saathi.db", log_dir=tmp_path / "logs",
        models_dir=tmp_path,
    )


def _receive_until(ws, wanted_type: str, max_messages: int = 30) -> dict:
    for _ in range(max_messages):
        msg = ws.receive_json()
        if msg["type"] == wanted_type:
            return msg
    raise AssertionError(f"no {wanted_type!r} within {max_messages} messages")


def test_ws_connect_sends_status_snapshot(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        with client.websocket_connect("/ws/caregiver") as ws:
            msg = ws.receive_json()
    assert msg["type"] == "status"
    assert msg["status"]["active_alert_level"] == 0
    assert msg["status"]["camera_state"] == "SLEEPING"


def test_caregiver_receives_alert_created_after_synthetic_trigger(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        with client.websocket_connect("/ws/caregiver") as ws:
            assert ws.receive_json()["type"] == "status"  # connect snapshot

            client.post("/api/demo/trigger", json={"scenario": "gas", "synthetic": True})

            created = _receive_until(ws, "alert.created")
            assert created["alert"]["kind"] == "GAS"
            assert created["alert"]["synthetic"] is True

            updated = _receive_until(ws, "alert.updated")
            assert updated["alert"]["state"] == "ANNOUNCED"

        # a caregiver received it → delivered flag set (contract #3)
        assert client.get("/api/alerts?undelivered=1").json()["alerts"] == []
        assert len(client.get("/api/alerts").json()["alerts"]) == 1


def test_dashboard_gets_ticker_busevents(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        with client.websocket_connect("/ws/dashboard") as ws:
            assert ws.receive_json()["type"] == "status"
            client.post("/api/demo/trigger", json={"scenario": "gas", "synthetic": True})
            ev = _receive_until(ws, "busevent")
            assert set(ev) == {"type", "text", "level", "ts"}
            assert ev["level"] in ("info", "warn", "crit")


def test_broadcast_drops_dead_client_and_continues():
    class GoodWS:
        def __init__(self):
            self.got = []

        async def send_json(self, m):
            self.got.append(m)

    class DeadWS:
        async def send_json(self, m):
            raise RuntimeError("gone")

    async def run() -> tuple[int, GoodWS]:
        manager = WSManager("test")
        good, dead = GoodWS(), DeadWS()
        manager._clients.update({good, dead})
        delivered = await manager.broadcast({"type": "status"})
        return delivered, good

    delivered, good = asyncio.run(run())
    assert delivered == 1
    assert good.got == [{"type": "status"}]
