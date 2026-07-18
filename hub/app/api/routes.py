"""§9 REST endpoints — thin: read state, serialize (§5). Phase 3 ships routes
2–4, 6, 8 (8 lives in demo.py); route 5 (digest) lands with cloud in Phase 8,
route 7 (check-in audio) is a NICE-TO-HAVE.

Routes are sync `def` on purpose: they may block briefly on db.flush()
(read-your-write), and FastAPI runs sync routes on the threadpool.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.domain import Alert, StatusResponse

log = logging.getLogger("api")

router = APIRouter(prefix="/api")


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Contract #1: liveness + subsystem map. `status:"ok"` = the process answers;
    per-subsystem truth lives in the map (§26 honesty rule)."""
    app = request.app
    return {
        "status": "ok",
        "subsystems": app.state.health.snapshot(),
        "internet": app.state.health.internet,
        "ep": app.state.settings.ep,
    }


def build_status(app) -> StatusResponse:
    """Contract #2 payload — shared by GET /api/status and the 5-s WS push.
    camera_state is a constant until vision lands in Phase 6: the camera
    process genuinely does not run yet, and SLEEPING is the truthful label."""
    active = app.state.fusion.alerts.active()
    top = max(active, key=lambda a: (a.level, a.updated_ts), default=None)
    return StatusResponse(
        ts=time.time(),
        node_online=app.state.ingest.node_online,
        telemetry=app.state.ingest.latest_telemetry,
        active_alert=top,
        active_alert_level=top.level if top else 0,
        camera_state="SLEEPING",
        subsystems=app.state.health.snapshot(),
        internet=app.state.health.internet,
        ep=app.state.settings.ep,
    )


@router.get("/status")
def status(request: Request) -> StatusResponse:
    return build_status(request.app)


@router.get("/alerts")
def list_alerts(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    undelivered: int = Query(default=0, ge=0, le=1),
) -> dict[str, Any]:
    request.app.state.db.flush()  # alert writes are queued; history must not lag
    alerts = request.app.state.repo.list_alerts(limit=limit, undelivered=bool(undelivered))
    return {"alerts": alerts}


class AckBody(BaseModel):
    by: str = "caregiver"


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, request: Request, body: AckBody | None = None) -> Alert:
    """Contract #4. async on purpose: fusion.ack cancels the escalation timer and
    broadcasts on the loop-affine bus, so it must run ON the event loop."""
    fusion = request.app.state.fusion
    alert = fusion.alerts.get(alert_id)
    if alert is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ALERT_NOT_FOUND", "message": f"no alert {alert_id}"},
        )
    if alert.state in ("RESOLVED", "FALSE_ALARM"):
        raise HTTPException(
            status_code=409,
            detail={"code": "ALERT_ALREADY_RESOLVED",
                    "message": f"alert {alert_id} is {alert.state}"},
        )
    by = body.by if body else "caregiver"
    updated = fusion.ack(alert_id)
    log.info("alert %s acked by=%s", alert_id, by)
    return updated


@router.get("/events")
def list_events(
    request: Request,
    since_ts: float = Query(default=0.0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    request.app.state.db.flush()
    return {"events": request.app.state.repo.list_events(since_ts=since_ts, limit=limit)}
