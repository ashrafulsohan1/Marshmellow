"""Packaged-asset resolver tests (M5 Task 4)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from marshmellow.core import assets


def test_asset_dir_dev_is_repo_root() -> None:
    """Dev checkout (tests never run frozen): the asset root is the repo root."""
    assert getattr(sys, "frozen", False) is False
    assert assets.asset_dir() == Path(assets.__file__).resolve().parents[3]


def test_default_wake_model_path_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tmp asset_dir without the wake model → None (the caller's degrade path)."""
    monkeypatch.setattr(assets, "asset_dir", lambda: tmp_path)
    assert assets.default_wake_model_dir() == tmp_path / "wake"
    assert assets.default_wake_model_path() is None
    model = tmp_path / "wake" / "hey-marshmellow.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"onnx")
    assert assets.default_wake_model_path() == model


def test_default_stt_and_voice_dirs_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unbundled STT model / voices → None; present directories → themselves."""
    monkeypatch.setattr(assets, "asset_dir", lambda: tmp_path)
    assert assets.default_stt_model_dir() is None
    assert assets.default_voice_dir() is None
    stt = tmp_path / "models" / "faster-whisper-base"
    stt.mkdir(parents=True)
    (tmp_path / "voices").mkdir()
    assert assets.default_stt_model_dir() == stt
    assert assets.default_voice_dir() == tmp_path / "voices"
