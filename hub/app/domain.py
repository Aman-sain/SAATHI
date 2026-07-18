"""Shared vocabulary (§6): the pydantic models every module speaks.

Placement rule (§5): imported by everyone, imports nothing from the app.
Telemetry/NodeEvent/Alert mirror the FROZEN shapes in docs/CONTRACTS.md —
changing a field here requires the deviation protocol (§27).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

NodeEventType = Literal["GAS_HIGH", "GAS_CRIT", "MOTION", "LOUD_NOISE", "NODE_BOOT"]
AlertKind = Literal["GAS", "FALL", "HELP", "NOISE"]
AlertState = Literal["OPEN", "ANNOUNCED", "ACKED", "ESCALATED", "RESOLVED", "FALSE_ALARM"]
MessageEngine = Literal["template", "local-llm", "cloud-ai-100", "mock"]
CameraState = Literal["SLEEPING", "VERIFYING", "INCIDENT"]


class Telemetry(BaseModel):
    """CONTRACTS.md §1 — node telemetry, published every 2 s."""

    node_id: str
    ts: float
    gas_raw: int = Field(ge=0)
    gas_norm: float = Field(ge=0.0, le=1.0)
    temp_c: float
    motion: bool
    sound_rms: float = Field(ge=0.0, le=1.0)
    fw: str = "unknown"


class NodeEvent(BaseModel):
    """CONTRACTS.md §1 — edge-triggered node event.

    `synthetic` is hub-internal (contract #8: demo injections are flagged
    synthetic:true end-to-end). exclude=True keeps model_dump() byte-identical
    to the frozen wire shape — consumers read the attribute, never the dump.
    """

    node_id: str
    ts: float
    type: NodeEventType
    value: float | None = None
    synthetic: bool = Field(default=False, exclude=True)


class VisionEvent(BaseModel):
    """Fall verdict from the vision pipeline (§11.3) — keypoints only, never frames."""

    ts: float
    kind: Literal["FALL"] = "FALL"
    conf: float = Field(ge=0.0, le=1.0)
    synthetic: bool = False


class ASREvent(BaseModel):
    """Matched keyword only (§10 privacy invariant: full transcripts never persist)."""

    ts: float
    kind: Literal["HELP", "OK"]
    keyword: str
    conf: float = Field(default=1.0, ge=0.0, le=1.0)
    synthetic: bool = False


class Alert(BaseModel):
    """CONTRACTS.md §2 — the canonical Alert object."""

    id: str
    kind: AlertKind
    level: int = Field(ge=1, le=3)
    state: AlertState = "OPEN"
    title: str
    message: str
    message_engine: MessageEngine = "template"
    facts: dict[str, Any] = Field(default_factory=dict)
    created_ts: float
    updated_ts: float
    synthetic: bool = False


class Digest(BaseModel):
    """Daily caregiver digest (§12); engine label is always truthful (§26)."""

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    text: str
    engine: MessageEngine
    created_ts: float


class StatusResponse(BaseModel):
    """CONTRACTS.md #2 — the PWA home payload, also pushed over WS every 5 s.
    `active_alert_level` 0 = all OK (drives the §7 status ring); subsystems map
    mirrors /api/health so the dashboard health strip rides the same push."""

    ts: float
    node_online: bool = False
    telemetry: Telemetry | None = None
    active_alert: Alert | None = None
    active_alert_level: int = Field(default=0, ge=0, le=3)
    camera_state: CameraState = "SLEEPING"
    subsystems: dict[str, str] = Field(default_factory=dict)
    internet: bool = False
    ep: str = "cpu"
