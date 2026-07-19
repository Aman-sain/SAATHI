"""§9 REST endpoints — thin: read state, serialize (§5). Phase 3 shipped routes
2–4, 6, 8 (8 lives in demo.py); route 5 (digest) ships here with cloud (Phase
8/F3); route 7 (check-in audio) is a NICE-TO-HAVE.

Routes are sync `def` on purpose: they may block briefly on db.flush()
(read-your-write), and FastAPI runs sync routes on the threadpool. The two
exceptions are async: ack (must cancel timers ON the loop) and digest/generate
(awaits the cloud→local→template chain + WS broadcast).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.cloud import digest as cloud_digest
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


class DigestBody(BaseModel):
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/digest/generate")
async def generate_digest(request: Request, body: DigestBody | None = None) -> dict[str, Any]:
    """Contract #2 route 5 (Phase 8/F3): on-demand caregiver digest, OPTIONAL
    subsystem — cloud→local→template per §15, never on the emergency path.
    async on purpose: awaits the engine chain and broadcasts on the loop-affine
    WS managers. The chain itself never raises for reachability (§16), so the
    contract's 503 covers only a genuinely unexpected engine failure."""
    app = request.app
    day = body.date if body and body.date else time.strftime("%Y-%m-%d")
    try:
        start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": f"date: {day} is not a calendar date"},
        )
    app.state.db.flush()  # event writes are queued; the digest must see today
    events = [
        # "time" spares the LLM tiers from misreading epoch ts as clock time
        # (observed: a digest citing "14:40" for a 10:40 event); template ignores it
        {**e, "time": time.strftime("%H:%M", time.localtime(e["ts"]))}
        for e in app.state.repo.list_events(since_ts=start, limit=1000)
        if e["ts"] < start + 86400.0
    ]
    try:
        digest = await cloud_digest.generate_digest(
            app.state.settings, events, day, local_llm=app.state.llm
        )
    except Exception:
        log.exception("digest generation failed date=%s", day)
        raise HTTPException(
            status_code=503,
            detail={"code": "DIGEST_UNAVAILABLE", "message": "all digest engines failed"},
        )
    app.state.repo.insert_digest(
        date=digest.date, text=digest.text, engine=digest.engine, created_ts=digest.created_ts
    )
    message = {"type": "digest", "digest": jsonable_encoder(digest)}
    await app.state.ws.caregiver.broadcast(message)
    await app.state.ws.dashboard.broadcast(message)
    log.info("digest generated date=%s engine=%s events=%d chars=%d",
             digest.date, digest.engine, len(events), len(digest.text))
    if digest.engine == "cloud-ai-100":
        app.state.health.set("cloud", "up")
    elif not app.state.settings.mock_cloud and app.state.settings.cloud_configured:
        app.state.health.set("cloud", "degraded")  # configured but this call fell back
    return {"digest": digest.model_dump()}
