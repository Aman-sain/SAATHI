"""TTS pipeline (§6.10): bus `speak.request` → WAV bank → stdlib winsound.

The canonical phrases can NEVER fail (§4): playback is a committed WAV file —
no model, no network. One pump consumes `speak.request` sequentially, so
announcements never overlap; the bus's bounded queue (drop-oldest) is the
overflow policy. Missing phrase file → ERROR log + beep — never raise into
fusion. `speak_dynamic` (pyttsx3) is decorative (§11.5): best-effort, optional
import, absent on this install is a logged no-op, not an error.

D-008 addendum — "ack means silence NOW" (live findings 2026-07-12):
- 10 s phrases at a 15 s cadence build a playback backlog, so an ack could be
  followed by queued announcements. The pipeline watches alert.updated and
  DROPS queued phrases whose alert already reached a final state.
- The phrase already in the speaker is CUT mid-air. winsound quirk: a stop
  request only works from the thread that started the sound (a cross-thread
  PlaySound BLOCKS until the other sound ends — measured 5 s of frozen event
  loop). So ALL playback runs on one dedicated player thread: start async,
  poll a threading.Event for interrupt, purge from that same thread.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import wave
from pathlib import Path

from app.bus import EventBus

try:  # stdlib on Windows (the hub OS, §4); absent on mac/CI — degrade to logs
    import winsound
except ImportError:  # pragma: no cover - exercised only on non-Windows
    winsound = None  # type: ignore[assignment]

log = logging.getLogger("tts")

AUDIO_DIR = Path(__file__).resolve().parents[2] / "static" / "audio"

# §11.5 WAV bank: fusion refers to phrase IDs only; each id is `<id>.wav`.
# Hindi-only since D-012 (English alternates removed); all_clear_hi is the
# resolve one-shot ("gas back to normal") added the same session.
PHRASE_IDS = (
    "gas_warning_hi", "gas_danger_hi", "are_you_ok_hi",
    "help_heard_hi", "alert_sent_hi", "all_ok_hi", "all_clear_hi",
)

# Duplicated on purpose (see notify/remote.py): importing fusion here would
# couple this pipeline into the emergency loop's module graph.
_FINAL_STATES = ("RESOLVED", "FALSE_ALARM", "ACKED")
_FINAL_CAP = 100
_POLL_S = 0.05  # interrupt latency while a phrase is playing
# SND_ASYNC returns before audio actually starts (device open/wake lag), so a
# wait of exactly the WAV's duration expires while the tail is still in the
# speaker — the NEXT queued PlaySound then clips it (heard live 2026-07-12
# 20:04: alert_sent_hi and gas_danger_hi both end-cut at escalation, where
# phrases queue back-to-back). Pad the wait; ack-interrupt latency unaffected.
_TAIL_S = 0.5


def _start_play_async(path: Path) -> None:
    # returns immediately; the player thread owns the sound instance
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)


def _purge_blocking() -> None:
    # same-thread stop of the SND_ASYNC sound started above
    winsound.PlaySound(None, winsound.SND_PURGE)


def _beep_blocking() -> None:
    winsound.MessageBeep()


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 12.0  # conservative: longer than any bank phrase


class TtsPipeline:
    """OPTIONAL subsystem (§16): the emergency loop publishes speak.request and
    moves on — whatever happens in here is log-only."""

    def __init__(self, bus: EventBus, audio_dir: Path = AUDIO_DIR) -> None:
        self._bus = bus
        self._audio_dir = audio_dir
        self._tasks: list[asyncio.Task] = []
        self._finalized: dict[str, None] = {}  # insertion-ordered alert-id set
        self._now_playing: str | None = None   # alert_id behind the live phrase
        self._interrupt = threading.Event()
        self._playq: queue.Queue = queue.Queue()
        self._player: threading.Thread | None = None
        self.healthy = winsound is not None

    async def start(self) -> None:
        if winsound is not None:
            self._player = threading.Thread(
                target=self._player_main, name="tts-player", daemon=True
            )
            self._player.start()
        self._tasks = [
            asyncio.create_task(
                self._pump(self._bus.subscribe("speak.request")),
                name="tts-speak.request",
            ),
            asyncio.create_task(
                self._watch_alerts(self._bus.subscribe("alert.updated")),
                name="tts-alert.updated",
            ),
        ]
        missing = [p for p in PHRASE_IDS if not (self._audio_dir / f"{p}.wav").exists()]
        if missing:
            log.warning("tts wav bank incomplete missing=%s", ",".join(missing))
        if winsound is None:
            log.warning("tts no audio backend (winsound unavailable) — logging only")
        log.info("tts up bank=%s healthy=%s", self._audio_dir, self.healthy)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._player is not None:
            self._interrupt.set()          # cut any live phrase
            self._playq.put(None)          # sentinel: player thread exits
            await asyncio.to_thread(self._player.join, 2.0)
            self._player = None
        log.info("tts stopped")

    # --- dedicated player thread (owns every winsound call) ---

    def _player_main(self) -> None:
        while True:
            item = self._playq.get()
            if item is None:
                return
            path, done = item
            try:
                self._play_interruptible(path)
            except Exception as e:  # bad/corrupt WAV etc. — §16 log-only
                log.error("tts playback failed err=%s", e)
            finally:
                done.set()

    def _play_interruptible(self, path: Path) -> None:
        self._interrupt.clear()
        start = time.perf_counter()
        _start_play_async(path)
        deadline = time.monotonic() + _wav_seconds(path) + _TAIL_S
        while time.monotonic() < deadline:
            if self._interrupt.wait(_POLL_S):
                self._interrupt.clear()
                _purge_blocking()
                log.info("tts purged live announcement phrase=%s", path.stem)
                return
        log.info("tts played phrase=%s ms=%.0f",
                 path.stem, (time.perf_counter() - start) * 1000)

    # --- bus pumps ---

    async def _watch_alerts(self, stream) -> None:
        async for alert in stream:
            try:
                if getattr(alert, "state", None) in _FINAL_STATES:
                    self._finalized[alert.id] = None
                    while len(self._finalized) > _FINAL_CAP:
                        del self._finalized[next(iter(self._finalized))]
                    if self._now_playing == alert.id:
                        self._interrupt.set()  # thread-safe, never blocks the loop
            except Exception:
                log.exception("tts alert watch failed")  # §16: pump never dies

    async def _pump(self, stream) -> None:
        async for payload in stream:
            try:
                phrase_id = payload.get("phrase_id") if isinstance(payload, dict) else None
                if not phrase_id:
                    log.warning("tts ignored malformed speak.request payload=%r", payload)
                    continue
                alert_id = payload.get("alert_id")
                if alert_id in self._finalized:
                    log.info("tts dropped stale announcement alert=%s phrase=%s",
                             alert_id, phrase_id)
                    continue
                self._now_playing = alert_id
                try:
                    await self.speak(phrase_id)
                finally:
                    self._now_playing = None
            except Exception:
                log.exception("tts dispatch failed")  # §16: pump never dies

    async def speak(self, phrase_id: str) -> None:
        """WAV bank lookup → hand to the player thread, await completion."""
        path = self._audio_dir / f"{phrase_id}.wav"
        if not path.exists():
            log.error("tts missing phrase file=%s — beep fallback", path.name)
            await self._call_blocking(_beep_blocking)
            return
        if winsound is None or self._player is None:
            log.info("tts (no audio backend) would play %s", phrase_id)
            return
        done = threading.Event()
        self._playq.put((path, done))
        await asyncio.to_thread(done.wait)

    async def speak_dynamic(self, text: str) -> None:
        """Decorative (§11.5): dynamic SAPI speech if pyttsx3 exists, else no-op."""
        try:
            import pyttsx3  # optional — deliberately NOT in requirements.txt (§4)
        except ImportError:
            log.info("tts dynamic skipped (pyttsx3 not installed)")
            return

        def _say() -> None:
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()

        try:
            await asyncio.to_thread(_say)
            log.info("tts dynamic spoke chars=%s", len(text))
        except Exception as e:
            log.warning("tts dynamic failed err=%s", e)

    async def _call_blocking(self, fn, *args) -> bool:
        if winsound is None:
            log.info("tts (no audio backend) would play %s", args or "beep")
            return False
        try:
            await asyncio.to_thread(fn, *args)
            return True
        except Exception as e:
            log.error("tts playback failed err=%s", e)
            return False
