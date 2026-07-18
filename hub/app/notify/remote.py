"""Push to the caregiver phone via ntfy (D-005 relay, D-006 self-hosted LAN
server, D-008 alert-v1 flow): alert facts → `<url>/<topic>`.

The notification IS the incident console entry point (D-008): tapping it opens
the SAATHI PWA (`Click`), and an `Acknowledge` action button POSTs the ack to
the hub straight from the lock screen — no app needed. While an L3 alert stays
unacknowledged the hub RE-PAGES every REMOTE_NOTIFY_REPEAT_S (server-side
insistence — wakes a sleeper regardless of client settings), capped at
REMOTE_NOTIFY_MAX_PAGES total notifications. Re-pages carry the LATEST alert
text, so page 2+ ships the LLM-written message.

OFF by default — main.py only starts this when both REMOTE_NOTIFY_URL and
REMOTE_NOTIFY_TOPIC are set (§15 empty defaults). Every failure is log-only
(§16) so the emergency loop (sensor→fusion→TTS→WS) never notices. stdlib
urllib on purpose: no new dependency (§27 hard rule).
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request

from app.bus import EventBus
from app.config import Settings
from app.domain import Alert

log = logging.getLogger("notify")

_TOPICS = ("alert.created", "alert.updated")
# Deliberately duplicated from fusion/alerts.py: importing fusion here would
# couple an optional bridge into the emergency loop's module graph.
_ACTIVE_STATES = ("OPEN", "ANNOUNCED", "ESCALATED")
_TRACK_CAP = 100  # alert ids remembered; demo-scale, pruned oldest-first


def build_request(settings: Settings, alert: Alert, page: int = 1) -> urllib.request.Request:
    """D-005 allowlist: id, kind, level, title, message — nothing else ever.
    ntfy reads the POST body as notification text and X-* headers as metadata.
    D-008: Click opens the PWA; the http action acks from the lock screen.
    Only page 1 is max priority: the client-side insistent alarm ("alert until
    viewed") applies to max ONLY, so re-pages nudge without stacking extra
    eternal ringers (live finding 2026-07-12: 3 stacked pages = 3 alarms)."""
    title = ("[SYNTHETIC] " if alert.synthetic else "") + alert.title
    if page > 1:
        title = f"(reminder {page}) {title}"
    url = f"{settings.remote_notify_url.rstrip('/')}/{settings.remote_notify_topic}"
    base = f"http://{settings.hub_lan_ip}:{settings.http_port}"
    app_url = f"{base}/app"
    ack_url = f"{base}/api/alerts/{alert.id}/ack"
    return urllib.request.Request(
        url,
        data=f"{alert.message}\n[{alert.id}] {alert.kind} L{alert.level}".encode(),
        headers={
            # headers travel latin-1; body stays full UTF-8
            "X-Title": title.encode("ascii", "ignore").decode(),
            "X-Priority": "urgent" if page == 1 else "high",
            "X-Tags": "rotating_light",
            "X-Click": app_url,
            "X-Actions": (
                f"http, Acknowledge, {ack_url}, method=POST, clear=true; "
                f"view, Open SAATHI, {app_url}"
            ),
        },
        method="POST",
    )


def _post(req: urllib.request.Request, timeout_s: float) -> int:
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.status


class RemoteNotifier:
    """Bus → ntfy bridge; one pump per topic (same shape as WsBroadcaster),
    plus one named re-page task per unacknowledged L3 alert (D-008)."""

    def __init__(self, bus: EventBus, settings: Settings) -> None:
        self._bus = bus
        self._settings = settings
        self._tasks: list[asyncio.Task] = []
        self._latest: dict[str, Alert] = {}  # id -> newest Alert seen (bounded)
        self._pages: dict[str, int] = {}     # id -> notifications sent so far
        self._repagers: dict[str, asyncio.Task] = {}  # id -> notify-repage task

    async def start(self) -> None:
        for topic in _TOPICS:
            stream = self._bus.subscribe(topic)
            self._tasks.append(
                asyncio.create_task(self._pump(stream), name=f"notify-{topic}")
            )
        log.info(
            "remote notify up url=%s topic=%s repage=%ss max_pages=%s",
            self._settings.remote_notify_url,
            self._settings.remote_notify_topic,
            self._settings.remote_notify_repeat_s,
            self._settings.remote_notify_max_pages,
        )

    async def stop(self) -> None:
        tasks = [*self._tasks, *self._repagers.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._repagers.clear()
        log.info("remote notify stopped")

    async def _pump(self, stream) -> None:
        async for payload in stream:
            try:
                if isinstance(payload, Alert) and self._track(payload):
                    await self._send(payload)
            except Exception:
                log.exception("remote notify dispatch failed")  # §16: pump never dies

    def _track(self, alert: Alert) -> bool:
        """Bookkeeping (synchronous — the created/updated pumps can't interleave
        it); returns True when this event deserves the FIRST page. Later
        alert.updated events only refresh _latest — the re-page timer decides
        when to send again."""
        self._latest[alert.id] = alert
        while len(self._latest) > _TRACK_CAP:
            del self._latest[next(iter(self._latest))]

        if alert.state not in _ACTIVE_STATES:
            self._cancel_repager(alert.id)  # acked/resolved: page storm ends
            return False
        if alert.level >= 3 and alert.id not in self._pages:
            self._pages[alert.id] = 0
            while len(self._pages) > _TRACK_CAP:
                del self._pages[next(iter(self._pages))]
            self._start_repager(alert.id)
            return True
        return False

    # --- re-paging (D-008: server-side insistence until ack) ---

    def _start_repager(self, alert_id: str) -> None:
        if self._settings.remote_notify_repeat_s <= 0 or alert_id in self._repagers:
            return
        task = asyncio.create_task(
            self._repage_loop(alert_id), name=f"notify-repage-{alert_id}"
        )
        self._repagers[alert_id] = task
        task.add_done_callback(lambda t: self._forget_repager(alert_id, t))

    def _forget_repager(self, alert_id: str, task: asyncio.Task) -> None:
        if self._repagers.get(alert_id) is task:
            del self._repagers[alert_id]

    def _cancel_repager(self, alert_id: str) -> None:
        task = self._repagers.pop(alert_id, None)
        if task is not None:
            task.cancel()
            log.info("notify-repage-%s cancelled (alert no longer active)", alert_id)

    async def _repage_loop(self, alert_id: str) -> None:
        while True:
            await asyncio.sleep(self._settings.remote_notify_repeat_s)
            alert = self._latest.get(alert_id)
            if alert is None or alert.state not in _ACTIVE_STATES:
                return
            if self._pages.get(alert_id, 0) >= self._settings.remote_notify_max_pages:
                log.info("notify-repage-%s cap reached (%s pages) — stopping",
                         alert_id, self._pages.get(alert_id))
                return
            await self._send(alert)  # latest text: page 2+ carries the LLM message

    async def _send(self, alert: Alert) -> None:
        # counted before the await: a failed push is NOT retried by this path
        # (log-only §16) — the NEXT re-page tick is the retry mechanism
        self._pages[alert.id] = self._pages.get(alert.id, 0) + 1
        req = build_request(self._settings, alert, page=self._pages[alert.id])
        start = time.perf_counter()
        try:
            status = await asyncio.to_thread(
                _post, req, self._settings.remote_notify_timeout_s
            )
        except Exception as e:
            log.warning("remote notify failed alert=%s err=%s", alert.id, e)
            return
        ms = (time.perf_counter() - start) * 1000
        log.info("remote notify sent alert=%s page=%s status=%s ms=%.0f",
                 alert.id, self._pages[alert.id], status, ms)
