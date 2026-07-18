"""§19 Phase-5 ASR tests: keyword scan, VAD gate, self-throttle, callback →
worker → loop handoff, degraded starts, and the real whisper loader/decoder
machinery. No real mic and no model — the mic is an injected seam and the
loader tests stub onnxruntime + a tiny structurally-faithful export dir, so
everything passes on machines without hub/models/ (CI, Aman's Mac)."""

import asyncio
import json
import sys
import time
import types

import numpy as np
import pytest

from app.bus import EventBus
from app.config import Settings
from app.pipelines import asr
from app.pipelines.asr import SAMPLE_RATE, AsrPipeline, scan_keywords

HELP_KW = Settings(_env_file=None).help_keyword_list
OK_KW = Settings(_env_file=None).ok_keyword_list

LOUD = np.full((int(SAMPLE_RATE * asr.CHUNK_S), 1), 0.2, dtype="float32")
SILENT = np.zeros_like(LOUD)


class _FakeMic:
    def __init__(self):
        self.stopped = self.closed = False

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _pipeline(bus=None, transcriber=None, **kw):
    settings = kw.pop("settings", Settings(_env_file=None))
    return AsrPipeline(
        bus or EventBus(), settings, transcriber=transcriber,
        mic_factory=kw.pop("mic_factory", lambda s, cb: _FakeMic()), **kw,
    )


# --- keyword scan (§11.2: dumb substring, lowercased) ---

SCAN_CASES = [
    ("please HELP me", ("HELP", "help")),
    ("bachao koi hai", ("HELP", "bachao")),
    ("madad karo", ("HELP", "madad")),
    ("i'm fine, thanks", ("OK", "i'm fine")),
    ("haan main theek hoon", ("OK", "theek hoon")),
    ("help... no wait im ok", ("HELP", "help")),  # HELP outranks OK — fail toward paging
    ("the weather is lovely", None),
    ("", None),
]


@pytest.mark.parametrize("text,expected", SCAN_CASES, ids=[c[0] or "empty" for c in SCAN_CASES])
def test_scan_keywords(text, expected):
    assert scan_keywords(text, HELP_KW, OK_KW) == expected


def test_keyword_lists_parse_from_csv():
    s = Settings(_env_file=None, help_keywords=" Help , BACHAO ,", ok_keywords="")
    assert s.help_keyword_list == ("help", "bachao")
    assert s.ok_keyword_list == ()


# --- VAD gate + transcriber containment (worker-side logic) ---

@pytest.mark.asyncio
async def test_vad_gate_silence_never_reaches_transcriber():
    calls = []
    pipe = _pipeline(transcriber=lambda samples: calls.append(1) or "hello")
    await pipe.start()
    try:
        pipe._process_chunk(SILENT[:, 0])
        assert calls == []          # §11.2: silence is gated out
        pipe._process_chunk(LOUD[:, 0])
        assert calls == [1]
    finally:
        await pipe.stop()


@pytest.mark.asyncio
async def test_self_throttle_on_slow_transcription(monkeypatch):
    monkeypatch.setattr(asr, "CHUNK_BUDGET_S", 0.0)  # every call is "too slow"
    pipe = _pipeline(transcriber=lambda samples: "hello")
    await pipe.start()
    try:
        assert pipe._chunk_s == asr.CHUNK_S
        pipe._process_chunk(LOUD[:, 0])
        assert pipe._chunk_s == asr.THROTTLED_CHUNK_S  # §11.2 self-throttle, once
    finally:
        await pipe.stop()


# --- the full thread path: _on_audio (PortAudio) → worker → loop → bus ---

@pytest.mark.asyncio
async def test_help_keyword_travels_callback_to_bus():
    bus = EventBus()
    events = bus.subscribe("asr.event")
    pipe = _pipeline(bus, transcriber=lambda samples: "arre bachao bachao")
    await pipe.start()
    try:
        assert pipe.healthy is True
        pipe._on_audio(LOUD, len(LOUD), None, None)  # one full chunk buffered
        event = await asyncio.wait_for(anext(events), 2)
        assert (event.kind, event.keyword, event.synthetic) == ("HELP", "bachao", False)
    finally:
        await pipe.stop()


@pytest.mark.asyncio
async def test_ok_keyword_travels_callback_to_bus():
    bus = EventBus()
    events = bus.subscribe("asr.event")
    pipe = _pipeline(bus, transcriber=lambda samples: "main theek hoon beta")
    await pipe.start()
    try:
        pipe._on_audio(LOUD, len(LOUD), None, None)
        event = await asyncio.wait_for(anext(events), 2)
        assert (event.kind, event.keyword) == ("OK", "theek hoon")
    finally:
        await pipe.stop()


