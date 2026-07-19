"""ASR pipeline (§6.9/§11.2): mic → 3 s chunks → energy VAD → whisper →
keyword scan → `ASREvent` on bus topic `asr.event`.

Phase-5 state (real transcriber landed 2026-07-17 against Aman's staged
export): `hub/models/whisper-base-en-onnx/` is an optimum-cli ONNX export of
whisper-base.en — encoder [1,80,3000]→(1,1500,512), no-KV-cache decoder
(input_ids + encoder_hidden_states → 51864 logits), tokenizer/config JSONs
shipped in-folder (see the download_models.py pin notes). Greedy decode is
enough for keyword spotting, so the whole path fits the pinned stack:
numpy log-mel → ORT CPU sessions → GPT-2 byte-level detokenize via vocab.json
(stdlib json). Model dir missing/broken → start() degrades to healthy=False,
the mic never opens, and R-HELP is driven by the demo endpoint / MOCK_ASR
(§12.6). `listen_for_response` (the fall-flow prompt window, §6.9) lands with
its only consumer, Phase-6 vision — the gas response window already works
because fusion consumes `asr.event` continuously (D-013).

Thread rule (§6.2): the PortAudio callback only buffers; chunks hop to one
worker thread for transcription (a 1–2 s model call inside the audio callback
would drop input); events hand off to the loop-affine bus via
call_soon_threadsafe (ingest is the worked example).

Privacy (§10): the matched keyword is the ONLY thing that ever leaves this
module — transcripts stay function-local, never logged, never persisted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from app.bus import EventBus
from app.config import Settings
from app.domain import ASREvent

log = logging.getLogger("asr")

SAMPLE_RATE = 16_000       # §11.2: 16 kHz mono float32
CHUNK_S = 3.0              # §11.2 chunk length…
THROTTLED_CHUNK_S = 5.0    # …self-throttles to this when transcription is slow
CHUNK_BUDGET_S = 2.5       # §11.2: one chunk must transcribe inside this on CPU
VAD_RMS = 0.010            # energy gate: near-silent chunks never reach whisper
OVERLAP_S = 0.8            # tail carried into the next chunk: a keyword that
                           # straddles a chunk cut is otherwise lost in both
                           # halves (~1-in-6 odds at 3 s chunks, bench 07-19)

# whisper front end (fixed by the export: encoder input is [1, 80, 3000])
N_FFT = 400
HOP = 160
N_MELS = 80
N_AUDIO_SAMPLES = 30 * SAMPLE_RATE  # chunks are zero-padded to whisper's 30 s window
MAX_NEW_TOKENS = 32        # a 3–5 s chunk is ≤ ~15 words; keywords need no more

# samples (float32 in [-1,1]) -> transcript text
Transcriber = Callable[[np.ndarray], str]


def scan_keywords(
    transcript: str, help_keywords: tuple[str, ...], ok_keywords: tuple[str, ...]
) -> tuple[str, str] | None:
    """§11.2: substring match on the lowercased transcript — no semantic
    parsing, dumb and reliable. HELP outranks OK ("help… no wait, I'm fine"
    still pages; a false page costs an ack, a missed cry costs everything)."""
    text = transcript.lower()
    for kw in help_keywords:
        if kw in text:
            return "HELP", kw
    for kw in ok_keywords:
        if kw in text:
            return "OK", kw
    return None


def _byte_decoder() -> dict[str, int]:
    """Inverse of GPT-2's bytes_to_unicode: vocab.json stores each token in a
    reversible unicode alphabet; mapping chars back yields the utf-8 bytes."""
    bs = (list(range(ord("!"), ord("~") + 1)) + list(range(0xA1, 0xAD))
          + list(range(0xAE, 0x100)))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(bs, cs)}


def _mel_filterbank() -> np.ndarray:
    """Slaney-scale mel filterbank (librosa formulation, fmax=8 kHz) — matches
    whisper's shipped mel filters within float tolerance, no asset file needed."""
    def to_mel(hz: np.ndarray) -> np.ndarray:
        mel = hz * 3.0 / 200.0
        log_r = hz >= 1000.0
        mel[log_r] = 15.0 + 27.0 * np.log(hz[log_r] / 1000.0) / np.log(6.4)
        return mel

    def to_hz(mel: np.ndarray) -> np.ndarray:
        hz = mel * 200.0 / 3.0
        log_r = mel >= 15.0
        hz[log_r] = 1000.0 * np.exp(np.log(6.4) * (mel[log_r] - 15.0) / 27.0)
        return hz

    pts = to_hz(np.linspace(0.0, to_mel(np.array([SAMPLE_RATE / 2.0]))[0], N_MELS + 2))
    freqs = np.linspace(0.0, SAMPLE_RATE / 2.0, N_FFT // 2 + 1)
    lower = (freqs - pts[:-2, None]) / np.diff(pts)[:-1, None]
    upper = (pts[2:, None] - freqs) / np.diff(pts)[1:, None]
    weights = np.maximum(0.0, np.minimum(lower, upper))
    return (weights * (2.0 / (pts[2:] - pts[:-2]))[:, None]).astype(np.float32)


def _log_mel(samples: np.ndarray, filters: np.ndarray) -> np.ndarray:
    """Whisper's log-mel front end in numpy: pad/trim to 30 s, centered hann
    STFT, mel projection, log10 with an 8 dB floor, (x+4)/4 normalization."""
    audio = np.asarray(samples, dtype=np.float32)
    if len(audio) < N_AUDIO_SAMPLES:
        audio = np.pad(audio, (0, N_AUDIO_SAMPLES - len(audio)))
    else:
        audio = audio[:N_AUDIO_SAMPLES]
    padded = np.pad(audio, N_FFT // 2, mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP]
    window = np.hanning(N_FFT + 1)[:-1]  # periodic hann, as torch.hann_window
    stft = np.fft.rfft(frames * window, axis=-1)
    power = (np.abs(stft[:-1]) ** 2).T  # whisper drops the trailing frame → 3000
    mel = filters @ power
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0)[None].astype(np.float32)


class _WhisperTranscriber:
    """The staged optimum export pair + its in-folder tokenizer JSONs (§11.2).

    Greedy, timestamp-free, no KV cache — the decoder re-runs per token, which
    is fine at ≤32 tokens; the worker's self-throttle absorbs slow chunks.
    Token ids come from the export's own generation_config.json/config.json at
    runtime, never from memory."""

    def __init__(self, encoder: Any, decoder: Any, export_dir: Path) -> None:
        self._encoder = encoder
        self._decoder = decoder
        self._filters = _mel_filterbank()
        self._bytes = _byte_decoder()
        vocab = json.loads((export_dir / "vocab.json").read_text(encoding="utf-8"))
        added_file = export_dir / "added_tokens.json"
        added: dict[str, int] = (
            json.loads(added_file.read_text(encoding="utf-8"))
            if added_file.exists() else {}
        )
        self._id_to_token = {i: t for t, i in {**vocab, **added}.items()}
        gen: dict[str, Any] = {}
        for name in ("generation_config.json", "config.json"):
            f = export_dir / name
            if f.exists():
                gen = json.loads(f.read_text(encoding="utf-8"))
                break
        self._eos = int(gen["eos_token_id"])
        forced = gen.get("forced_decoder_ids") or []
        self._prompt = [int(gen["decoder_start_token_id"])]
        if forced:
            self._prompt += [int(tid) for _pos, tid in sorted(forced)]
        elif "<|notimestamps|>" in added:  # older exports force it, newer configs may not
            self._prompt.append(int(added["<|notimestamps|>"]))
        self._enc_input = encoder.get_inputs()[0].name
        dec_names = [i.name for i in decoder.get_inputs()]
        self._dec_ids = next(n for n in dec_names if "input_ids" in n)
        self._dec_hidden = next(n for n in dec_names if "encoder" in n)

    def __call__(self, samples: np.ndarray) -> str:
        mel = _log_mel(samples, self._filters)
        (hidden,) = self._encoder.run(None, {self._enc_input: mel})
        tokens = list(self._prompt)
        for _ in range(MAX_NEW_TOKENS):
            (logits,) = self._decoder.run(None, {
                self._dec_ids: np.asarray([tokens], dtype=np.int64),
                self._dec_hidden: hidden,
            })
            next_id = int(np.argmax(logits[0, -1]))
            if next_id == self._eos:
                break
            tokens.append(next_id)
        return self._detokenize(tokens[len(self._prompt):])

    def _detokenize(self, ids: list[int]) -> str:
        parts = []
        for i in ids:
            tok = self._id_to_token.get(i)
            if tok is None or tok.startswith("<|"):
                continue  # timestamps/specials never reach the keyword scan
            parts.append(tok)
        data = bytes(self._bytes.get(ch, 32) for ch in "".join(parts))
        return data.decode("utf-8", errors="replace")


def _load_whisper(settings: Settings) -> Transcriber | None:
    """§11.2 real path against the staged export dir; any failure = WARN +
    degrade (§16) — the emergency loop never depends on this returning."""
    path = settings.models_path / settings.asr_model_file
    pair = [path / "encoder_model.onnx", path / "decoder_model.onnx"]
    if not all(f.exists() for f in pair):
        log.warning(
            "asr degraded: whisper export pair missing at %s (need "
            "encoder_model.onnx + decoder_model.onnx, see download_models.py) — "
            "R-HELP still runs via the demo endpoint (§12.6)", path,
        )
        return None
    try:
        import onnxruntime as ort  # deferred: degraded hubs never pay the import

        sessions = [
            ort.InferenceSession(str(f), providers=["CPUExecutionProvider"])
            for f in pair
        ]
        transcriber = _WhisperTranscriber(*sessions, export_dir=path)
    except Exception:
        log.exception(
            "asr degraded: whisper load failed at %s — continuing without ASR "
            "(§16)", path,
        )
        return None
    log.info("asr whisper pair loaded dir=%s", path.name)
    return transcriber


def _open_mic(settings: Settings, callback) -> Any:
    """Open + start the sounddevice input stream (§4: PortAudio in the wheel)."""
    import sounddevice as sd  # deferred: only the live path touches PortAudio

    device: Any = settings.mic_device
    if device in ("", "default"):
        device = None
    elif device.isdigit():
        device = int(device)
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
        device=device, callback=callback,
    )
    stream.start()
    return stream


