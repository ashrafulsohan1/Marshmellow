"""Fetch the bundle inputs the installer spec needs at BUILD time (M5 Task 7).

The piper voice and the faster-whisper ct2 model are re-downloadable binaries
that stay out of git (`.gitignore`: ``voices/`` and ``*.onnx``), so the build
fetches them into the dev asset locations the spec datas reference:

- ``voices/<tts_voice>.onnx`` + ``.onnx.json``  → bundled to ``_internal/voices``
- ``models/faster-whisper-base/`` (ct2)   → bundled to ``_internal/models/faster-whisper-base``

The ct2 model is copied from the local HuggingFace cache when present (offline
rebuilds work), else downloaded once from ``Systran/faster-whisper-base`` (the
model faster-whisper resolves for the hub name ``base`` — the exact artifact
the packaged build must ship so a clean account is offline-capable).

Usage: ``uv run python installer/fetch_bundle_inputs.py [--voice en_US-lessac-medium]``
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOICE = "en_US-lessac-medium"  # M0-SP7: config default, 04-tooling row
STT_REPO = "Systran/faster-whisper-base"  # what hub name "base" resolves to
STT_DIR = REPO_ROOT / "models" / "faster-whisper-base"


def _fetch_voice(voice: str) -> Path:
    dest = REPO_ROOT / "voices" / f"{voice}.onnx"
    if dest.exists() and dest.with_suffix(".onnx.json").exists():
        print(f"[ok] voice present: {dest}")
        return dest
    (REPO_ROOT / "voices").mkdir(parents=True, exist_ok=True)
    print(f"[fetch] piper voice {voice} via python -m piper.download_voices …")
    subprocess.run(
        [sys.executable, "-m", "piper.download_voices", voice, "--data-dir",
         str(REPO_ROOT / "voices")],
        check=True,
    )
    if not dest.exists():
        raise SystemExit(f"voice download finished but {dest} is missing")
    return dest


def _fetch_stt_model() -> Path:
    needed = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
    if STT_DIR.exists() and all((STT_DIR / f).exists() for f in needed):
        print(f"[ok] ct2 model present: {STT_DIR}")
        return STT_DIR
    STT_DIR.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download

    print(f"[fetch] {STT_REPO} → {STT_DIR} (HF cache reused when warm)")
    snapshot_download(repo_id=STT_REPO, local_dir=STT_DIR)
    missing = [f for f in needed if not (STT_DIR / f).exists()]
    if missing:
        raise SystemExit(f"download finished but {STT_DIR} lacks {missing}")
    return STT_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="piper voice to bundle")
    args = parser.parse_args(argv)

    voice_onnx = _fetch_voice(args.voice)
    _fetch_stt_model()
    print("[done] bundle inputs ready (never committed; re-downloadable)")
    # The voice json must ride along: piper's config defaults to <model>.json.
    assert voice_onnx.with_suffix(".onnx.json").exists(), "voice config json missing"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
