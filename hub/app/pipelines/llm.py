"""LLM pipeline (§6.11/§11.4): OpenAI-compatible chat against local llama.cpp.

Two halves:
- `LlmClient` — `write_alert()`/`summarize()` with a hard timeout, one retry,
  strict output validation, and template fallback (returns None = caller keeps
  the template). Sequential calls only (3B model, small box): a lock plus a
  pending cap of 2 — older work is dropped, only the latest matters (§11.4).
- `LlmUpgrader` — bus bridge (same shape as RemoteNotifier): watches
  alert.created/alert.updated for ACTIVE template alerts at L3, asks the client
  for better text, and applies it through the injected `update` callback
  (fusion's AlertManager — pipelines never mutate alerts directly). The
  resulting alert.updated rides the existing WS push untouched.

The emergency path NEVER blocks on any of this: alerts ship with template text
instantly; everything here is async and log-only on failure (§16).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Awaitable, Callable

import httpx

from app.bus import EventBus
from app.config import Settings
from app.domain import Alert

log = logging.getLogger("llm")

# §11.4 frozen prompt strings
SYSTEM_ALERT = (
    "You write one short, calm, concrete alert message (≤60 words) for a "
    "family caregiver of an elderly person, based only on the JSON facts given. "
    "No speculation, no medical advice, no dramatization. Plain English."
)
SYSTEM_DIGEST = (
    "You are SAATHI, a home guardian. Given today's event log (JSON), write a "
    "5-8 sentence caregiver digest: overall status first, notable events with "
    "times, gentle observations (activity level vs. yesterday if data present). "
    "Honest about false alarms. No advice beyond 'consider checking'."
)

_ACTIVE_STATES = ("OPEN", "ANNOUNCED", "ESCALATED")  # duplicated on purpose, see notify/remote.py
_ATTEMPT_CAP = 100
_MAX_PENDING = 2  # §11.4: queue depth 2, older requests dropped
_DIGEST_TIMEOUT_S = 30.0  # route-5 summarize only; alert upgrades keep llm_timeout_s


def _validate(text: str | None, max_chars: int) -> str | None:
    """§11.4 never trust output: collapse to a single paragraph, strip markdown
    decoration (models love "**Alert:**" — the PWA renders plain text), cap
    length, reject empty → caller falls back to the template."""
    if not text:
        return None
    cleaned = " ".join(re.sub(r"[*`]+|^#+\s*", "", text).split())
    if not cleaned:
        return None
    return cleaned[:max_chars]


class LlmClient:
    """OPTIONAL subsystem: probe failure just means templates (§16)."""

    def __init__(
        self,
        settings: Settings,
        on_health: Callable[[str], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._on_health = on_health or (lambda status: None)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._pending = 0
        self._probe_task: asyncio.Task | None = None
        self.healthy = False

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._settings.llm_base,
            timeout=self._settings.llm_timeout_s,
            transport=self._transport,
        )
        # reachability probe runs in the BACKGROUND: on this Windows box a dead
        # port times out (~2 s) instead of refusing, and startup must not pay
        # that. healthy stays False until the probe (or any real call) says up.
        self._probe_task = asyncio.create_task(self._probe(), name="llm-probe")
        log.info("llm up base=%s model=%s (probing)",
                 self._settings.llm_base, self._settings.llm_model)

    async def _probe(self) -> None:
        try:
            resp = await self._client.get("/models", timeout=2.0)
            self._set_health(resp.status_code == 200)
        except httpx.HTTPError:
            self._set_health(False)
        log.info("llm probe healthy=%s", self.healthy)

    async def stop(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            await asyncio.gather(self._probe_task, return_exceptions=True)
            self._probe_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        log.info("llm stopped")

    def _set_health(self, ok: bool) -> None:
        self.healthy = ok
        self._on_health("up" if ok else "down")

    # --- public calls (both: None = use the template, §6.11) ---

    async def write_alert(self, alert: Alert) -> str | None:
        facts = {
            "kind": alert.kind,
            "level": alert.level,
            "state": alert.state,
            "title": alert.title,
            "time": time.strftime("%H:%M", time.localtime(alert.created_ts)),
            "sensor_facts": alert.facts,
        }
        text = await self._chat(SYSTEM_ALERT, json.dumps(facts), max_tokens=120)
        return _validate(text, max_chars=300)

    async def summarize(self, events: list[dict]) -> str | None:
        # On-demand digest (route 5), NOT the emergency upgrade path: a 5-8
        # sentence answer decodes for longer than llm_timeout_s allows (the 6 s
        # default is tuned so alert upgrades fail fast to templates, §6.11).
        # Non-streaming llama-server answers only when done, so the digest gets
        # its own budget; measured ~8-12 s on the event PC's CPU EP.
        text = await self._chat(SYSTEM_DIGEST, json.dumps(events), max_tokens=256,
                                timeout_s=_DIGEST_TIMEOUT_S)
        return _validate(text, max_chars=1200)

    # --- transport ---

    async def _chat(self, system: str, user: str, max_tokens: int,
                    timeout_s: float | None = None) -> str | None:
        if self._client is None:
            return None
        if self._pending >= _MAX_PENDING:
            log.warning("llm queue full — request dropped (§11.4)")
            return None
        self._pending += 1
        try:
            async with self._lock:  # sequential calls only (§11.4)
                for attempt in (1, 2):  # hard timeout + one retry (§6.11)
                    start = time.perf_counter()
                    try:
                        resp = await self._client.post(
                            "/chat/completions",
                            json={
                                "model": self._settings.llm_model,
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": user},
                                ],
                                "temperature": 0.3,
                                "max_tokens": max_tokens,
                            },
                            # httpx trap: timeout=None means NO timeout, not the
                            # client default — the sentinel keeps llm_timeout_s
                            timeout=(timeout_s if timeout_s is not None
                                     else httpx.USE_CLIENT_DEFAULT),
                        )
                        resp.raise_for_status()
                        text = resp.json()["choices"][0]["message"]["content"]
                        ms = (time.perf_counter() - start) * 1000
                        log.info("llm call url=%s status=%s ms=%.0f",
                                 resp.request.url, resp.status_code, ms)
                        self._set_health(True)
                        return text
                    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                        log.warning("llm call failed attempt=%s err=%s", attempt, e)
                self._set_health(False)
                return None
        finally:
            self._pending -= 1


class LlmUpgrader:
    """alert bus → LLM text upgrade for ACTIVE L3 template alerts, once per id."""

    def __init__(
        self,
        bus: EventBus,
        client: LlmClient,
        update: Callable[[str, str, str], Awaitable[None] | None],
    ) -> None:
        self._bus = bus
        self._client = client
        self._update = update  # (alert_id, message, engine) — AlertManager.update_message
        self._tasks: list[asyncio.Task] = []
        self._attempted: dict[str, None] = {}  # insertion-ordered id set

    async def start(self) -> None:
        for topic in ("alert.created", "alert.updated"):
            stream = self._bus.subscribe(topic)
            self._tasks.append(
                asyncio.create_task(self._pump(stream), name=f"llm-{topic}")
            )
        log.info("llm upgrader up")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("llm upgrader stopped")

    async def _pump(self, stream) -> None:
        async for payload in stream:
            try:
                if isinstance(payload, Alert) and self._should_upgrade(payload):
                    self._attempted[payload.id] = None
                    while len(self._attempted) > _ATTEMPT_CAP:
                        del self._attempted[next(iter(self._attempted))]
                    await self._upgrade(payload)
            except Exception:
                log.exception("llm upgrade dispatch failed")  # §16: pump never dies

    def _should_upgrade(self, alert: Alert) -> bool:
        # L3-active mirrors the escalate hook in §13; message_engine guard means
        # our own upgrade broadcast can never re-trigger us
        return (
            alert.level >= 3
            and alert.state in _ACTIVE_STATES
            and alert.message_engine == "template"
            and alert.id not in self._attempted
        )

    async def _upgrade(self, alert: Alert) -> None:
        text = await self._client.write_alert(alert)
        if text is None:
            log.info("llm upgrade skipped alert=%s (template stays)", alert.id)
            return
        result = self._update(alert.id, text, "local-llm")
        if asyncio.iscoroutine(result):
            await result
        log.info("llm upgraded alert=%s chars=%s", alert.id, len(text))
