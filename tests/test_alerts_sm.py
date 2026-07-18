"""§19 test_alerts_sm: all legal + illegal transitions, idempotent ack, and the
invariant that every transition is persisted AND broadcast."""

import asyncio

import pytest

from app.bus import EventBus
from app.fusion.alerts import AlertManager, IllegalTransition
from app.storage.db import Database
from app.storage.repo import Repo


@pytest.fixture
def stack(tmp_path):
    db = Database(tmp_path / "saathi.db")
    db.start()
    bus = EventBus()
    yield AlertManager(Repo(db), bus), Repo(db), db, bus
    db.stop()


def _open_gas(mgr):
    return mgr.create(
        kind="GAS", level=2, title="Gas warning", message="[GAS] Level 2",
        facts={"gas_norm": 0.42},
    )


def test_golden_path_open_announce_escalate_ack_resolve(stack):
    mgr, repo, db, _ = stack
    a = _open_gas(mgr)
    assert a.state == "OPEN" and a.level == 2

    a = mgr.announce(a)
    assert a.state == "ANNOUNCED"

    a = mgr.escalate(a, facts={"last_motion_s": 540})
    assert a.state == "ESCALATED" and a.level == 3
    assert a.facts == {"gas_norm": 0.42, "last_motion_s": 540}  # facts merge, not replace

    a = mgr.ack(a)
    assert a.state == "RESOLVED"  # ack = ACKED then RESOLVED (J1.6)
    assert mgr.active() == []

    db.flush()  # final persisted row matches the final state
    assert repo.get_alert(a.id).state == "RESOLVED"


def test_every_transition_is_persisted_and_broadcast(stack):
    mgr, repo, db, bus = stack

    async def run():
        created = bus.subscribe("alert.created")
        updated = bus.subscribe("alert.updated")
        a = _open_gas(mgr)
        mgr.ack(mgr.escalate(mgr.announce(a)))
        assert (await asyncio.wait_for(anext(created), 1)).state == "OPEN"
        states = [
            (await asyncio.wait_for(anext(updated), 1)).state for _ in range(4)
        ]
        assert states == ["ANNOUNCED", "ESCALATED", "ACKED", "RESOLVED"]
        return a.id

    alert_id = asyncio.run(run())
    db.flush()
    assert repo.get_alert(alert_id).state == "RESOLVED"


def test_illegal_transitions_raise(stack):
    mgr, *_ = stack
    resolved = mgr.resolve(_open_gas(mgr))
    with pytest.raises(IllegalTransition):
        mgr.escalate(resolved)  # no resurrection after RESOLVED
    with pytest.raises(IllegalTransition):
        mgr.transition(resolved, "OPEN")

    escalated = mgr.escalate(_open_gas(mgr))
    with pytest.raises(IllegalTransition):
        mgr.announce(escalated)  # ESCALATED may only be acked/resolved


def test_ack_is_idempotent_double_tap_safe(stack):
    mgr, *_ = stack
    a = mgr.announce(_open_gas(mgr))
    first = mgr.ack(a)
    second = mgr.ack(first)  # caregiver double-tap
    assert first.state == second.state == "RESOLVED"
    assert second == first  # unchanged, no new transition


def test_false_alarm_path(stack):
    mgr, *_ = stack
    a = mgr.announce(_open_gas(mgr))
    a = mgr.false_alarm(a)  # J2.4: OK-word response resolves as FALSE_ALARM
    assert a.state == "FALSE_ALARM"
    assert mgr.active() == []


def test_restore_readopts_only_active_states(stack):
    mgr, *_ = stack
    open_alert = _open_gas(mgr).model_copy(update={"id": "a-live"})
    done_alert = open_alert.model_copy(update={"id": "a-done", "state": "RESOLVED"})
    fresh = AlertManager(mgr._repo, mgr._bus)  # simulated hub restart (§14)
    fresh.restore(open_alert)
    fresh.restore(done_alert)
    assert [a.id for a in fresh.active()] == ["a-live"]


def test_active_by_kind_finds_only_active(stack):
    mgr, *_ = stack
    a = _open_gas(mgr)
    assert mgr.active_by_kind("GAS").id == a.id
    assert mgr.active_by_kind("FALL") is None
    mgr.resolve(a)
    assert mgr.active_by_kind("GAS") is None
