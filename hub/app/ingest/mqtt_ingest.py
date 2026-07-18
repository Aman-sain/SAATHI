"""MQTT ingest (§6.3): paho bridge node → bus + storage.

- Validates payloads against domain.Telemetry/NodeEvent; invalid → WARN + drop
  (never crash — a misbehaving node must not touch the emergency loop).
- Telemetry persisted BATCHED (buffer flushed every 2 s = one DB write).
- Reconnect loop with 1→8 s backoff (paho's own reconnect machinery).
- Node liveness: no telemetry for 10 s → node down + system.health event;
  next message flips it back up.

Thread rule (§6.2): paho callbacks run on paho's network thread; the bus is
loop-affine, so everything is handed to the event loop via call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from app.bus import EventBus
from app.config import Settings
from app.domain import NodeEvent, Telemetry
from app.storage.repo import Repo

log = logging.getLogger("ingest")

TELEMETRY_TOPIC = "saathi/node/+/telemetry"
EVENT_TOPIC = "saathi/node/+/event"
RECONNECT_MIN_S, RECONNECT_MAX_S = 1, 8


class MqttIngest:
    def __init__(
        self,
        bus: EventBus,
        repo: Repo,
        settings: Settings,
        health=None,                    # HealthRegistry, optional in tests
        *,
        flush_interval_s: float = 2.0,  # §6.3 batch cadence
        liveness_timeout_s: float = 10.0,
        client_factory=None,            # tests inject a fake paho client here
    ) -> None:
        self._bus = bus
        self._repo = repo
        self._settings = settings
        self._health = health
        self._flush_interval_s = flush_interval_s
        self._liveness_timeout_s = liveness_timeout_s
        self._client_factory = client_factory or (
            lambda: mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        )
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._tel_buffer: list[Telemetry] = []
        self._last_msg_mono: float | None = None
        self._node_online = False
        self.latest_telemetry: Telemetry | None = None  # /api/status reads this

    @property
    def node_online(self) -> bool:
        return self._node_online

    # --- lifecycle (called from the lifespan, loop already running) ---

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        client = self._client_factory()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(RECONNECT_MIN_S, RECONNECT_MAX_S)
        # async connect + own network thread: a dead broker just means retries,
        # never a startup failure (§8: ingest is not a REQUIRED subsystem)
        client.connect_async(self._settings.mqtt_host, self._settings.mqtt_port)
        client.loop_start()
        self._client = client
        self._tasks = [
            asyncio.create_task(self._flush_loop(), name="ingest-flush"),
            asyncio.create_task(self._watchdog(), name="ingest-watchdog"),
        ]
        log.info("ingest up broker=%s:%s", self._settings.mqtt_host, self._settings.mqtt_port)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._flush_telemetry()  # don't drop the last partial batch
        log.info("ingest stopped")

    # --- paho callbacks (network thread — hand off, don't touch the bus) ---

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        client.subscribe(TELEMETRY_TOPIC)
        client.subscribe(EVENT_TOPIC, qos=1)
        log.info("broker connected rc=%s", reason_code)
        self._call_in_loop(self._set_health, "broker", "up")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        log.warning("broker disconnected rc=%s — reconnecting %s→%s s",
                    reason_code, RECONNECT_MIN_S, RECONNECT_MAX_S)
        self._call_in_loop(self._set_health, "broker", "down")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload)
            if msg.topic.endswith("/telemetry"):
                model: Telemetry | NodeEvent = Telemetry.model_validate(data)
            elif msg.topic.endswith("/event"):
                model = NodeEvent.model_validate(data)
            else:
                log.warning("unexpected topic=%s dropped", msg.topic)
                return
        except (ValueError, ValidationError) as e:
            log.warning("invalid payload topic=%s dropped err=%s", msg.topic, e)
            return
        self._call_in_loop(self._deliver, model)

    def _call_in_loop(self, fn, *args) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    # --- loop-thread side ---

    def _deliver(self, model: Telemetry | NodeEvent) -> None:
        self._last_msg_mono = time.monotonic()
        if not self._node_online:
            self._node_online = True
            self._set_health("node", "up")
        if isinstance(model, Telemetry):
            self.latest_telemetry = model
            self._bus.publish("node.telemetry", model)
            self._tel_buffer.append(model)
        else:
            self._bus.publish("node.event", model)
            # node events are domain events worth keeping (§10 events table)
            self._repo.insert_event(
                ts=model.ts, source=model.node_id, type=model.type,
                payload={"value": model.value},
            )

    def _set_health(self, name: str, status: str) -> None:
        if self._health is not None:
            self._health.set(name, status)
        self._bus.publish("system.health", {"subsystem": name, "status": status,
                                            "ts": time.time()})

    def _flush_telemetry(self) -> None:
        if self._tel_buffer:
            self._repo.insert_telemetry_batch(self._tel_buffer)
            self._tel_buffer = []

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            self._flush_telemetry()

    async def _watchdog(self) -> None:
        """§16: node silent > liveness timeout → health event `node:down`;
        auto-recovers on the next message (no state to reset)."""
        tick = min(1.0, self._liveness_timeout_s / 5)
        while True:
            await asyncio.sleep(tick)
            if (
                self._node_online
                and self._last_msg_mono is not None
                and time.monotonic() - self._last_msg_mono > self._liveness_timeout_s
            ):
                self._node_online = False
                log.warning("node silent >%ss — marking down", self._liveness_timeout_s)
                self._set_health("node", "down")
