from pathlib import Path

import pytest
from pydantic import ValidationError

from marshmellow.core import config as config_mod
from marshmellow.core.config import SafetyConfig, Settings, default_config_path, load_settings


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nope.toml")
    assert s.activation == "both"
    assert s.autostart is False
    assert s.providers.llm.kind == "fake"
    assert s.safety == SafetyConfig()  # standard preset, no overrides
    assert s.stt_model == "base"  # M0-SP2 decision
    assert s.tts_voice == "en_US-lessac-medium"  # M0-SP7 decision


def test_safety_overrides_roundtrip(tmp_path: Path) -> None:
    """Task 4: per-kind risk overrides live in config ([safety].overrides)."""
    s = Settings()
    s.safety = SafetyConfig(preset="strict", overrides={"type_text": "L3", "click": "L1"})
    p = tmp_path / "config.toml"
    s.save(p)
    loaded = load_settings(p)
    assert loaded.safety.preset == "strict"
    assert loaded.safety.overrides == {"type_text": "L3", "click": "L1"}


def test_safety_override_with_invalid_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SafetyConfig(overrides={"type_text": "L9"})  # type: ignore[dict-item]


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


def test_config_discovery_env_var_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "explicit.toml"
    p.write_text("", encoding="utf-8")
    monkeypatch.setenv("MARSHMELLOW_CONFIG", str(p))
    assert default_config_path() == p


def test_config_discovery_repo_local_before_product_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARSHMELLOW_CONFIG", raising=False)
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lappdata"))
    assert default_config_path() is None  # neither exists → defaults

    (tmp_path / "config.local.toml").write_text("", encoding="utf-8")
    assert default_config_path() == tmp_path / "config.local.toml"

    (tmp_path / "config.local.toml").unlink()
    product = tmp_path / "lappdata" / "Marshmellow"
    product.mkdir(parents=True)
    (product / "config.toml").write_text("", encoding="utf-8")
    assert default_config_path() == product / "config.toml"