class AsrPipeline:
    """OPTIONAL subsystem (§16): whatever happens in here is log-only — the
    emergency loop (sensor→fusion→TTS→WS) never notices ASR failing."""

    def __init__(
        self,
        bus: EventBus,
        settings: Settings,
        transcriber: Transcriber | None = None,
        mic_factory: Callable[[Settings, Callable], Any] = _open_mic,
        on_health: Callable[[str], None] | None = None,
    ) -> None:
        self._bus = bus
        self._settings = settings
        self._transcriber = transcriber
        self._mic_factory = mic_factory
        self._on_health = on_health or (lambda status: None)
        self._help_kw = settings.help_keyword_list
        self._ok_kw = settings.ok_keyword_list
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self._worker: threading.Thread | None = None
        self._chunks: queue.Queue = queue.Queue(maxsize=4)  # bounded — audio never blocks
        self._buf: list[np.ndarray] = []
        self._buffered = 0
        self._chunk_s = CHUNK_S
        self.healthy = False

    # --- lifecycle ---

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._transcriber is None:
            self._transcriber = _load_whisper(self._settings)
        if self._transcriber is None:
            self._on_health("down")
            return  # degraded — _load_whisper logged why
        try:
            self._stream = self._mic_factory(self._settings, self._on_audio)
        except Exception as e:
            log.error("asr mic open failed err=%s — continuing degraded (§16)", e)
            self._on_health("down")
            return
        self._worker = threading.Thread(
            target=self._worker_main, name="asr-worker", daemon=True
        )
        self._worker.start()
        self.healthy = True
        self._on_health("up")
        log.info("asr up rate=%s chunk=%ss help=%s ok=%s", SAMPLE_RATE,
                 self._chunk_s, ",".join(self._help_kw), ",".join(self._ok_kw))

    async def stop(self) -> None:
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await asyncio.to_thread(stream.stop)
            await asyncio.to_thread(stream.close)
        if self._worker is not None:
            self._chunks.put(None)  # sentinel: worker exits after current chunk
            await asyncio.to_thread(self._worker.join, 2.0)
            self._worker = None
        self.healthy = False
        log.info("asr stopped")

    # --- PortAudio thread: buffer only ---

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("asr mic status=%s", status)
        self._buf.append(indata[:, 0].copy())
        self._buffered += frames
        if self._buffered < int(self._chunk_s * SAMPLE_RATE):
            return
        chunk = np.concatenate(self._buf)
        self._buf.clear()
        tail = chunk[-int(OVERLAP_S * SAMPLE_RATE):]
        self._buf.append(tail)
        self._buffered = len(tail)
        try:
            self._chunks.put_nowait(chunk)
        except queue.Full:
            log.warning("asr worker behind — chunk dropped")

    # --- worker thread: VAD → transcribe → keyword scan → loop handoff ---

    def _worker_main(self) -> None:
        while True:
            chunk = self._chunks.get()
            if chunk is None:
                return
            try:
                self._process_chunk(chunk)
            except Exception:
                log.exception("asr chunk failed")  # §16: the worker never dies

    def _process_chunk(self, samples: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
        if rms < VAD_RMS:
            return  # §11.2 energy VAD
        start = time.perf_counter()
        text = self._transcriber(samples)
        elapsed = time.perf_counter() - start
        if elapsed > CHUNK_BUDGET_S and self._chunk_s < THROTTLED_CHUNK_S:
            self._chunk_s = THROTTLED_CHUNK_S
            log.warning("asr self-throttled to %ss chunks (transcribe took %.1fs)",
                        THROTTLED_CHUNK_S, elapsed)
        match = scan_keywords(text, self._help_kw, self._ok_kw)
        if match is None:
            return
        kind, keyword = match
        # §10/§17: log the matched keyword and nothing else of the transcript
        log.info("asr keyword=%s kind=%s ms=%.0f", keyword, kind, elapsed * 1000)
        event = ASREvent(ts=time.time(), kind=kind, keyword=keyword)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._bus.publish, "asr.event", event)
