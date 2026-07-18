"""§6.3 ingest tests with an injected fake paho client — no broker required.
Valid payloads reach bus + DB; garbage is dropped without crashing; the
liveness watchdog flips node down after silence and back up on traffic."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.bus import EventBus
from app.config import Settings
from app.domain import NodeEvent, Telemetry
from app.ingest.mqtt_ingest import MqttIngest
from app.storage.db import Database
from app.storage.repo import Repo


class FakeMqttClient:
    """Records what ingest asks of paho; lets tests fire the callbacks."""

    def __init__(self):
        self.on_connect = self.on_disconnect = self.on_message = None
        self.subscribed: list[tuple[str, int]] = []
        self.connect_args = self.reconnect_delays = None
        self.loop_started = self.disconnected = False

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect_delays = (min_delay, max_delay)

    def connect_async(self, host, port):
        self.connect_args = (host, port)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    # --- test helpers (simulate the paho network thread) ---

    def fire_connect(self):
        self.on_connect(self, None, {}, 0, None)

    def fire_message(self, topic: str, payload) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=body))


class FakeHealth:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def set(self, name, status):
        self.calls.append((name, status))


TELEMETRY = {"node_id": "node1", "ts": 1752787200.5, "gas_raw": 312, "gas_norm": 0.18,
             "temp_c": 31.5, "motion": False, "sound_rms": 0.04, "fw": "1.0"}
EVENT = {"node_id": "node1", "ts": 1752787201.1, "type": "GAS_HIGH", "value": 0.62}


@pytest.fixture
def stack(tmp_path):
    db = Database(tmp_path / "saathi.db")
    db.start()
    settings = Settings(_env_file=None, db_path=tmp_path / "saathi.db")
    fake, health = FakeMqttClient(), FakeHealth()
    ingest = MqttIngest(
        EventBus(), Repo(db), settings, health,
        flush_interval_s=0.03, liveness_timeout_s=0.15,
        client_factory=lambda: fake,
    )
    yield ingest, fake, health, Repo(db), db
    db.stop()


@pytest.mark.asyncio
async def test_connect_subscribes_contract_topics_with_backoff(stack):
    ingest, fake, health, *_ = stack
    ingest.start()
    try:
        fake.fire_connect()
        await asyncio.sleep(0)
        assert fake.connect_args == ("127.0.0.1", 1883)      # §15 defaults
        assert fake.reconnect_delays == (1, 8)                # §6.3 backoff 1→8 s
        assert ("saathi/node/+/telemetry", 0) in fake.subscribed
        assert ("saathi/node/+/event", 1) in fake.subscribed  # qos=1 per contract
        assert ("broker", "up") in health.calls
    finally:
        await ingest.stop()


@pytest.mark.asyncio
async def test_valid_telemetry_hits_bus_and_db_batched(stack):
    ingest, fake, health, repo, db = stack
    sub = ingest._bus.subscribe("node.telemetry")
    ingest.start()
    try:
        fake.fire_message("saathi/node/node1/telemetry", TELEMETRY)
        got = await asyncio.wait_for(anext(sub), 1)
        assert isinstance(got, Telemetry) and got.gas_norm == 0.18
        assert ("node", "up") in health.calls                 # first traffic = node up
        await asyncio.sleep(0.06)                             # one flush interval
        db.flush()
        assert db.query("SELECT COUNT(*) c FROM telemetry")[0]["c"] == 1
    finally:
        await ingest.stop()


@pytest.mark.asyncio
async def test_valid_node_event_hits_bus_and_events_table(stack):
    ingest, fake, _, repo, db = stack
    sub = ingest._bus.subscribe("node.event")
    ingest.start()
    try:
        fake.fire_message("saathi/node/node1/event", EVENT)
        got = await asyncio.wait_for(anext(sub), 1)
        assert isinstance(got, NodeEvent) and got.type == "GAS_HIGH"
        db.flush()
        (row,) = repo.list_events()
        assert row["source"] == "node1" and row["type"] == "GAS_HIGH"
        assert row["payload"] == {"value": 0.62}
    finally:
        await ingest.stop()


@pytest.mark.asyncio
async def test_garbage_payloads_dropped_never_crash(stack):
    ingest, fake, _, repo, db = stack
    sub_t = ingest._bus.subscribe("node.telemetry")
    sub_e = ingest._bus.subscribe("node.event")
    ingest.start()
    try:
        fake.fire_message("saathi/node/node1/telemetry", b"not json {")
        fake.fire_message("saathi/node/node1/telemetry", {"node_id": "node1"})  # fields missing
        fake.fire_message("saathi/node/node1/event",
                          {**EVENT, "type": "NOT_A_REAL_TYPE"})
        fake.fire_message("saathi/node/node1/unknown", TELEMETRY)  # wrong subtopic
        fake.fire_message("saathi/node/node1/telemetry", TELEMETRY)  # still alive after all that
        got = await asyncio.wait_for(anext(sub_t), 1)
        assert got.gas_norm == 0.18
        assert sub_e.__anext__ is not None  # no event ever surfaced
        db.flush()
        assert repo.list_events() == []
    finally:
        await ingest.stop()


@pytest.mark.asyncio
async def test_liveness_watchdog_marks_node_down_then_recovers(stack):
    ingest, fake, health, *_ = stack
    sub = ingest._bus.subscribe("system.health")
    ingest.start()
    try:
        fake.fire_message("saathi/node/node1/telemetry", TELEMETRY)
        up = await asyncio.wait_for(anext(sub), 1)
        assert (up["subsystem"], up["status"]) == ("node", "up")

        await asyncio.sleep(0.3)  # > liveness_timeout_s: silence
        down = await asyncio.wait_for(anext(sub), 1)
        assert (down["subsystem"], down["status"]) == ("node", "down")

        fake.fire_message("saathi/node/node1/telemetry", TELEMETRY)  # auto-recover
        up2 = await asyncio.wait_for(anext(sub), 1)
        assert (up2["subsystem"], up2["status"]) == ("node", "up")
    finally:
        await ingest.stop()


@pytest.mark.asyncio
async def test_stop_flushes_partial_batch_and_shuts_client(stack):
    ingest, fake, _, repo, db = stack
    ingest.start()
    fake.fire_message("saathi/node/node1/telemetry", TELEMETRY)
    await asyncio.sleep(0.01)   # delivered, but before any flush tick
    await ingest.stop()
    assert fake.disconnected and not fake.loop_started
    db.flush()
    assert db.query("SELECT COUNT(*) c FROM telemetry")[0]["c"] == 1  # not dropped
