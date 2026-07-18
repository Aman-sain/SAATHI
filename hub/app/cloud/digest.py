"""Cloud AI 100 digest adapter (Phase 8, A9): Cirrascale Inference Cloud —
OpenAI-compatible, partner free credits (STATUS 2026-07-15 partner-call intel;
CONTRACTS.md route 5). Fallback chain: cloud -> local LLM -> deterministic
template. OPTIONAL subsystem: never sits on the emergency path (route 5 is
on-demand, invoked by the PWA/a cron, never by fusion), and every tier here is
log-only on failure (§16) — `generate_digest()` always returns a Digest, never
raises for a reachability/timeout failure.

Ownership: this file is Aman's (hub/app/cloud/** only). It does NOT register a
FastAPI route or touch hub/app/main.py/api/** — see the handoff note in
DevopsTasks/report/2026-07-16-session-log.md for exactly what a route handler
needs to call.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from app.config import Settings
from app.domain import Digest
from app.pipelines.llm import SYSTEM_DIGEST, LlmClient

log = logging.getLogger("cloud")

_MAX_CHARS = 1200  # matches LlmClient.summarize's own cap (pipelines/llm.py), kept consistent


def _validate(text: str | None) -> str | None:
    """Same non-trust posture as pipelines/llm.py's _validate: collapse
    whitespace, cap length, reject empty -> caller falls back a tier."""
    if not text:
        return None
    cleaned = " ".join(text.split())
    return cleaned[:_MAX_CHARS] if cleaned else None


async def _call_cloud(
    settings: Settings,
    events: list[dict],
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """One httpx call + one retry against the OpenAI-compatible Cirrascale
    endpoint. Returns None on ANY failure or when the tier is intentionally
    off (MOCK_CLOUD=1 default, or the CLOUD_* placeholder trio unset, §15) —
    never raises, so a cloud outage can never block digest generation."""
    if settings.mock_cloud or not settings.cloud_configured:
        return None
    async with httpx.AsyncClient(
        base_url=settings.cloud_api_base,
        timeout=settings.cloud_timeout_s,
        transport=transport,
        headers={"Authorization": f"Bearer {settings.cloud_api_key}"},
    ) as client:
        for attempt in (1, 2):  # hard timeout + one retry, mirrors pipelines/llm.py
            start = time.perf_counter()
            try:
                resp = await client.post(
                    "/chat/completions",
                    json={
                        "model": settings.cloud_model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_DIGEST},
                            {"role": "user", "content": json.dumps(events)},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 256,
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                ms = (time.perf_counter() - start) * 1000
                log.info("cloud call url=%s status=%s ms=%.0f",
                          resp.request.url, resp.status_code, ms)
                return text
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                log.warning("cloud call failed attempt=%s err=%s", attempt, e)
    return None


def _template_digest(date: str, events: list[dict]) -> str:
    """Deterministic, LLM-free last resort (§16: the emergency/optional-feature
    posture applies here too) — must always succeed, no network, no model."""
    if not events:
        return f"No events recorded for {date}."
    counts: dict[str, int] = {}
    for e in events:
        kind = str(e.get("kind", "event"))
        counts[kind] = counts.get(kind, 0) + 1
    parts = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    return f"{len(events)} event(s) on {date}: {parts}."


async def generate_digest(
    settings: Settings,
    events: list[dict],
    date: str,
    local_llm: LlmClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Digest:
    """The one function a route handler needs (CONTRACTS #2 route 5):

        digest = await generate_digest(settings, events, date, local_llm=app.state.llm_client)
        return {"digest": digest.model_dump()}

    `events` is the day's event log (list of dicts — same shape fusion already
    produces for repo.list_events()). `local_llm` should be the SAME LlmClient
    instance fusion/the LLM upgrader already runs (do not construct a second
    one) — passing None just skips straight to the template tier.

    Deliberately request-scoped (a fresh httpx.AsyncClient per call, closed
    immediately) rather than a persistent client like LlmClient: digest
    generation is on-demand (once/day via the route, or a cron), not a
    continuous background subscriber, so there is no lifecycle to wire into
    main.py's startup/shutdown — nothing beyond the one route-handler call
    above is needed to use this.
    """
    text = _validate(await _call_cloud(settings, events, transport=transport))
    if text is not None:
        return Digest(date=date, text=text, engine="cloud-ai-100", created_ts=time.time())

    if local_llm is not None:
        text = await local_llm.summarize(events)
        if text is not None:
            return Digest(date=date, text=text, engine="local-llm", created_ts=time.time())

    return Digest(
        date=date,
        text=_template_digest(date, events),
        engine="template",
        created_ts=time.time(),
    )
