"""Packaged-asset resolver — where bundled resources live (M5 Task 4).

One place answers "where are the shipped models and voices" for both layouts:
a frozen (PyInstaller) build — resources unpacked next to the executable, with
``sys._MEIPASS`` pointing at the extraction dir in one-file mode — and the dev
checkout, where the repo root is the asset root. The ``default_*`` helpers
return ``None`` when a directory is absent so callers keep their existing
degrade paths instead of crashing on a missing asset (Tasks 5 and 7 consume
these when wiring the packaged build).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_WAKE_MODEL_NAME = "hey-marshmellow.onnx"


def asset_dir() -> Path:
    """The bundled-resource root for the running layout.

    Frozen: ``sys._MEIPASS`` when PyInstaller set it (one-file extraction
    dir), else the executable's directory (one-dir builds). Dev checkout: the
    repo root, so ``wake/``, ``models/`` and ``voices/`` sit beside the
    checkout like every M0 asset tier assumes.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return REPO_ROOT


def default_wake_model_dir() -> Path:
    """Wake-word model directory (``asset_dir()/wake``)."""
    return asset_dir() / "wake"


def default_wake_model_path() -> Path | None:
    """The self-trained wake model, or None when it was never trained/shipped."""
    path = default_wake_model_dir() / _WAKE_MODEL_NAME
    return path if path.exists() else None


def default_stt_model_dir() -> Path | None:
    """The bundled faster-whisper model dir, or None when it is not bundled."""
    path = asset_dir() / "models" / "faster-whisper-base"
    return path if path.exists() else None


def default_voice_dir() -> Path | None:
    """The bundled piper voice dir, or None when it is not bundled."""
    path = asset_dir() / "voices"
    return path if path.exists() else None
