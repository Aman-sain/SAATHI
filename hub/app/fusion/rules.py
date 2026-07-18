"""Declarative rules (§6.5): pure `(state, event) -> list[Action]`. No I/O, no
clocks, no side effects — the engine executes the returned actions. Thresholds
come from config, never literals. Phase 2 shipped R-GAS (J1); Phase 5 adds
R-HELP plus the response window (an OK/help word against an active gas alert);
R-FALL/R-NOISE land with vision in Phase 6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from app.domain import Alert, AlertKind, ASREvent, NodeEvent, Telemetry

# --- actions the engine knows how to execute (§6.4) ---


@dataclass(frozen=True)
class OpenAlert:
    kind: AlertKind
    level: int
    title: str
    message: str
    facts: dict[str, Any] = field(default_factory=dict)
    announce_phrase: str = ""       # speak.request payload (TTS lands Phase 4)
    escalate_after_s: float = 0.0   # >0 = start the named escalation timer


@dataclass(frozen=True)
class EscalateAlert:
    alert_id: str
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolveAlert:
    alert_id: str
    reason: str


@dataclass(frozen=True)
class CancelTimer:
    alert_id: str
    reason: str


Action = Union[OpenAlert, EscalateAlert, ResolveAlert, CancelTimer]


@dataclass(frozen=True)
class RuleConfig:
    """The §15 threshold slice rules need — engine builds it from Settings."""

    gas_warn: float
    gas_crit: float
    escalate_seconds: float


@dataclass
class RuleState:
    """The read-only view of FusionState that rules see."""

    active_gas: Alert | None = None
    active_help: Alert | None = None
    last_motion_s: float | None = None  # seconds since last motion, None = never


# D-007 announcement phrases (WAV bank §11.5) — phrase CHOICE stays here in
# fusion (§5 placement). Voice-truth rule (scope lock 2026-07-14): the house
# voice talks to an elder who may be ALONE — it may state the danger, the
# elder's next action, or what SAATHI itself is verifiably doing; it never
# claims a third person was reached. D-012 exception (Ansh's call): the
# escalation one-shot names the phone alert — honest in the demo (remote-notify
# ON), overclaims if remote-notify is OFF. Canonical texts: docs/VOICE_PHRASES.md.
def repeat_phrase(kind: str, level: int) -> str:
    if kind == "GAS":
        return "gas_danger_hi" if level >= 3 else "gas_warning_hi"
    # HELP (Phase 5): periodic check-in. The help_heard_hi one-shot plays once
    # at open (re-locked 2026-07-17, D-013); repeats stay this prompt.
    return "are_you_ok_hi"  # also FALL/NOISE when Phase 6 lands


def escalation_oneshot(kind: str) -> str:
    # slot id is historical — since D-012 the WAV says a phone alert has gone
    # out and to attend to the gas leak (Moment ② of the gas voice story)
    return "alert_sent_hi"


def ack_oneshot(kind: str) -> str | None:
    # post-ack confirmation (locked 2026-07-14): alarm is stopping, stay in
    # fresh air until the gas clears. Text is gas-specific, so GAS only.
    return "all_ok_hi" if kind == "GAS" else None


def resolve_oneshot(kind: str) -> str | None:
    # D-012 Moment ④b: gas returned to normal on its own — reassure the elder
    # once ("situation safe, SAATHI is with you"). Gas-specific, so GAS only.
    return "all_clear_hi" if kind == "GAS" else None


# §6.11 template message shape — the alert never waits on an LLM (Phase 4 upgrades it).
def _gas_message(level: int, gas_norm: float, last_motion_s: float | None) -> str:
    motion = f"{int(last_motion_s)} s ago" if last_motion_s is not None else "not seen"
    return (
        f"[GAS] Level {level}. Gas level {gas_norm:.2f} above threshold; "
        f"last motion {motion}."
    )


def evaluate(
    state: RuleState, event: Telemetry | NodeEvent | ASREvent, cfg: RuleConfig
) -> list[Action]:
    """R-GAS (J1) + R-HELP (Phase 5). Unknown event kinds produce no actions."""
    if isinstance(event, NodeEvent):
        return _on_node_event(state, event, cfg)
    if isinstance(event, Telemetry):
        return _on_telemetry(state, event, cfg)
    if isinstance(event, ASREvent):
        return _on_asr_event(state, event)
    return []


def _on_node_event(state: RuleState, ev: NodeEvent, cfg: RuleConfig) -> list[Action]:
    if ev.type == "GAS_HIGH":
        if state.active_gas is not None:
            return []  # one active GAS alert at a time — no duplicates
        gas = ev.value if ev.value is not None else cfg.gas_warn
        return [
            OpenAlert(
                kind="GAS",
                level=2,
                title="Gas warning",
                message=_gas_message(2, gas, state.last_motion_s),
                facts=_gas_facts(gas, state),
                announce_phrase="gas_warning_hi",
                escalate_after_s=cfg.escalate_seconds,
            )
        ]

    if ev.type == "GAS_CRIT":
        gas = ev.value if ev.value is not None else cfg.gas_crit
        if state.active_gas is None:
            # crossed straight into critical: open at L3, no timer needed
            return [
                OpenAlert(
                    kind="GAS",
                    level=3,
                    title="Gas emergency",
                    message=_gas_message(3, gas, state.last_motion_s),
                    facts=_gas_facts(gas, state),
                    announce_phrase="gas_danger_hi",  # severity-correct (D-007)
                )
            ]
        if state.active_gas.level < 3:
            return [EscalateAlert(state.active_gas.id, facts=_gas_facts(gas, state))]
        return []

    if ev.type == "MOTION" and state.active_gas is not None:
        # J1.4b: elder responded — clear the escalation timer, keep the alert
        return [CancelTimer(state.active_gas.id, reason="motion near node")]

    return []


def _on_telemetry(state: RuleState, t: Telemetry, cfg: RuleConfig) -> list[Action]:
    actions: list[Action] = []
    if state.active_gas is not None:
        if t.gas_norm < cfg.gas_warn:
            # J1.4a: gas back below warn — danger passed, resolve outright
            actions.append(ResolveAlert(state.active_gas.id, reason="gas cleared"))
        elif t.motion:
            actions.append(CancelTimer(state.active_gas.id, reason="motion near node"))
    return actions


def _gas_facts(gas_norm: float, state: RuleState) -> dict[str, Any]:
    facts: dict[str, Any] = {"gas_norm": round(gas_norm, 3)}
    if state.last_motion_s is not None:
        facts["last_motion_s"] = int(state.last_motion_s)
    return facts


def _on_asr_event(state: RuleState, ev: ASREvent) -> list[Action]:
    """R-HELP + the Phase-5 response window (§6.9, §20 Phase 5).

    HELP word: a cry for help is explicit distress — no warning stage, no
    escalation timer. With a gas countdown running it escalates NOW; alone it
    opens straight at L3 (created-at-L3 pages the caregiver immediately, D-005).
    OK word: same treatment as motion (J1.4b) — evidence the elder responded
    defers the gas escalation, but never silences an alert (scope lock
    2026-07-14: the danger, not the response, drives resolution). An OK word
    never auto-resolves a HELP alert either — a misheard TV line must not
    cancel a real cry; J2's FALSE_ALARM-on-OK stays scoped to the Phase-6
    prompt window.
    """
    if ev.kind == "HELP":
        if state.active_gas is not None and state.active_gas.level < 3:
            return [EscalateAlert(
                state.active_gas.id, facts={"help_keyword": ev.keyword}
            )]
        if state.active_help is not None or state.active_gas is not None:
            return []  # already at L3 / already paging — nothing new to do
        return [
            OpenAlert(
                kind="HELP",
                level=3,
                title="Help requested",
                message=(
                    f"[HELP] Level 3. Voice keyword \"{ev.keyword}\" detected; "
                    f"no other sensor alarm active."
                ),
                facts={"keyword": ev.keyword, "conf": round(ev.conf, 2)},
                # one-shot re-locked 2026-07-17 (Ansh) — D-013 gap closed. The
                # WAV claims only the cry was heard + a phone notification went
                # out: true at open because created-at-L3 pages immediately
                # (D-005) — same phone-claim basis as D-012's escalation
                # one-shot. The D-007 announcer then repeats are_you_ok_hi.
                announce_phrase="help_heard_hi",
            )
        ]

    if ev.kind == "OK":
        if state.active_gas is not None and state.active_gas.level < 3:
            return [CancelTimer(state.active_gas.id, reason="elder responded ok")]
        return []

    return []
