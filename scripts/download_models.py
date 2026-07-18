"""Fetch SAATHI's AI model files into hub/models/ with sha256 verification.

Phase 0 deliverable (MASTER_ARCHITECTURE.md §20). Stdlib only — runs before the
venv exists. Idempotent: verified files are skipped on re-run.

Pinning policy (§27.18 — never trust pins blindly):
  * llama GGUF pin was read from Hugging Face LFS metadata on 2026-07-09.
  * Entries with url=None cannot be auto-fetched yet (they need a team export or an
    AI Hub account). Stage the file manually into hub/models/, then run
    `python scripts/download_models.py --pin <name>` to print the sha256 and paste
    it below. Tracked in docs/STATUS.md open items.

Exit code: 0 if every *required-now* model is present+verified or explicitly
deferred; 1 on download/checksum failure.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_DIR = REPO_ROOT / "hub" / "models"
CHUNK = 1024 * 1024  # 1 MiB


@dataclass(frozen=True)
class ModelSpec:
    name: str          # CLI name
    filename: str      # target file inside models dir (the pinned asset itself)
    url: str | None    # None = manual staging required (see module docstring)
    sha256: str | None # None = print-and-pin on first appearance
    phase: int         # first phase that needs it
    note: str
    extract_to: str | None = None  # if set: filename is a .zip, sha256-verify the
    # zip itself (that's the pinned release asset), then extract into
    # hub/models/<extract_to>/ — llama.cpp's win-* release ships llama-server.exe
    # alongside a required llama-server-impl.dll, so both must land in the same
    # folder (confirmed by inspecting the actual release archive, not assumed).


MODELS: list[ModelSpec] = [
    ModelSpec(
        name="llama",
        filename="llama-3.2-3b-q4.gguf",
        url=(
            "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF"
            "/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf"
        ),
        sha256="6c1a2b41161032677be168d354123594c0e6e67d2b9227c84f296ad037c728ff",
        phase=4,
        note="local LLM weights for llama.cpp server (~1.9 GB)",
    ),
    ModelSpec(
        name="pose",
        filename="yolov8n-pose.onnx",
        url=None,  # no auto-fetch: exported via `yolo export model=yolov8n-pose.pt format=onnx imgsz=640`
        sha256="bf36e3d9b7aae8aa0692272cb07e0bb898923b2aa79caa89c3a4e622990cca04",
        phase=6,
        note="pose model - STAGED+PINNED 2026-07-16 from Aman's export (ultralytics "
             "8.4.96, onnx 1.22.0 opset 20, imgsz 640, 12.9 MB). sha256 is for THAT "
             "exact artifact - a re-export on a different toolchain will differ; ship "
             "the staged file in the kit rather than re-exporting.",
    ),
    # Whisper-Base-En: AI Hub RETIRED the En export (page 404 + gone from
    # qai-hub-models 0.58.0, whose exporter now only emits chip-locked QNN,
    # verified 2026-07-16). Provenance deviation approved by Aman (option A):
    # local `optimum-cli export onnx` of openai/whisper-base.en - identical
    # weights, HF/optimum exporter, runs on CPU onnxruntime (smoke-tested:
    # encoder [1,80,3000]->1500x512, decoder -> 51864-logit vocab). The staged
    # folder hub/models/whisper-base-en-onnx/ also carries the tokenizer/config
    # set (tokenizer.json, vocab.json, merges.txt, config.json, etc.) the ASR
    # loader will need - unpinned companions, shipped with the folder.
    ModelSpec(
        name="whisper-encoder",
        filename="whisper-base-en-onnx/encoder_model.onnx",
        url=None,  # local optimum export - re-export will NOT match; ship the staged file
        sha256="dc8721022fd662f2d14dbf3613a6d2f01dbf04ded81799b823183a02e8c1bc13",
        phase=5,
        note="whisper-base.en ONNX encoder (optimum 2.2.0/optimum-onnx 0.1.0, "
             "opset 18, fp32, 82 MB) - STAGED+PINNED 2026-07-16 on Aman's Mac",
    ),
    ModelSpec(
        name="whisper-decoder",
        filename="whisper-base-en-onnx/decoder_model.onnx",
        url=None,  # local optimum export - re-export will NOT match; ship the staged file
        sha256="d8f86c966fdf3e0a4ccfafadcec9bed9b643eec57612a5b32d0fffead6497aa0",
        phase=5,
        note="whisper-base.en ONNX decoder (same export, 314 MB) - STAGED+PINNED "
             "2026-07-16 on Aman's Mac",
    ),
    ModelSpec(
        name="llama-server-arm64",
        filename="llama-b10034-bin-win-cpu-arm64.zip",
        url=(
            "https://github.com/ggml-org/llama.cpp/releases/download/b10034/"
            "llama-b10034-bin-win-cpu-arm64.zip"
        ),
        sha256="7137bb4638ccd31555167c2c1c3e1b79d49745f5e8bfa54d7312660cdf7b1d42",
        phase=4,
        note="llama.cpp CPU server, native win-arm64 (release b10034, pinned 2026-07-16 "
             "per 27.18 - downloaded + sha256d directly, contents inspected for "
             "llama-server.exe). Use if the event PC runs native ARM64 Python (§4 pin).",
        extract_to="llama-arm64",
    ),
    ModelSpec(
        name="llama-server-x64",
        filename="llama-b10034-bin-win-cpu-x64.zip",
        url=(
            "https://github.com/ggml-org/llama.cpp/releases/download/b10034/"
            "llama-b10034-bin-win-cpu-x64.zip"
        ),
        sha256="936539730059f642374c42d07eab51d974da3d0e50fcf59fd3b88d20293502e2",
        phase=4,
        note="llama.cpp CPU server, win-x64 (release b10034, pinned 2026-07-16 per "
             "27.18). Use if the event PC runs x64 Python for QNN access instead "
             "(the STATUS x64-vs-arm64 decision, undecided until hour 0) - stage "
             "BOTH so either outcome works without a re-fetch on-site.",
        extract_to="llama-x64",
    ),
]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "saathi-setup/1.0"})
    # timeout guards each socket op (connect/read), not the whole transfer -
    # a stalled connection fails in 30 s instead of hanging setup forever
    with urllib.request.urlopen(req, timeout=30) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(CHUNK):
            out.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r  downloading {dest.name}: {pct}% ({done // (1024*1024)} MiB)", end="")
    print()
    if total and done != total:
        tmp.unlink()  # a truncated stream must not masquerade as a checksum mismatch
        raise OSError(f"TRUNCATED (got {done} of {total} bytes)")
    tmp.replace(dest)


def extract_zip(spec: ModelSpec, zip_path: Path, models_dir: Path) -> None:
    """Extract a verified release zip into models_dir/<extract_to>/. Idempotent:
    skipped if llama-server.exe is already there (re-verifying the zip's sha256
    is enough of a re-check on repeat runs)."""
    assert spec.extract_to is not None
    dest = models_dir / spec.extract_to
    if (dest / "llama-server.exe").exists():
        print(f"[{spec.name}] already extracted -> {dest}/llama-server.exe")
        return
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    exe = dest / "llama-server.exe"
    if not exe.exists():
        # nested folder inside the zip - find it rather than assume a layout
        found = next(dest.rglob("llama-server.exe"), None)
        if found is None:
            raise RuntimeError(f"[{spec.name}] extracted but llama-server.exe not found under {dest}")
        exe = found
    print(f"[{spec.name}] extracted -> {exe}")


def process(spec: ModelSpec, models_dir: Path, pin_mode: bool) -> bool:
    """Returns True if this spec is in an acceptable state."""
    target = models_dir / spec.filename

    if target.exists():
        digest = file_sha256(target)
        if spec.sha256 is None:
            print(f"[{spec.name}] present, UNPINNED. sha256={digest}")
            if pin_mode:
                print(f"  -> paste into MODELS['{spec.name}'].sha256 in {__file__}")
            return True
        if digest == spec.sha256:
            print(f"[{spec.name}] present + verified ({spec.filename})")
            if spec.extract_to:
                extract_zip(spec, target, models_dir)
            return True
        print(f"[{spec.name}] CHECKSUM MISMATCH - expected {spec.sha256}, got {digest}")
        print("  file left in place for inspection; delete it and re-run to re-download")
        return False

    if spec.url is None:
        print(f"[{spec.name}] not present, no auto-fetch URL yet (needed from Phase {spec.phase}).")
        print(f"  {spec.note}")
        return True  # deferred by design, not a failure

    print(f"[{spec.name}] fetching - {spec.note}")
    try:
        download(spec.url, target)
    except (urllib.error.URLError, OSError) as e:
        print(f"[{spec.name}] DOWNLOAD FAILED: {e}")
        return False

    digest = file_sha256(target)
    if spec.sha256 is None:
        print(f"[{spec.name}] downloaded, sha256={digest} - pin this in MODELS")
        return True
    if digest != spec.sha256:
        target.unlink()
        print(f"[{spec.name}] CHECKSUM MISMATCH after download - deleted. Verify URL/pin (27.18).")
        return False
    print(f"[{spec.name}] downloaded + verified")
    if spec.extract_to:
        extract_zip(spec, target, models_dir)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=[m.name for m in MODELS], help="process a single model")
    ap.add_argument("--pin", metavar="NAME", choices=[m.name for m in MODELS],
                    help="print sha256 of an already-staged file so it can be pinned")
    ap.add_argument("--max-phase", type=int, default=99,
                    help="only fetch models needed up to this phase (e.g. 0 = none yet)")
    ap.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    args = ap.parse_args()

    args.models_dir.mkdir(parents=True, exist_ok=True)

    selected = [m for m in MODELS if m.phase <= args.max_phase]
    if args.only:
        selected = [m for m in MODELS if m.name == args.only]
    if args.pin:
        selected = [m for m in MODELS if m.name == args.pin]

    if not selected:
        print(f"no models needed at --max-phase={args.max_phase}; nothing to do")
        return 0

    ok = all([process(m, args.models_dir, pin_mode=bool(args.pin)) for m in selected])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
