"""hub/ is the import root (§8: run_hub.py lives there), so tests import the
package exactly as the app does: `from app.… import …`."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hub"))


@pytest.fixture(autouse=True)
def _silence_tts(monkeypatch):
    """§19 tests must run on any laptop, silently: never play real audio.
    (Playing the ~9 s gas WAV also stalls loop shutdown until it finishes.)
    Tests that assert on playback re-patch these same attributes themselves.

    Copy-first note: the tts import is guarded so the spine tests run before
    pipelines/tts is copied (Stage 4). Once tts lands the stub always applies —
    behaviour is identical to the reference from Stage 4 onward."""
    try:
        from app.pipelines import tts
    except ModuleNotFoundError:
        return

    monkeypatch.setattr(tts, "_start_play_async", lambda path: None)
    monkeypatch.setattr(tts, "_wav_seconds", lambda path: 0.0)
    monkeypatch.setattr(tts, "_beep_blocking", lambda: None)
