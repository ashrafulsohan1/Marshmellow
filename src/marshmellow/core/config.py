"""Typed settings — one pydantic model is the only config schema (01-architecture §3.14).

TOML on disk (tomllib read / tomli-w write). A missing or malformed file yields
defaults plus a logged warning — the app never refuses to start over config.
API keys are stored as credential-manager *references*, never in the TOML.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field

from marshmellow.core.contracts import ActivationMode, RiskLevel

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


class LLMProviderConfig(BaseModel):
    kind: Literal["fake", "openai_compat", "ollama"] = "fake"
    base_url: str = ""
    model: str = ""
    api_key_ref: str = ""  # Windows Credential Manager ref, never the key itself
    vision: bool = False  # cloud model serves the D5 SoM vision fallback (§3.6)


class ProvidersConfig(BaseModel):
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    # §3.4 fallback order — the local adapter behind the cloud
    fallback: LLMProviderConfig | None = None


class SafetyConfig(BaseModel):
    preset: Literal["relaxed", "standard", "strict"] = "standard"
    # Per-kind risk overrides ([safety].overrides, keyed by ActionKind).
    # RiskPolicy applies them as max(table_level, override): they may only
    # escalate — the L3 floor is never lowered (§3.13).
    overrides: dict[str, RiskLevel] = Field(default_factory=dict)


class UIConfig(BaseModel):
    mascot_scale: float = 1.0
    theme: Literal["dark", "light"] = "dark"


class Settings(BaseModel):
    activation: ActivationMode = "both"
    hotkey: str = "ctrl+shift+m"
    wake_phrase: str = "hey marshmellow"
    autostart: bool = False
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    stt_model: str = "base"  # M0-SP2: only config inside the latency budget
    tts_voice: str = "en_US-lessac-medium"  # M0-SP7
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # exclude_none: tomli_w refuses null values — a None field (providers.
        # fallback, the default) is simply omitted and loads back as None.
        path.write_text(
            tomli_w.dumps(self.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
        )


def default_config_path() -> Path | None:
    """Where `load_settings` looks when the caller passes no path (discovery order):

    1. ``MARSHMELLOW_CONFIG`` env var (explicit override),
    2. ``config.local.toml`` next to the repo checkout (dev; gitignored),
    3. ``%LOCALAPPDATA%/Marshmellow/config.toml`` (the product location, §3.14).

    Returns None when nothing exists — the app then runs on defaults.
    """
    env = os.environ.get("MARSHMELLOW_CONFIG")
    if env:
        return Path(env)
    candidates = [REPO_ROOT / "config.local.toml"]
    lappdata = os.environ.get("LOCALAPPDATA")
    if lappdata:
        candidates.append(Path(lappdata) / "Marshmellow" / "config.toml")
    for path in candidates:
        if path.exists():
            return path
    return None


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from TOML, or defaults when absent/malformed."""
    if path is None or not path.exists():
        return Settings()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return Settings.model_validate(data)
    except Exception:
        logger.warning("Malformed config file %s; using defaults", path)
        return Settings()
