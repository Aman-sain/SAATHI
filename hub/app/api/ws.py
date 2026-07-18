"""WS managers (§6.13 + CONTRACTS.md §3): /ws/caregiver + /ws/dashboard.

The WsBroadcaster is the bus→WS bridge — the last hop of the emergency loop
(§13 sensor→fusion→TTS→WS). Alert and status messages fan out to both
channels; the dashboard additionally gets every bus event as a human-readable
ticker line. A failing client is dropped and never breaks the broadcast (§16).
Delivery of an alert to ≥1 caregiver client sets the alert's `delivered` flag
(contract #3 `undelivered` filter).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from app.api.routes import build_status
from app.bus import TOPICS
from app.domain import Alert, NodeEvent, Telemetry

log = logging.getLogger("ws")

router = APIRouter()

STATUS_PUSH_INTERVAL_S = 5.0  # contract §3: {"type":"status"} every 5 s


class WSManager:
    """Register/unregister + JSON broadcast for one WS channel (§6.13)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        log.info("ws %s connected clients=%d", self.name, len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        log.info("ws %s disconnected clients=%d", self.name, len(self._clients))

    async def broadcast(self, message: dict[str, Any]) -> int:
        """Returns how many clients actually received the message."""
        delivered = 0
        for ws in list(self._clients):
            try:
                await ws.send_json(message)
                delivered += 1
            except Exception:
                log.warning("ws %s send failed — dropping client", self.name)
                self.disconnect(ws)
        return delivered


class WsBroadcaster:
    """One pump task per bus topic + the 5-s status push. Owns both managers."""

    def __init__(self, app) -> None:
        self._app = app
        self.caregiver = WSManager("caregiver")
        self.dashboard = WSManager("dashboard")
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for topic in TOPICS:
            stream = self._app.state.bus.subscribe(topic)
            self._tasks.append(
                asyncio.create_task(self._pump(topic, stream), name=f"ws-{topic}")
            )
        self._tasks.append(
            asyncio.create_task(self._status_loop(), name="ws-status-push")
        )
        log.info("ws broadcaster up topics=%d", len(TOPICS))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("ws broadcaster stopped")

    async def _pump(self, topic: str, stream) -> None:
        async for payload in stream:
            try:
                await self._dispatch(topic, payload)
            except Exception:
                log.exception("ws dispatch failed topic=%s", topic)  # §16: pump never dies

    async def _dispatch(self, topic: str, payload: Any) -> None:
        if topic in ("alert.created", "alert.updated") and isinstance(payload, Alert):
            message = {"type": topic, "alert": jsonable_encoder(payload)}
            received = await self.caregiver.broadcast(message)
            await self.dashboard.broadcast(message)
            if received:
                self._app.state.repo.mark_alert_delivered(payload.id)
        elif topic == "vision.keypoints" and isinstance(payload, dict):
            # ≤10 Hz frames go to the skeleton canvas, not the ticker
            await self.dashboard.broadcast({"type": "keypoints", **payload})
            return
        await self.dashboard.broadcast(_busevent(topic, payload))

    async def _status_loop(self) -> None:
        while True:
            message = {"type": "status", "status": jsonable_encoder(build_status(self._app))}
            await self.caregiver.broadcast(message)
            await self.dashboard.broadcast(message)
            await asyncio.sleep(STATUS_PUSH_INTERVAL_S)


def _busevent(topic: str, payload: Any) -> dict[str, Any]:
    text, level = _describe(topic, payload)
    return {"type": "busevent", "text": text, "level": level, "ts": time.time()}


def _describe(topic: str, payload: Any) -> tuple[str, str]:
    """Human-readable ticker line + severity for the §7 dashboard."""
    if isinstance(payload, Telemetry):
        motion = "yes" if payload.motion else "no"
        return (
            f"telemetry gas={payload.gas_norm:.2f} temp={payload.temp_c:.1f}C motion={motion}",
            "info",
        )
    if isinstance(payload, NodeEvent):
        level = "warn" if payload.type in ("GAS_HIGH", "GAS_CRIT", "LOUD_NOISE") else "info"
        return f"node {payload.type} value={payload.value}", level
    if isinstance(payload, Alert):
        verb = "opened" if topic == "alert.created" else payload.state
        level = "crit" if payload.level >= 3 else "warn"
        return f"alert {payload.id} {verb} {payload.kind} L{payload.level}", level
    if topic == "speak.request" and isinstance(payload, dict):
        return f"speak {payload.get('phrase_id', '?')}", "info"
    if topic == "system.health" and isinstance(payload, dict):
        status = payload.get("status", "?")
        level = "info" if status == "up" else "warn"
        return f"health {payload.get('subsystem', '?')} {status}", level
    return f"{topic} {payload!r}"[:120], "info"


async def _serve(manager: WSManager, ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        # paint-fast snapshot; the 5-s status push takes over from here
        await ws.send_json(
            {"type": "status", "status": jsonable_encoder(build_status(ws.app))}
        )
        while True:
            await ws.receive_json()  # phone→hub is only {"type":"ping"} — consume
    except WebSocketDisconnect:
        pass
    except Exception:
        log.warning("ws %s receive error — dropping client", manager.name)
    finally:
        manager.disconnect(ws)


@router.websocket("/ws/caregiver")
async def ws_caregiver(ws: WebSocket) -> None:
    await _serve(ws.app.state.ws.caregiver, ws)


@router.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket) -> None:
    await _serve(ws.app.state.ws.dashboard, ws)
