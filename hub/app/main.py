"""App factory + logging setup (§8).

Entry sequence: `config.load()` → `setup_logging()` → `create_app()` → uvicorn.
(The thin `run_hub.py` launcher lives at hub/ root — outside this session's
ownership area — so until it lands, run:
`hub/venv/Scripts/python -m uvicorn app.main:create_app --factory --app-dir hub`.)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from rich.logging import RichHandler
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.demo import router as demo_router
from app.api.routes import router
from app.api.ws import WsBroadcaster
from app.api.ws import router as ws_router
from app.bus import EventBus
from app.config import Settings, load
from app.fusion.engine import FusionEngine
from app.ingest.mqtt_ingest import MqttIngest
from app.notify.remote import RemoteNotifier
from app.pipelines.asr import AsrPipeline
from app.pipelines.llm import LlmClient, LlmUpgrader
from app.pipelines.tts import TtsPipeline
from app.storage.db import Database
from app.storage.repo import Repo

log = logging.getLogger("hub")
api_log = logging.getLogger("api")

# §17 format: HH:MM:SS.mmm LEVEL [subsystem] message key=val…
# (subsystem = logger name; writers append key=val pairs in the message)
LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(settings: Settings, level: int = logging.INFO) -> None:
    """§17: rich console + one rotating file logs/hub.log (10 MB × 3).

    Idempotent: replaces only handlers it installed (tagged), so pytest's own
    capture handlers survive repeated calls.
    """
    settings.log_path.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_saathi", False)]:
        root.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        settings.log_path / "hub.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    console = RichHandler(show_path=False, log_time_format=DATE_FORMAT)
    console.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    for handler in (file_handler, console):
        handler._saathi = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)


# §7 health-strip chips + the Phase-1 spine. Everything starts "down" and each
# subsystem flips its own entry when its phase wires it in — the map never claims
# more than what actually runs (§26 honesty rule).
SUBSYSTEM_DEFAULTS = {
    "storage": "down",
    "bus": "down",
    "node": "down",
    "broker": "down",
    "vision": "down",
    "asr": "down",
    "llm": "down",
    "cloud": "down",
}


class HealthRegistry:
    """In-memory subsystem status map (§14 — ephemeral by nature)."""

    def __init__(self) -> None:
        self._status = dict(SUBSYSTEM_DEFAULTS)
        self.internet = False  # flipped by the §8 connectivity probe task

    def set(self, name: str, status: str) -> None:
        self._status[name] = status

    def snapshot(self) -> dict[str, str]:
        return dict(self._status)


def _local_ipv4s() -> set[str]:
    """Best-effort list of this machine's IPv4 addresses (no traffic sent)."""
    ips: set[str] = set()
    try:
        ips.update(ip for ip in socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:  # default-route interface (UDP connect sends nothing)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return ips


def _internet_reachable(timeout_s: float = 2.0) -> bool:
    """One cheap outbound TCP probe (DNS port). No data sent — offline-first
    means this may fail forever and nothing downstream cares (§16)."""
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=timeout_s):
            return True
    except OSError:
        return False


async def _connectivity_probe(health: HealthRegistry, bus: EventBus,
                              interval_s: float = 15.0) -> None:
    """§8 background task. Sleep-FIRST: startup (and tests) always begin
    `internet:false` deterministically; the truth arrives one interval later."""
    while True:
        await asyncio.sleep(interval_s)
        online = await asyncio.to_thread(_internet_reachable)
        if online != health.internet:
            health.internet = online
            bus.publish("system.health", {
                "subsystem": "internet",
                "status": "up" if online else "down",
                "ts": time.time(),
            })


