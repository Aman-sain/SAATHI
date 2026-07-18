"""Fusion engine (§6.4): the single consumer of sensor events. Owns FusionState,
applies the pure rules, executes the returned actions. ALL side effects flow out
via bus/storage — the engine never touches HTTP or hardware.

Timer discipline (§20 Phase-2 failure watch): every escalation timer is a NAMED
asyncio task `t-esc-{alert_id}`, tracked in one dict, cancelled on motion, on
resolve/ack, and on engine stop — timers can never leak.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from app.bus import EventBus
from app.config import Settings
from app.domain import ASREvent, NodeEvent, Telemetry
from app.fusion import rules
from app.fusion.alerts import ACTIVE_STATES, AlertManager, IllegalTransition
from app.fusion.rules import (
    Action, CancelTimer, EscalateAlert, OpenAlert, ResolveAlert, RuleConfig,
)
from app.storage.repo import Repo

log = logging.getLogger("fusion")

WINDOW_S = 120  # §6.4: rolling window per signal


class FusionEngine:
    def __init__(self, bus: EventBus, repo: Repo, settings: Settings) -> None:
        self._bus = bus
        self._repo = repo
        self._cfg = RuleConfig(
            gas_warn=settings.gas_warn,
            gas_crit=settings.gas_crit,
            escalate_seconds=settings.escalate_seconds,
        )
        self.alerts = AlertManager(repo, bus)
        self._repeat_s = settings.announce_repeat_seconds
        self._timers: dict[str, asyncio.Task] = {}  # alert_id -> named t-esc task
        self._announcers: dict[str, asyncio.Task] = {}  # alert_id -> named t-ann task
        self._consumers: list[asyncio.Task] = []
        # FusionState (§14: in-memory, rebuilt harmlessly on restart)
        self._gas_window: deque[tuple[float, float]] = deque()
        self._last_motion_ts: float | None = None

    # --- lifecycle ---

    async def start(self) -> None:
        self._restore_active_alerts()
        # subscribe BEFORE the consumer tasks run: events published from now on buffer
        for topic in ("node.event", "node.telemetry", "asr.event"):
            stream = self._bus.subscribe(topic)
            self._consumers.append(
                asyncio.create_task(self._consume(stream), name=f"fusion-{topic}")
            )
        log.info("fusion up rules=R-GAS,R-HELP thresholds warn=%s crit=%s esc=%ss",
                 self._cfg.gas_warn, self._cfg.gas_crit, self._cfg.escalate_seconds)

    async def stop(self) -> None:
        tasks = [*self._consumers, *self._timers.values(), *self._announcers.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._consumers.clear()
        self._timers.clear()
        self._announcers.clear()
        log.info("fusion stopped")

    def _restore_active_alerts(self) -> None:
        """§14: reload persisted ACTIVE alerts; any OPEN/ANNOUNCED older than its
        escalation window escalates immediately, younger ones get the remainder."""
        now = time.time()
        for alert in self._repo.list_active_alerts():
            self.alerts.restore(alert)
            self._start_announcer(alert.id)  # danger persists across restarts (D-007)
            if alert.state not in ("OPEN", "ANNOUNCED"):
                continue
            age = now - alert.created_ts
            remaining = self._cfg.escalate_seconds - age
            if remaining <= 0:
                log.info("restart: alert %s age=%.0fs past window — escalating now",
                         alert.id, age)
                self._escalate(alert.id, {"escalated_on": "restart"})
            else:
                self._start_timer(alert.id, remaining)

    # --- event loop ---

    async def _consume(self, stream) -> None:
        async for event in stream:
            try:
                self._handle(event)
            except Exception:
                log.exception("event dropped by engine")  # §16: fusion never dies

    def _handle(self, event: Telemetry | NodeEvent | ASREvent) -> None:
        now = time.time()
        if isinstance(event, Telemetry):
            self._gas_window.append((now, event.gas_norm))
            while self._gas_window and self._gas_window[0][0] < now - WINDOW_S:
                self._gas_window.popleft()
            if event.motion:
                self._last_motion_ts = now
        elif isinstance(event, NodeEvent) and event.type == "MOTION":
            self._last_motion_ts = now

        for action in rules.evaluate(self._rule_state(), event, self._cfg):
            self._apply(action, event)

    def _rule_state(self) -> rules.RuleState:
        last_motion_s = (
            time.time() - self._last_motion_ts if self._last_motion_ts else None
        )
        return rules.RuleState(
            active_gas=self.alerts.active_by_kind("GAS"),
            active_help=self.alerts.active_by_kind("HELP"),
            last_motion_s=last_motion_s,
        )

    # --- action execution ---

    def _apply(self, action: Action, event) -> None:
        if isinstance(action, OpenAlert):
            if action.kind == "GAS":
                log.info("R-GAS fired gas_norm=%s thr=%s",
                         action.facts.get("gas_norm"), self._cfg.gas_warn)
            else:  # §17: rule firings log the values that fired them
                log.info("R-HELP fired keyword=%s conf=%s",
                         action.facts.get("keyword"), action.facts.get("conf"))
            alert = self.alerts.create(
                kind=action.kind, level=action.level, title=action.title,
                message=action.message, facts=action.facts,
                # contract #8: a demo-injected event taints its alert end-to-end
                synthetic=getattr(event, "synthetic", False),
            )
            # J1.3/§13: announce = speak.request out, state machine → ANNOUNCED.
            # Empty phrase = no open one-shot (no rule emits one since
            # help_heard_hi was re-locked 2026-07-17; guard kept defensive) —
            # the D-007 announcer still repeats.
            if action.announce_phrase:
                self._bus.publish(
                    "speak.request",
                    {"phrase_id": action.announce_phrase, "alert_id": alert.id},
                )
            alert = self.alerts.announce(alert)
            self._start_announcer(alert.id)  # D-007: repeat while danger persists
            if action.escalate_after_s > 0:
                self._start_timer(alert.id, action.escalate_after_s)

        elif isinstance(action, EscalateAlert):
            self._escalate(action.alert_id, action.facts)

        elif isinstance(action, ResolveAlert):
            self._cancel_timer(action.alert_id, action.reason)
            self._cancel_announcer(action.alert_id)
            alert = self.alerts.get(action.alert_id)
            if alert and alert.state not in ("RESOLVED", "FALSE_ALARM"):
                log.info("R-GAS resolved %s reason=%s", alert.id, action.reason)
                self.alerts.resolve(alert)
                # D-012 Moment ④b: gas cleared on its own — reassure the elder
                # once. NO alert_id: resolve finalized the alert and tts drops
                # finalized-alert phrases (D-008), same pattern as the ack one-shot.
                phrase = rules.resolve_oneshot(alert.kind)
                if phrase:
                    self._bus.publish("speak.request", {"phrase_id": phrase})

        elif isinstance(action, CancelTimer):
            self._cancel_timer(action.alert_id, action.reason)

    def _escalate(self, alert_id: str, facts: dict) -> None:
        self._cancel_timer(alert_id, "escalating")
        alert = self.alerts.get(alert_id)
        if not alert or alert.state not in ("OPEN", "ANNOUNCED"):
            return
        try:
            escalated = self.alerts.escalate(alert, facts=facts)
        except IllegalTransition:
            log.warning("escalate raced a final state alert=%s", alert_id)
            return
        # D-007 escalation one-shot (truthful text since 2026-07-14: gas still
        # high + what to check — never claims a person was reached); the
        # announcer keeps repeating and picks the L3 phrase on its next tick
        self._bus.publish("speak.request", {
            "phrase_id": rules.escalation_oneshot(escalated.kind),
            "alert_id": escalated.id,
        })

    def ack(self, alert_id: str) -> object | None:
        """Phase 3's POST /api/alerts/{id}/ack lands here (J1.4c: ack clears timer)."""
        alert = self.alerts.get(alert_id)
        if alert is None:
            return None
        self._cancel_timer(alert_id, "acked")
        self._cancel_announcer(alert_id)
        was_active = alert.state in ACTIVE_STATES
        acked = self.alerts.ack(alert)
        phrase = rules.ack_oneshot(alert.kind) if was_active else None
        if phrase:
            # no alert_id on purpose: the ack just finalized this alert, and
            # tts drops phrases whose alert reached a final state (D-008)
            self._bus.publish("speak.request", {"phrase_id": phrase})
        return acked

    # --- named timers ---

    def _start_timer(self, alert_id: str, seconds: float) -> None:
        self._cancel_timer(alert_id, "rearmed")
        name = f"t-esc-{alert_id}"
        task = asyncio.create_task(self._escalate_later(alert_id, seconds), name=name)
        self._timers[alert_id] = task
        task.add_done_callback(lambda t: self._forget_timer(alert_id, t))
        log.info("%s armed seconds=%s", name, seconds)

    def _forget_timer(self, alert_id: str, task: asyncio.Task) -> None:
        if self._timers.get(alert_id) is task:
            del self._timers[alert_id]

    def _cancel_timer(self, alert_id: str, reason: str | None) -> None:
        task = self._timers.pop(alert_id, None)
        if task is not None:
            task.cancel()
            if reason:
                log.info("t-esc-%s cancelled reason=%s", alert_id, reason)

    async def _escalate_later(self, alert_id: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        # J1.5: timer fired — no motion, no ack, gas never cleared
        log.info("t-esc-%s fired after %ss — no motion, no ack", alert_id, seconds)
        self._escalate(alert_id, {"escalated_on": "timer"})

    # --- repeating announcements (D-007) ---

    def _start_announcer(self, alert_id: str) -> None:
        """Named task t-ann-{id}: re-announce every ANNOUNCE_REPEAT_SECONDS while
        the alert stays active. Motion does NOT stop it (gas is still high —
        J1.4a resolution or an ack is what ends the danger); 0 disables."""
        if self._repeat_s <= 0 or alert_id in self._announcers:
            return
        name = f"t-ann-{alert_id}"
        task = asyncio.create_task(self._announce_loop(alert_id), name=name)
        self._announcers[alert_id] = task
        task.add_done_callback(lambda t: self._forget_announcer(alert_id, t))
        log.info("%s armed every=%ss", name, self._repeat_s)

    def _forget_announcer(self, alert_id: str, task: asyncio.Task) -> None:
        if self._announcers.get(alert_id) is task:
            del self._announcers[alert_id]

    def _cancel_announcer(self, alert_id: str) -> None:
        task = self._announcers.pop(alert_id, None)
        if task is not None:
            task.cancel()
            log.info("t-ann-%s cancelled", alert_id)

    async def _announce_loop(self, alert_id: str) -> None:
        while True:
            await asyncio.sleep(self._repeat_s)
            alert = self.alerts.get(alert_id)
            if alert is None or alert.state not in ("OPEN", "ANNOUNCED", "ESCALATED"):
                return  # belt-and-braces: cancellation is the primary stop
            # phrase re-derived each tick — escalation upgrades it automatically
            self._bus.publish("speak.request", {
                "phrase_id": rules.repeat_phrase(alert.kind, alert.level),
                "alert_id": alert_id,
            })