@pytest.mark.asyncio
async def test_transcriber_exception_contained_worker_survives():
    bus = EventBus()
    events = bus.subscribe("asr.event")
    boom = {"armed": True}

    def flaky(samples):
        if boom.pop("armed", None):
            raise RuntimeError("model exploded")
        return "help"

    pipe = _pipeline(bus, transcriber=flaky)
    await pipe.start()
    try:
        pipe._on_audio(LOUD, len(LOUD), None, None)   # raises inside the worker
        pipe._on_audio(LOUD, len(LOUD), None, None)   # worker must still be alive
        event = await asyncio.wait_for(anext(events), 2)
        assert event.kind == "HELP"                   # §16: contained, not fatal
    finally:
        await pipe.stop()


# --- degraded starts (§16: OPTIONAL subsystem, emergency loop unaffected) ---

@pytest.mark.asyncio
async def test_degraded_when_model_missing(tmp_path):
    statuses, mic_opened = [], []
    pipe = AsrPipeline(
        EventBus(), Settings(_env_file=None, models_dir=tmp_path),
        mic_factory=lambda s, cb: mic_opened.append(1),
        on_health=statuses.append,
    )
    await pipe.start()
    assert pipe.healthy is False
    assert statuses == ["down"]
    assert mic_opened == []       # no transcriber → the mic never opens
    await pipe.stop()             # stop of a degraded pipeline is a clean no-op


@pytest.mark.asyncio
async def test_mic_open_failure_degrades():
    statuses = []

    def no_mic(settings, cb):
        raise OSError("no input device")

    pipe = _pipeline(transcriber=lambda s: "hi", mic_factory=no_mic,
                     on_health=statuses.append)
    await pipe.start()
    assert pipe.healthy is False and statuses == ["down"]
    await pipe.stop()


@pytest.mark.asyncio
async def test_stop_stops_stream_and_joins_worker():
    mic = _FakeMic()
    pipe = _pipeline(transcriber=lambda s: "hi", mic_factory=lambda s, cb: mic)
    await pipe.start()
    await pipe.stop()
    assert mic.stopped and mic.closed
    assert pipe._worker is None and pipe.healthy is False


# --- whisper front end + detokenizer (pure numpy/stdlib, no model) ---