@asynccontextmanager
async def lifespan(app: FastAPI):
    """§8 startup order (Phase-1 slice): storage → bus; shutdown in reverse.
    REQUIRED subsystems abort startup on failure; later phases append their
    OPTIONAL subsystems here (fail = log ERROR + continue degraded)."""
    settings: Settings = app.state.settings
    health: HealthRegistry = app.state.health

    try:
        db = Database(settings.db_file)
        db.start()
    except Exception:
        log.exception("REQUIRED subsystem storage failed — aborting startup (§8)")
        raise
    health.set("storage", "up")
    app.state.db = db
    app.state.repo = Repo(db)

    app.state.bus = EventBus()
    health.set("bus", "up")

    # Phase 4: TTS is OPTIONAL (§16 — worst case the announcement is a log
    # line); fusion publishes speak.request either way and never waits
    tts = None
    try:
        tts = TtsPipeline(app.state.bus)
        await tts.start()
    except Exception:
        log.exception("OPTIONAL subsystem tts failed — continuing degraded (§8)")
        tts = None
    app.state.tts = tts

    # Phase 4: LLM client probe (§8). MOCK_LLM=1 or a dead server = templates
    # only — the alert path is identical either way (§6.11)
    llm = None
    if settings.mock_llm:
        log.info("llm mocked (MOCK_LLM=1) — template messages only (§12.6)")
    else:
        try:
            llm = LlmClient(settings, on_health=lambda s: health.set("llm", s))
            await llm.start()
        except Exception:
            log.exception("OPTIONAL subsystem llm failed — continuing degraded (§8)")
            llm = None
    app.state.llm = llm

    # Phase 2: fusion is part of the emergency loop — a startup failure aborts (§16)
    fusion = FusionEngine(app.state.bus, app.state.repo, settings)
    await fusion.start()
    app.state.fusion = fusion

    # Phase 4: async message upgrade (§13) — text improves in place when the
    # LLM answers; mutation goes through AlertManager so persist+broadcast hold
    upgrader = None
    if llm is not None:
        upgrader = LlmUpgrader(app.state.bus, llm, fusion.alerts.update_message)
        await upgrader.start()
    app.state.llm_upgrader = upgrader

    # Phase 2: ingest is resilient by design — a dead broker just means the paho
    # thread retries with 1→8 s backoff while broker/node chips stay "down" (§8)
    ingest = MqttIngest(app.state.bus, app.state.repo, settings, health)
    ingest.start()
    app.state.ingest = ingest

    # Phase 5: ASR is OPTIONAL (§8/§16). MOCK_ASR=1 = keyword events come from
    # the demo endpoint (§12.6); the real pipeline runs degraded (chip "down")
    # until A5 stages the whisper model. Emergency loop never notices either way.
    asr = None
    if settings.mock_asr:
        log.info("asr mocked (MOCK_ASR=1) — keyword events via demo endpoint (§12.6)")
    else:
        try:
            asr = AsrPipeline(app.state.bus, settings,
                              on_health=lambda s: health.set("asr", s))
            await asr.start()
        except Exception:
            log.exception("OPTIONAL subsystem asr failed — continuing degraded (§8)")
            asr = None
    app.state.asr = asr

    probe = asyncio.create_task(
        _connectivity_probe(health, app.state.bus), name="connectivity-probe"
    )

    # Phase 3b (D-005): OPTIONAL remote push — inert unless both REMOTE_NOTIFY_*
    # keys are set; a startup failure logs and continues degraded (§8)
    notifier = None
    if settings.remote_notify_configured:
        # D-008: the notification's Click/Acknowledge buttons embed HUB_LAN_IP —
        # a stale value (network changed) means the buttons silently do nothing
        local = _local_ipv4s()
        if local and settings.hub_lan_ip not in local:
            log.warning(
                "HUB_LAN_IP=%s is not an address of this machine (%s) — "
                "notification Click/Acknowledge buttons will be DEAD; fix .env",
                settings.hub_lan_ip, ",".join(sorted(local)),
            )
        try:
            notifier = RemoteNotifier(app.state.bus, settings)
            await notifier.start()
        except Exception:
            log.exception("OPTIONAL subsystem notify failed — continuing degraded (§8)")
            notifier = None
    app.state.notifier = notifier

    # §8: WS managers start LAST — every earlier subsystem already publishes to
    # the bus, so the bridge sees a complete picture from its first message
    ws = WsBroadcaster(app)
    await ws.start()
    app.state.ws = ws

    if not settings.mock_cloud and not settings.cloud_configured:
        log.warning("cloud trio unset — cloud engine disabled (§15)")
    elif not settings.mock_cloud:
        # trio present: yellow until a digest actually returns from the cloud —
        # "up" is earned by a real call (routes.py), never claimed (§26)
        health.set("cloud", "degraded")
    log.info(
        "hub up http_port=%s db=%s ep=%s", settings.http_port, settings.db_file, settings.ep
    )
    yield

    await ws.stop()
    if notifier is not None:
        await notifier.stop()
    probe.cancel()
    await asyncio.gather(probe, return_exceptions=True)
    if asr is not None:
        await asr.stop()
    await ingest.stop()
    if upgrader is not None:
        await upgrader.stop()
    await fusion.stop()
    if llm is not None:
        await llm.stop()
    if tts is not None:
        await tts.stop()
    health.set("bus", "down")
    health.set("storage", "down")
    db.stop()  # FIFO sentinel: drains queued writes before closing
    log.info("hub down")


def _error_json(request: Request, status: int, code: str, message: str) -> JSONResponse:
    """§16 standard error schema — every non-2xx body has exactly this shape."""
    return JSONResponse(
        status_code=status,
        content={"error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", ""),
        }},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """§8 app factory (also uvicorn's --factory target)."""
    app = FastAPI(title="SAATHI hub", lifespan=lifespan)
    app.state.settings = settings or load()
    app.state.health = HealthRegistry()
    app.include_router(router)
    app.include_router(demo_router)
    app.include_router(ws_router)

    # §7 frontends, served same-origin (deliberately no CORS, §8)
    static = Path(__file__).resolve().parents[1] / "static"
    app.mount("/app", StaticFiles(directory=static / "app", html=True), name="pwa")
    app.mount("/dash", StaticFiles(directory=static / "dash", html=True), name="dash")

    @app.middleware("http")
    async def request_id_and_timing(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request.state.request_id
        api_log.info("[%s] %s %s status=%s ms=%.1f", request.state.request_id,
                     request.method, request.url.path, response.status_code, ms)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        # routes raise detail={"code","message"}; framework 404/405 pass a string
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code", f"HTTP_{exc.status_code}")
            message = exc.detail.get("message", "")
        else:
            code, message = f"HTTP_{exc.status_code}", str(exc.detail)
        return _error_json(request, exc.status_code, code, message)

    @app.exception_handler(RequestValidationError)
    async def validation_exception(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        message = f"{where}: {first.get('msg', 'invalid input')}" if where else "invalid input"
        return _error_json(request, 422, "VALIDATION_ERROR", message)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        # §16: stack trace to log only, never to the client
        log.exception("unhandled error req=%s", getattr(request.state, "request_id", ""))
        return _error_json(request, 500, "INTERNAL_ERROR", "internal error")

    return app
