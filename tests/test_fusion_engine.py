"""§19 golden scenarios for the fusion engine: gas rises → L2 → timer → L3;
motion/clear/ack cancel the timer; §14 restart-escalation; no timer leaks."""

import asyncio
import time

import pytest

from app.bus import EventBus
from app.config import Settings
from app.domain import Alert, NodeEvent, Telemetry
from app.fusion.engine import FusionEngine
from app.storage.db import Database
from app.storage.repo import Repo

ESC = 0.15  # escalation window compressed for tests (threshold comes from config)


@pytest.fixture
def stack(tmp_path):
    db = Database(tmp_path / "saathi.db")
    db.start()
    settings = Settings(_env_file=None, db_path=tmp_path / "saathi.db",
                        escalate_seconds=ESC)
    yield EventBus(), Repo(db), db, settings
    db.stop()


def _gas_high(value=0.42) -> NodeEvent:
    return NodeEvent(node_id="node1", ts=time.time(), type="GAS_HIGH", value=value)


def _telemetry(gas_norm: float, motion=False) -> Telemetry:
    return Telemetry(node_id="node1", ts=time.time(), gas_raw=300, gas_norm=gas_norm,
                     temp_c=31.0, motion=motion, sound_rms=0.05)


async def _next_state(sub, want: str, timeout=2.0) -> Alert:
    """Read alert.updated until `want` appears — fails loudly on a wrong order."""
    while True:
        alert = await asyncio.wait_for(anext(sub), timeout)
        if alert.state == want:
            return alert
        raise AssertionError(f"expected {want}, got {alert.state}")


