"""Contract #8 / §26 demo panel: inject a synthetic bus event so the REAL
pipeline (fusion → alert → WS → screens) runs — nothing downstream is faked.

Every injection is stamped synthetic:true end-to-end (bus event → alert →
events table), so a judge can always tell staged from real. `help` drives
R-HELP (Phase 5) exactly like a real mic keyword; `fall` publishes to a topic
whose consumer lands in Phase 6 — until then the bus logs it and no alert is
produced, honest by construction.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.domain import ASREvent, NodeEvent, VisionEvent

log = logging.getLogger("demo")

router = APIRouter(prefix="/api/demo")

DEMO_NODE_ID = "demo"


class TriggerBody(BaseModel):
    scenario: Literal["gas", "fall", "help"]  # anything else → 422 (contract #8)
    # pinned True: this endpoint is INCAPABLE of injecting a non-synthetic event
    synthetic: Literal[True] = True


@router.post("/trigger")
async def trigger(body: TriggerBody, request: Request) -> dict[str, Any]:
    """async on purpose: publishes on the loop-affine bus."""
    app = request.app
    now = time.time()
    if body.scenario == "gas":
        gas = round(min(1.0, app.state.settings.gas_warn + 0.2), 2)
        topic, event = "node.event", NodeEvent(
            node_id=DEMO_NODE_ID, ts=now, type="GAS_HIGH", value=gas, synthetic=True
        )
    elif body.scenario == "fall":
        topic, event = "vision.event", VisionEvent(ts=now, conf=0.9, synthetic=True)
    else:  # help
        topic, event = "asr.event", ASREvent(
            ts=now, kind="HELP", keyword="help", synthetic=True
        )

    app.state.bus.publish(topic, event)
    app.state.repo.insert_event(
        ts=now, source="demo", type=f"DEMO_{body.scenario.upper()}",
        payload=event.model_dump(), synthetic=True,
    )
    log.info("SYNTHETIC trigger scenario=%s topic=%s", body.scenario, topic)
    return {"injected": {"scenario": body.scenario, "topic": topic, "synthetic": True}}
