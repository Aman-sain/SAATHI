"""Phase 4 + D-008 TTS tests: the committed WAV bank is complete and valid,
speak.request on the bus reaches playback, announcements never overlap (one
dedicated player thread), a missing phrase falls back to a beep without
raising (§16), queued phrases for a finalized alert are dropped, the live
phrase is purged mid-air on ack, and the pump survives malformed payloads
and playback errors."""

import asyncio
import time

from app.bus import EventBus
from app.domain import Alert
from app.pipelines import tts
from app.pipelines.tts import AUDIO_DIR, PHRASE_IDS, TtsPipeline


async def _until(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _alert(id="a-done", state="RESOLVED") -> Alert:
    now = time.time()
    return Alert(id=id, kind="GAS", level=3, state=state, title="t",
                 message="m", created_ts=now, updated_ts=now)


# --- WAV bank (§11.5) ---


def test_wav_bank_complete_and_valid():
    for phrase_id in PHRASE_IDS:
        path = AUDIO_DIR / f"{phrase_id}.wav"
        assert path.exists(), f"missing committed WAV: {path.name}"
        header = path.read_bytes()[:12]
        assert header[:4] == b"RIFF" and header[8:12] == b"WAVE", path.name


def test_every_rule_announce_phrase_exists_in_the_bank():
    # fusion refers to phrase IDs only (§11.5) — every id rules.py can emit
    # must be a committed file, or the announcement degrades to a beep
    import inspect

    from app.fusion import rules

    source = inspect.getsource(rules)
    used = {p for p in PHRASE_IDS if p in source}
    assert used, "rules.py no longer names any announce phrase?"
    for phrase_id in used:
        assert (AUDIO_DIR / f"{phrase_id}.wav").exists()


# --- pump wiring + fallback behavior (playback stubbed, no sound in CI) ---


def _run_pipeline(scenario_body, monkeypatch, duration_s=0.0):
    """Start a TtsPipeline on a fresh bus, record playback starts, run the body."""
    calls = []

    monkeypatch.setattr(tts, "_start_play_async",
                        lambda path: calls.append(("play", path.name)))
    monkeypatch.setattr(tts, "_wav_seconds", lambda path: duration_s)
    monkeypatch.setattr(tts, "_beep_blocking", lambda: calls.append(("beep", None)))
    monkeypatch.setattr(tts, "winsound", object())  # backend "present" even on CI

    async def scenario():
        bus = EventBus()
        pipeline = TtsPipeline(bus)
        await pipeline.start()
        await scenario_body(bus, pipeline, calls)
        await pipeline.stop()

    asyncio.run(scenario())
    return calls


def test_speak_request_on_bus_plays_the_phrase(monkeypatch):
    async def body(bus, pipeline, calls):
        bus.publish("speak.request", {"phrase_id": "gas_warning_hi", "alert_id": "a-1"})
        await _until(lambda: calls)

    calls = _run_pipeline(body, monkeypatch)
    assert calls == [("play", "gas_warning_hi.wav")]


def test_missing_phrase_beeps_and_pump_survives(monkeypatch):
    async def body(bus, pipeline, calls):
        bus.publish("speak.request", {"phrase_id": "no_such_phrase", "alert_id": "a-1"})
        bus.publish("speak.request", {"phrase_id": "all_ok_hi", "alert_id": "a-2"})
        await _until(lambda: len(calls) == 2)

    calls = _run_pipeline(body, monkeypatch)
    assert calls == [("beep", None), ("play", "all_ok_hi.wav")]


def test_malformed_payload_is_ignored_not_fatal(monkeypatch):
    async def body(bus, pipeline, calls):
        bus.publish("speak.request", "junk-string")
        bus.publish("speak.request", {"no_phrase": True})
        bus.publish("speak.request", {"phrase_id": "all_ok_hi"})
        await _until(lambda: calls)

    calls = _run_pipeline(body, monkeypatch)
    assert calls == [("play", "all_ok_hi.wav")]


def test_announcements_never_overlap(monkeypatch):
    """Two rapid requests: the single player thread serializes them fully."""
    starts = []
    monkeypatch.setattr(tts, "_start_play_async",
                        lambda path: starts.append((path.name, time.monotonic())))
    monkeypatch.setattr(tts, "_wav_seconds", lambda path: 0.08)
    monkeypatch.setattr(tts, "winsound", object())

    async def scenario():
        bus = EventBus()
        pipeline = TtsPipeline(bus)
        await pipeline.start()
        bus.publish("speak.request", {"phrase_id": "gas_warning_hi"})
        bus.publish("speak.request", {"phrase_id": "gas_danger_hi"})
        await _until(lambda: len(starts) == 2)
        await pipeline.stop()

    asyncio.run(scenario())
    assert [name for name, _ in starts] == ["gas_warning_hi.wav", "gas_danger_hi.wav"]
    assert starts[1][1] - starts[0][1] >= 0.08  # second waited out the first


def test_playback_error_is_log_only(monkeypatch):
    def broken_play(path):
        raise OSError("audio device busy")

    monkeypatch.setattr(tts, "_start_play_async", broken_play)
    monkeypatch.setattr(tts, "_wav_seconds", lambda path: 0.0)
    monkeypatch.setattr(tts, "winsound", object())

    async def scenario():
        bus = EventBus()
        pipeline = TtsPipeline(bus)
        await pipeline.start()
        await pipeline.speak("all_ok_hi")  # must not raise
        await pipeline.stop()

    asyncio.run(scenario())


# --- D-008 addendum: ack means silence NOW ---


def test_queued_announcements_dropped_after_alert_finalized(monkeypatch):
    """Phrases queued behind a long WAV must not play once their alert reached
    a final state (heard live 2026-07-12 09:27)."""

    async def body(bus, pipeline, calls):
        bus.publish("alert.updated", _alert("a-done", "RESOLVED"))
        await _until(lambda: "a-done" in pipeline._finalized)
        bus.publish("speak.request", {"phrase_id": "gas_danger_hi", "alert_id": "a-done"})
        bus.publish("speak.request", {"phrase_id": "all_ok_hi", "alert_id": "a-live"})
        await _until(lambda: calls)

    calls = _run_pipeline(body, monkeypatch)
    # the finalized alert's phrase was dropped; the live one played
    assert calls == [("play", "all_ok_hi.wav")]


def test_finalizing_alert_purges_the_live_phrase(monkeypatch):
    """The phrase already in the speaker is cut mid-air, from the player thread
    (a cross-thread stop blocks the event loop — measured live 2026-07-12)."""
    purged = []
    monkeypatch.setattr(tts, "_start_play_async", lambda path: None)
    monkeypatch.setattr(tts, "_wav_seconds", lambda path: 5.0)  # "long" phrase
    monkeypatch.setattr(tts, "_purge_blocking", lambda: purged.append(True))
    monkeypatch.setattr(tts, "winsound", object())

    async def scenario():
        bus = EventBus()
        pipeline = TtsPipeline(bus)
        await pipeline.start()
        bus.publish("speak.request", {"phrase_id": "gas_danger_hi", "alert_id": "a-x"})
        await _until(lambda: pipeline._now_playing == "a-x")
        bus.publish("alert.updated", _alert("a-x", "ACKED"))
        await _until(lambda: purged)  # cut well before the 5 s "duration"
        await pipeline.stop()

    start = time.monotonic()
    asyncio.run(scenario())
    assert purged and time.monotonic() - start < 3.0


def test_speak_dynamic_without_pyttsx3_is_a_noop():
    async def scenario():
        pipeline = TtsPipeline(EventBus())
        await pipeline.speak_dynamic("hello")  # pyttsx3 absent → logged no-op

    asyncio.run(scenario())