@pytest.mark.asyncio
async def test_golden_gas_open_announced_escalated_and_db_rows(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    created, updated, speak = (bus.subscribe(t) for t in
                               ("alert.created", "alert.updated", "speak.request"))
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())

        opened = await asyncio.wait_for(anext(created), 1)
        assert (opened.state, opened.level, opened.kind) == ("OPEN", 2, "GAS")

        announced = await _next_state(updated, "ANNOUNCED")
        assert (await asyncio.wait_for(anext(speak), 1))["phrase_id"] == "gas_warning_hi"

        escalated = await _next_state(updated, "ESCALATED")  # timer fires (no motion/ack)
        assert escalated.level == 3
        assert escalated.id == announced.id
        assert engine._timers == {}  # fired timer cleaned itself up

        db.flush()
        row = repo.get_alert(escalated.id)  # rows in DB — §20 criterion
        assert row.state == "ESCALATED" and row.level == 3
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_motion_cancels_timer_no_escalation(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(0.05)  # OPEN→ANNOUNCED, timer armed
        bus.publish("node.event", NodeEvent(node_id="node1", ts=time.time(), type="MOTION"))
        await asyncio.sleep(ESC * 2)  # well past the would-be escalation
        (alert,) = engine.alerts.active()
        assert alert.state == "ANNOUNCED" and alert.level == 2  # elder responded (J1.4b)
        assert engine._timers == {}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_gas_clearing_resolves_and_cancels_timer(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(0.05)
        bus.publish("node.telemetry", _telemetry(0.08))  # back below GAS_WARN (J1.4a)
        await asyncio.sleep(0.05)
        assert engine.alerts.active() == []
        assert engine._timers == {}
        db.flush()
        (row,) = repo.list_alerts()
        assert row.state == "RESOLVED"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_gas_crit_escalates_immediately(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    updated = bus.subscribe("alert.updated")
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await _next_state(updated, "ANNOUNCED")
        bus.publish("node.event",
                    NodeEvent(node_id="node1", ts=time.time(), type="GAS_CRIT", value=0.71))
        escalated = await _next_state(updated, "ESCALATED")  # no waiting for the timer
        assert escalated.level == 3 and escalated.facts["gas_norm"] == 0.71
        assert engine._timers == {}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_ack_cancels_timer_and_resolves(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(0.05)
        (alert,) = engine.alerts.active()
        acked = engine.ack(alert.id)  # what Phase 3's ack route will call
        assert acked.state == "RESOLVED"
        assert engine._timers == {} and engine.alerts.active() == []
        assert engine.ack("a-nope") is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_restart_escalates_stale_announced_alert(stack):
    bus, repo, db, settings = stack
    stale = Alert(
        id="a-old", kind="GAS", level=2, state="ANNOUNCED", title="Gas warning",
        message="m", created_ts=time.time() - 60, updated_ts=time.time() - 60,
    )
    repo.upsert_alert(stale)
    db.flush()
    engine = FusionEngine(bus, repo, settings)  # simulated hub restart (§14)
    await engine.start()
    try:
        await asyncio.sleep(0.05)
        db.flush()
        assert repo.get_alert("a-old").state == "ESCALATED"  # older than window → now
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_stop_cancels_armed_timers(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    await engine.start()
    bus.publish("node.event", _gas_high())
    await asyncio.sleep(0.05)
    assert len(engine._timers) == 1
    await engine.stop()
    assert engine._timers == {}  # §20 failure watch: no leaked tasks
    assert engine._announcers == {}


# --- D-007: repeating announcements while the danger persists ---

REPEAT = 0.05  # compressed announce interval for tests


def _repeat_settings(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "saathi.db",
                    escalate_seconds=kw.pop("escalate_seconds", ESC),
                    announce_repeat_seconds=kw.pop("announce_repeat_seconds", REPEAT))


async def _drain(sub, seconds: float) -> list:
    got = []
    deadline = asyncio.get_event_loop().time() + seconds
    while (left := deadline - asyncio.get_event_loop().time()) > 0:
        try:
            got.append(await asyncio.wait_for(anext(sub), left))
        except asyncio.TimeoutError:
            break
    return got


@pytest.mark.asyncio
async def test_announcement_repeats_while_active_and_upgrades_on_escalation(stack, tmp_path):
    bus, repo, db, _ = stack
    engine = FusionEngine(bus, repo, _repeat_settings(tmp_path, escalate_seconds=0.12))
    speak = bus.subscribe("speak.request")
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        phrases = [m["phrase_id"] for m in await _drain(speak, 0.5)]
    finally:
        await engine.stop()
    assert phrases[0] == "gas_warning_hi"                  # J1.3 first announce
    assert "alert_sent_hi" in phrases                      # one-shot at escalation
    assert "gas_danger_hi" in phrases                      # L3 repeats use danger phrase
    assert phrases.count("alert_sent_hi") == 1
    assert len(phrases) >= 4                               # it genuinely repeats


@pytest.mark.asyncio
async def test_ack_stops_repeating_announcements(stack, tmp_path):
    bus, repo, db, _ = stack
    engine = FusionEngine(bus, repo, _repeat_settings(tmp_path, escalate_seconds=60))
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(REPEAT * 3)  # first announce + a few repeats
        (alert,) = engine.alerts.active()
        engine.ack(alert.id)
        assert engine._announcers == {}
        # subscribed after ack: the one-shot confirmation already fired, so any
        # message here would be a leaked announcer → must stay silent
        post_ack = bus.subscribe("speak.request")
        assert await _drain(post_ack, REPEAT * 4) == []
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_gas_clearing_stops_repeating_announcements(stack, tmp_path):
    bus, repo, db, _ = stack
    engine = FusionEngine(bus, repo, _repeat_settings(tmp_path, escalate_seconds=60))
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(REPEAT * 2)
        assert len(engine._announcers) == 1
        bus.publish("node.telemetry", _telemetry(0.08))  # J1.4a: danger passed
        await asyncio.sleep(0.05)
        assert engine._announcers == {}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_repeat_zero_announces_exactly_once(stack, tmp_path):
    bus, repo, db, _ = stack
    engine = FusionEngine(
        bus, repo, _repeat_settings(tmp_path, escalate_seconds=60,
                                    announce_repeat_seconds=0))
    speak = bus.subscribe("speak.request")
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        phrases = [m["phrase_id"] for m in await _drain(speak, REPEAT * 5)]
        assert phrases == ["gas_warning_hi"]
        assert engine._announcers == {}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_gas_crit_direct_open_announces_danger_phrase(stack, tmp_path):
    bus, repo, db, _ = stack
    engine = FusionEngine(
        bus, repo, _repeat_settings(tmp_path, announce_repeat_seconds=0))
    speak = bus.subscribe("speak.request")
    await engine.start()
    try:
        bus.publish("node.event",
                    NodeEvent(node_id="node1", ts=time.time(), type="GAS_CRIT", value=0.71))
        first = await asyncio.wait_for(anext(speak), 1)
        assert first["phrase_id"] == "gas_danger_hi"
    finally:
        await engine.stop()


# --- scope lock 2026-07-14: post-ack confirmation one-shot ---


@pytest.mark.asyncio
async def test_ack_speaks_confirmation_once_without_alert_id(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(0.05)
        (alert,) = engine.alerts.active()
        speak = bus.subscribe("speak.request")
        engine.ack(alert.id)
        msgs = await _drain(speak, 0.1)
        # exactly one confirmation; NO alert_id — the ack finalized the alert
        # and tts drops phrases whose alert reached a final state (D-008)
        assert [m["phrase_id"] for m in msgs] == ["all_ok_hi"]
        assert "alert_id" not in msgs[0]
        # re-ack of a finished alert is idempotent and stays silent (fresh
        # subscription: a timed-out _drain closes the previous generator)
        speak2 = bus.subscribe("speak.request")
        engine.ack(alert.id)
        assert await _drain(speak2, 0.1) == []
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_gas_clearing_speaks_all_clear_once_without_alert_id(stack, tmp_path):
    # D-012 Moment ④b: gas returns to normal on its own → one reassurance
    bus, repo, db, _ = stack
    engine = FusionEngine(
        bus, repo, _repeat_settings(tmp_path, escalate_seconds=60,
                                    announce_repeat_seconds=60))
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(0.05)  # OPEN→ANNOUNCED, alert active
        speak = bus.subscribe("speak.request")  # after the initial announce
        bus.publish("node.telemetry", _telemetry(0.08))  # gas cleared (J1.4a)
        msgs = await _drain(speak, 0.1)
        # exactly one all-clear; NO alert_id — resolve finalized the alert and
        # tts drops finalized-alert phrases (D-008), like the post-ack one-shot
        assert [m["phrase_id"] for m in msgs] == ["all_clear_hi"]
        assert "alert_id" not in msgs[0]
    finally:
        await engine.stop()


# --- Phase 5: R-HELP through the engine (asr.event is a first-class input) ---


def _asr_help(keyword="help", synthetic=False):
    from app.domain import ASREvent
    return ASREvent(ts=time.time(), kind="HELP", keyword=keyword, synthetic=synthetic)


def _asr_ok(keyword="theek hoon"):
    from app.domain import ASREvent
    return ASREvent(ts=time.time(), kind="OK", keyword=keyword)


@pytest.mark.asyncio
async def test_help_keyword_opens_l3_with_help_heard_oneshot(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    created = bus.subscribe("alert.created")
    speak = bus.subscribe("speak.request")
    await engine.start()
    try:
        bus.publish("asr.event", _asr_help("bachao", synthetic=True))
        opened = await asyncio.wait_for(anext(created), 1)
        assert (opened.kind, opened.level) == ("HELP", 3)
        assert opened.synthetic is True  # contract #8 taint flows from the event
        assert opened.facts["keyword"] == "bachao"
        await asyncio.sleep(0.05)
        (alert,) = engine.alerts.active()
        assert alert.state == "ANNOUNCED"
        assert engine._timers == {}  # L3 open: no escalation countdown
        # open one-shot (D-013 gap closed 2026-07-17): help_heard_hi WITH the
        # alert_id, so an ack purges it mid-air like any danger phrase (D-008)
        msgs = await _drain(speak, 0.1)
        assert [m["phrase_id"] for m in msgs] == ["help_heard_hi"]
        assert msgs[0]["alert_id"] == opened.id
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_help_alert_repeats_are_you_ok_checkin(stack, tmp_path):
    bus, repo, db, _ = stack
    engine = FusionEngine(bus, repo, _repeat_settings(tmp_path, escalate_seconds=60))
    speak = bus.subscribe("speak.request")
    await engine.start()
    try:
        bus.publish("asr.event", _asr_help())
        phrases = [m["phrase_id"] for m in await _drain(speak, REPEAT * 5)]
        assert phrases[0] == "help_heard_hi"  # the cry is answered immediately
        assert phrases[1:] and set(phrases[1:]) == {"are_you_ok_hi"}  # D-007 check-in
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_help_during_gas_countdown_escalates_immediately(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    updated = bus.subscribe("alert.updated")
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await _next_state(updated, "ANNOUNCED")
        bus.publish("asr.event", _asr_help("madad"))
        escalated = await _next_state(updated, "ESCALATED")  # no waiting for the timer
        assert escalated.level == 3 and escalated.kind == "GAS"
        assert escalated.facts["help_keyword"] == "madad"
        assert engine._timers == {}
        assert engine.alerts.active_by_kind("HELP") is None  # no second alert opened
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_ok_word_defers_gas_escalation_keeps_alert(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    await engine.start()
    try:
        bus.publish("node.event", _gas_high())
        await asyncio.sleep(0.05)  # OPEN→ANNOUNCED, timer armed
        bus.publish("asr.event", _asr_ok())
        await asyncio.sleep(ESC * 2)  # well past the would-be escalation
        (alert,) = engine.alerts.active()
        assert alert.state == "ANNOUNCED" and alert.level == 2  # deferred, not silenced
        assert engine._timers == {}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_ok_word_never_resolves_help_alert(stack):
    bus, repo, db, settings = stack
    engine = FusionEngine(bus, repo, settings)
    await engine.start()
    try:
        bus.publish("asr.event", _asr_help())
        await asyncio.sleep(0.05)
        bus.publish("asr.event", _asr_ok("im ok"))
        await asyncio.sleep(0.05)
        (alert,) = engine.alerts.active()  # still active — ack/demo resolves it
        assert alert.kind == "HELP" and alert.state == "ANNOUNCED"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_restored_alert_resumes_announcements(stack, tmp_path):
    bus, repo, db, _ = stack
    stale = Alert(
        id="a-old", kind="GAS", level=3, state="ESCALATED", title="Gas emergency",
        message="m", created_ts=time.time() - 60, updated_ts=time.time() - 60,
    )
    repo.upsert_alert(stale)
    db.flush()
    engine = FusionEngine(bus, repo, _repeat_settings(tmp_path))  # hub restart (§14)
    speak = bus.subscribe("speak.request")
    await engine.start()
    try:
        phrases = [m["phrase_id"] for m in await _drain(speak, REPEAT * 4)]
        assert phrases and set(phrases) == {"gas_danger_hi"}  # danger persists audibly
    finally:
        await engine.stop()