def test_log_mel_shape_and_energy():
    filters = asr._mel_filterbank()
    assert filters.shape == (asr.N_MELS, asr.N_FFT // 2 + 1)
    silence = asr._log_mel(np.zeros(int(SAMPLE_RATE * asr.CHUNK_S), dtype="float32"), filters)
    assert silence.shape == (1, 80, 3000) and silence.dtype == np.float32
    t = np.arange(int(SAMPLE_RATE * asr.CHUNK_S), dtype="float32") / SAMPLE_RATE
    tone = asr._log_mel(0.5 * np.sin(2 * np.pi * 440.0 * t), filters)
    assert np.isfinite(tone).all()
    assert tone.max() > silence.max()  # a real signal must beat the log floor


def test_byte_decoder_roundtrips_gpt2_alphabet():
    dec = asr._byte_decoder()
    assert dec["Ġ"] == 32          # GPT-2's space marker
    assert dec["h"] == ord("h")
    assert len(dec) == 256         # every byte reachable → detokenize can't KeyError


# --- real whisper loader (§11.2) — ORT + export stubbed, no model needed ---

# ids: 1=start, 2=eos, 3=notimestamps, 5/6=text tokens ("Ġhelp me" decoded)
_VOCAB = {"Ġhelp": 5, "Ġme": 6, "<|endoftext|>": 2}
_ADDED = {"<|startoftranscript|>": 1, "<|notimestamps|>": 3}
_PROMPT_LEN = 2  # [start, notimestamps] via forced_decoder_ids


def _fake_export_dir(tmp_path):
    d = tmp_path / "whisper-base-en-onnx"   # = the Settings default name
    d.mkdir()
    (d / "encoder_model.onnx").write_bytes(b"onnx")
    (d / "decoder_model.onnx").write_bytes(b"onnx")
    (d / "vocab.json").write_text(json.dumps(_VOCAB), encoding="utf-8")
    (d / "added_tokens.json").write_text(json.dumps(_ADDED), encoding="utf-8")
    (d / "generation_config.json").write_text(json.dumps({
        "decoder_start_token_id": 1, "eos_token_id": 2,
        "forced_decoder_ids": [[1, 3]],
    }), encoding="utf-8")
    return d


class _FakeIO:
    def __init__(self, name):
        self.name = name


class _FakeEncoder:
    def get_inputs(self):
        return [_FakeIO("input_features")]

    def run(self, _out, feeds):
        (mel,) = feeds.values()
        assert mel.shape == (1, 80, 3000)  # the export's fixed encoder input
        return [np.zeros((1, 1500, 512), dtype=np.float32)]


class _FakeDecoder:
    """Emits a scripted token per greedy step, keyed off the growing input_ids."""

    def __init__(self, script=(5, 6, 2)):
        self._script = script

    def get_inputs(self):
        return [_FakeIO("input_ids"), _FakeIO("encoder_hidden_states")]

    def run(self, _out, feeds):
        ids = feeds["input_ids"]
        assert ids.dtype == np.int64
        step = ids.shape[1] - _PROMPT_LEN
        logits = np.zeros((1, ids.shape[1], 16), dtype=np.float32)
        logits[0, -1, self._script[step]] = 1.0
        return [logits]


def _fake_ort(monkeypatch):
    def make_session(path, providers=None):
        return _FakeEncoder() if "encoder" in path else _FakeDecoder()
    monkeypatch.setitem(sys.modules, "onnxruntime",
                        types.SimpleNamespace(InferenceSession=make_session))


def test_load_whisper_transcribes_via_stubbed_sessions(tmp_path, monkeypatch):
    _fake_export_dir(tmp_path)
    _fake_ort(monkeypatch)
    transcriber = asr._load_whisper(Settings(_env_file=None, models_dir=tmp_path))
    assert transcriber is not None
    assert transcriber(LOUD[:, 0]) == " help me"  # mel→encoder→greedy→bytes


def test_load_whisper_missing_pair_returns_none(tmp_path):
    d = tmp_path / "whisper-base-en-onnx"
    d.mkdir()
    (d / "encoder_model.onnx").write_bytes(b"onnx")  # decoder absent → half pair
    assert asr._load_whisper(Settings(_env_file=None, models_dir=tmp_path)) is None


def test_load_whisper_broken_runtime_degrades_not_raises(tmp_path, monkeypatch):
    _fake_export_dir(tmp_path)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # import → ImportError
    assert asr._load_whisper(Settings(_env_file=None, models_dir=tmp_path)) is None


@pytest.mark.asyncio
async def test_real_loader_drives_keyword_flow_to_bus(tmp_path, monkeypatch):
    """start() with NO injected transcriber: _load_whisper (stubbed ORT) feeds
    the same mic→worker→bus path the live hub runs."""
    _fake_export_dir(tmp_path)
    _fake_ort(monkeypatch)
    bus = EventBus()
    events = bus.subscribe("asr.event")
    pipe = AsrPipeline(bus, Settings(_env_file=None, models_dir=tmp_path),
                       mic_factory=lambda s, cb: _FakeMic())
    await pipe.start()
    try:
        assert pipe.healthy is True
        pipe._on_audio(LOUD, len(LOUD), None, None)
        event = await asyncio.wait_for(anext(events), 2)
        assert (event.kind, event.keyword, event.synthetic) == ("HELP", "help", False)
    finally:
        await pipe.stop()


# --- §8 lifespan wiring (mirrors the Phase-4 wiring tests) ---

def test_app_mock_asr_skips_pipeline(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings = Settings(_env_file=None, db_path=tmp_path / "saathi.db",
                        log_dir=tmp_path / "logs", mock_asr=True)
    with TestClient(create_app(settings)) as client:
        assert client.app.state.asr is None  # §8: ASR skipped under MOCK_ASR
        health = client.get("/api/health").json()
        assert health["subsystems"]["asr"] == "down"  # §26: mock ≠ running


def test_app_asr_degrades_without_model_gas_loop_unaffected(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings = Settings(_env_file=None, db_path=tmp_path / "saathi.db",
                        log_dir=tmp_path / "logs", models_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        pipe = client.app.state.asr
        assert pipe is not None and pipe.healthy is False  # degraded, not dead
        assert client.get("/api/health").json()["subsystems"]["asr"] == "down"
        # the frozen invariant: gas demo works with ASR degraded
        client.post("/api/demo/trigger", json={"scenario": "gas", "synthetic": True})
        deadline = time.time() + 3
        alerts = []
        while time.time() < deadline and not alerts:
            alerts = client.get("/api/alerts").json()["alerts"]
            time.sleep(0.05)
        assert alerts and alerts[0]["kind"] == "GAS"
