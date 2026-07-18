"""§19 test_rules: table-driven over R-GAS (gas warn/crit/clear/motion) and
R-HELP + the Phase-5 response window (help/ok words vs active alerts). Rules
are pure functions, so these tests need no bus, no DB, no clock."""

import pytest

from app.domain import Alert, ASREvent, NodeEvent, Telemetry
from app.fusion import rules
from app.fusion.rules import (
    CancelTimer, EscalateAlert, OpenAlert, ResolveAlert, RuleConfig, RuleState,
)

CFG = RuleConfig(gas_warn=0.35, gas_crit=0.60, escalate_seconds=30)


def _gas_alert(level=2, state="ANNOUNCED") -> Alert:
    return Alert(
        id="a-gas1", kind="GAS", level=level, state=state, title="Gas warning",
        message="m", created_ts=1.0, updated_ts=1.0,
    )


def _help_alert(state="ANNOUNCED") -> Alert:
    return Alert(
        id="a-help1", kind="HELP", level=3, state=state, title="Help requested",
        message="m", created_ts=1.0, updated_ts=1.0,
    )


def _asr(kind: str, keyword="help") -> ASREvent:
    return ASREvent(ts=2.0, kind=kind, keyword=keyword)


def _event(type_: str, value=None) -> NodeEvent:
    return NodeEvent(node_id="node1", ts=2.0, type=type_, value=value)


def _telemetry(gas_norm: float, motion=False) -> Telemetry:
    return Telemetry(
        node_id="node1", ts=2.0, gas_raw=int(gas_norm * 1023), gas_norm=gas_norm,
        temp_c=31.5, motion=motion, sound_rms=0.04,
    )


# --- table: (name, state, event, expected action types) ---
CASES = [
    ("gas_high opens L2", RuleState(), _event("GAS_HIGH", 0.42), [OpenAlert]),
    ("gas_high deduped while active", RuleState(active_gas=_gas_alert()),
     _event("GAS_HIGH", 0.45), []),
    ("gas_crit escalates active L2", RuleState(active_gas=_gas_alert()),
     _event("GAS_CRIT", 0.71), [EscalateAlert]),
    ("gas_crit on already-L3 is noop", RuleState(active_gas=_gas_alert(level=3)),
     _event("GAS_CRIT", 0.9), []),
    ("gas_crit with no alert opens L3", RuleState(), _event("GAS_CRIT", 0.71), [OpenAlert]),
    ("motion clears timer", RuleState(active_gas=_gas_alert()), _event("MOTION"), [CancelTimer]),
    ("motion without alert is noop", RuleState(), _event("MOTION"), []),
    ("node_boot is noop", RuleState(), _event("NODE_BOOT"), []),
    ("loud_noise is noop in phase 2", RuleState(), _event("LOUD_NOISE", 0.8), []),
    ("telemetry below warn resolves", RuleState(active_gas=_gas_alert()),
     _telemetry(0.10), [ResolveAlert]),
    ("telemetry motion clears timer", RuleState(active_gas=_gas_alert()),
     _telemetry(0.50, motion=True), [CancelTimer]),
    ("telemetry high gas no alert is noop", RuleState(), _telemetry(0.50), []),
    ("telemetry quiet no alert is noop", RuleState(), _telemetry(0.10), []),
]


@pytest.mark.parametrize("name,state,event,expected", CASES, ids=[c[0] for c in CASES])
def test_r_gas_table(name, state, event, expected):
    assert [type(a) for a in rules.evaluate(state, event, CFG)] == expected


def test_gas_high_action_details():
    (action,) = rules.evaluate(RuleState(last_motion_s=540), _event("GAS_HIGH", 0.42), CFG)
    assert action.kind == "GAS" and action.level == 2
    assert action.escalate_after_s == CFG.escalate_seconds  # threshold from config
    assert action.announce_phrase == "gas_warning_hi"       # J1.3 wav id
    assert action.facts == {"gas_norm": 0.42, "last_motion_s": 540}
    assert "0.42" in action.message  # template text carries the sensor facts


def test_gas_crit_direct_open_is_level3_no_timer():
    (action,) = rules.evaluate(RuleState(), _event("GAS_CRIT", 0.71), CFG)
    assert action.level == 3 and action.escalate_after_s == 0.0


def test_escalate_carries_fresh_gas_fact():
    state = RuleState(active_gas=_gas_alert(), last_motion_s=120)
    (action,) = rules.evaluate(state, _event("GAS_CRIT", 0.71), CFG)
    assert action.alert_id == "a-gas1"
    assert action.facts == {"gas_norm": 0.71, "last_motion_s": 120}


# --- Phase 5: R-HELP + response window ---

HELP_CASES = [
    ("help alone opens L3", RuleState(), _asr("HELP"), [OpenAlert]),
    ("help deduped while help active", RuleState(active_help=_help_alert()),
     _asr("HELP", "madad"), []),
    ("help during gas countdown escalates now", RuleState(active_gas=_gas_alert()),
     _asr("HELP", "bachao"), [EscalateAlert]),
    ("help with gas already L3 is noop", RuleState(active_gas=_gas_alert(level=3)),
     _asr("HELP"), []),
    ("ok word defers gas escalation", RuleState(active_gas=_gas_alert()),
     _asr("OK", "theek hoon"), [CancelTimer]),
    ("ok with gas already L3 is noop", RuleState(active_gas=_gas_alert(level=3)),
     _asr("OK", "theek hoon"), []),
    ("ok alone is noop", RuleState(), _asr("OK", "im ok"), []),
    # safety: a misheard OK must never cancel a real cry for help (see _on_asr_event)
    ("ok never resolves a help alert", RuleState(active_help=_help_alert()),
     _asr("OK", "im ok"), []),
]


@pytest.mark.parametrize("name,state,event,expected", HELP_CASES,
                         ids=[c[0] for c in HELP_CASES])
def test_r_help_table(name, state, event, expected):
    assert [type(a) for a in rules.evaluate(state, event, CFG)] == expected


def test_help_open_action_details():
    (action,) = rules.evaluate(RuleState(), _asr("HELP", "bachao"), CFG)
    assert action.kind == "HELP" and action.level == 3
    assert action.escalate_after_s == 0.0     # already terminal — nothing to escalate to
    assert action.announce_phrase == "help_heard_hi"  # re-locked 2026-07-17 (D-013)
    assert action.facts == {"keyword": "bachao", "conf": 1.0}
    assert "bachao" in action.message


def test_help_escalation_carries_keyword_fact():
    (action,) = rules.evaluate(
        RuleState(active_gas=_gas_alert()), _asr("HELP", "madad"), CFG)
    assert action.alert_id == "a-gas1"
    assert action.facts == {"help_keyword": "madad"}
