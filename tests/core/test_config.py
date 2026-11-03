from pathlib import Path

import pytest

from marshmellow.core.config import Settings, load_settings


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nope.toml")
    assert s.activation == "both"
    assert s.autostart is False
    assert s.providers.llm.kind == "fake"
    assert s.safety.preset == "standard"
    assert s.stt_model == "base"  # M0-SP2 decision
    assert s.tts_voice == "en_US-lessac-medium"  # M0-SP7 decision


def test_toml_roundtrip(tmp_path: Path) -> None:
    s = Settings()
    s.activation = "hotkey"
    s.providers.llm.kind = "openai_compat"
    s.wake_phrase = "hey marshmellow"
    p = tmp_path / "config.toml"
    s.save(p)
    loaded = load_settings(p)
    assert loaded == s


def test_malformed_toml_gives_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "bad.toml"
    p.write_text("not [valid toml", encoding="utf-8")
    s = load_settings(p)
    assert s.activation == "both"
    assert any("config" in r.getMessage().lower() for r in caplog.records)
