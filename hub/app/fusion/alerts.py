"""Alert state machine (§6.6): OPEN → ANNOUNCED → (ACKED | ESCALATED) → RESOLVED,
plus FALSE_ALARM. Every transition is persisted AND broadcast — the PWA and the
DB can never disagree. Ack is idempotent (caregiver double-tap safe, J1.6).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from app.bus import EventBus
from app.domain import Alert, AlertKind, AlertState
from app.storage.repo import Repo

log = logging.getLogger("fusion")

# Legal transitions (§6.6 diagram). Anything else raises IllegalTransition.
_LEGAL: dict[AlertState, set[AlertState]] = {
    "OPEN": {"ANNOUNCED", "ESCALATED", "ACKED", "RESOLVED", "FALSE_ALARM"},
    "ANNOUNCED": {"ESCALATED", "ACKED", "RESOLVED", "FALSE_ALARM"},
    "ESCALATED": {"ACKED", "RESOLVED"},
    "ACKED": {"RESOLVED"},
    "RESOLVED": set(),
    "FALSE_ALARM": set(),
}

ACTIVE_STATES: tuple[AlertState, ...] = ("OPEN", "ANNOUNCED", "ESCALATED")
FINAL_STATES: tuple[AlertState, ...] = ("RESOLVED", "FALSE_ALARM")


class IllegalTransition(Exception):
    pass


class AlertManager:
    """Owns every alert mutation. Engine and (later) the ack route go through here,
    so persistence + broadcast can never be skipped."""

    def __init__(self, repo: Repo, bus: EventBus) -> None:
        self._repo = repo
        self._bus = bus
        self._active: dict[str, Alert] = {}  # id -> alert in an ACTIVE state

    # --- lifecycle ---

    def create(
        self,
        *,
        kind: AlertKind,
        level: int,
        title: str,
        message: str,
        facts: dict[str, Any] | None = None,
        synthetic: bool = False,
    ) -> Alert:
        now = time.time()
        alert = Alert(
            id=f"a-{uuid.uuid4().hex[:4]}",
            kind=kind,
            level=level,
            state="OPEN",
            title=title,
            message=message,
            facts=facts or {},
            synthetic=synthetic,
            created_ts=now,
            updated_ts=now,
        )
        self._active[alert.id] = alert
        self._repo.upsert_alert(alert)
        self._bus.publish("alert.created", alert)
        log.info("alert %s OPEN kind=%s level=%s", alert.id, kind, level)
        return alert

    def transition(self, alert: Alert, new_state: AlertState, **updates: Any) -> Alert:
        if new_state not in _LEGAL[alert.state]:
            raise IllegalTransition(f"{alert.id}: {alert.state} → {new_state}")
        changed = alert.model_copy(
            update={"state": new_state, "updated_ts": time.time(), **updates}
        )
        if new_state in FINAL_STATES:
            self._active.pop(changed.id, None)
        else:
            self._active[changed.id] = changed
        self._repo.upsert_alert(changed)
        self._bus.publish("alert.updated", changed)
        log.info("alert %s %s → %s level=%s", changed.id, alert.state, new_state, changed.level)
        return changed

    # --- named transitions the engine/rules use ---

    def announce(self, alert: Alert) -> Alert:
        return self.transition(alert, "ANNOUNCED")

    def escalate(self, alert: Alert, facts: dict[str, Any] | None = None) -> Alert:
        return self.transition(
            alert, "ESCALATED", level=3, facts={**alert.facts, **(facts or {})}
        )

    def resolve(self, alert: Alert) -> Alert:
        return self.transition(alert, "RESOLVED")

    def false_alarm(self, alert: Alert) -> Alert:
        return self.transition(alert, "FALSE_ALARM")

    def ack(self, alert: Alert) -> Alert:
        """J1.6 + §13: ack lands the alert in ACKED then RESOLVED (both persisted,
        both broadcast). Idempotent: acking a finished alert returns it unchanged."""
        if alert.state in FINAL_STATES or alert.state == "ACKED":
            return alert
        return self.resolve(self.transition(alert, "ACKED"))

    def update_message(self, alert_id: str, message: str, engine: str) -> Alert | None:
        """§6.11 async LLM upgrade: better text in place, NOT a state transition.
        The broadcast alert.updated rides the existing WS push (§13). Skipped if
        the alert reached a final state while the LLM was thinking — the moment
        has passed and history keeps the text it was delivered with."""
        alert = self.get(alert_id)
        if alert is None or alert.state in FINAL_STATES:
            return None
        changed = alert.model_copy(
            update={"message": message, "message_engine": engine, "updated_ts": time.time()}
        )
        self._active[changed.id] = changed
        self._repo.upsert_alert(changed)
        self._bus.publish("alert.updated", changed)
        log.info("alert %s message upgraded engine=%s", changed.id, engine)
        return changed

    # --- lookups ---

    def active(self) -> list[Alert]:
        return list(self._active.values())

    def active_by_kind(self, kind: AlertKind) -> Alert | None:
        return next((a for a in self._active.values() if a.kind == kind), None)

    def get(self, alert_id: str) -> Alert | None:
        return self._active.get(alert_id) or self._repo.get_alert(alert_id)

    def restore(self, alert: Alert) -> None:
        """§14 restart semantics: re-adopt a persisted ACTIVE alert after a hub
        restart (no broadcast — nothing changed, we just remembered it)."""
        if alert.state in ACTIVE_STATES:
            self._active[alert.id] = alert
