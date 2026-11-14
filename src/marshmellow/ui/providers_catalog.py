"""Provider catalog — the settings UI's single source of provider truth.

Users never edit config files: they pick a provider, get its known base URLs
and typical models as dropdowns (model/base-URL fields stay editable for
anything not listed), paste a key, press Test. The catalog is pure data so
the cascades are testable headless; the one I/O function is the connection
probe (httpx, transport injectable for tests).

Base URLs and model lists reflect the endpoints known at authoring time
(2026-11); the model field is editable precisely because these lists age.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx


@dataclass(frozen=True)
class ProviderOption:
    """One selectable provider: its kind mapping, known endpoints, models."""

    id: str
    label: str
    kind: str  # the Settings.providers.llm.kind this maps to
    base_urls: tuple[str, ...]  # known-valid endpoints; first = default
    models: tuple[str, ...]  # typical models; first = default (editable)
    requires_key: bool
    note: str = ""


PROVIDERS: Final[tuple[ProviderOption, ...]] = (
    ProviderOption(
        id="zai",
        label="Z.AI / GLM",
        kind="openai_compat",
        base_urls=(
            "https://api.z.ai/api/paas/v4",
            "https://open.bigmodel.cn/api/paas/v4",
        ),
        models=("glm-4.6", "glm-4.5-air", "glm-4.5v"),
        requires_key=True,
        note="Z.AI open platform or Zhipu mainland. glm-4.5v is vision-capable "
        "(needed for the screenshot perception fallback).",
    ),
    ProviderOption(
        id="openai",
        label="OpenAI",
        kind="openai_compat",
        base_urls=("https://api.openai.com/v1",),
        models=("gpt-4o", "gpt-4.1", "gpt-4o-mini"),
        requires_key=True,
        note="Pick a vision-capable model (gpt-4o class) for the screenshot fallback.",
    ),
    ProviderOption(
        id="anthropic",
        label="Anthropic / Claude (compat)",
        kind="openai_compat",
        base_urls=("https://api.anthropic.com/v1",),
        models=("claude-sonnet-4-5", "claude-opus-4-1"),
        requires_key=True,
        note="Anthropic's OpenAI-compatibility layer with an Anthropic API key. "
        "Claude Code subscriptions authenticate via OAuth and are not key-based — "
        "not usable here yet.",
    ),
    ProviderOption(
        id="openrouter",
        label="OpenRouter",
        kind="openai_compat",
        base_urls=("https://openrouter.ai/api/v1",),
        models=("anthropic/claude-sonnet-4.5", "openai/gpt-4o", "z-ai/glm-4.6"),
        requires_key=True,
        note="One key, many upstream providers; model ids are provider-prefixed.",
    ),
    ProviderOption(
        id="codex",
        label="OpenAI Codex (ChatGPT plan)",
        kind="openai_compat",
        base_urls=("https://chatgpt.com/backend-api/codex",),
        models=("gpt-5-codex",),
        requires_key=True,
        note="Uses your ChatGPT-plan backend. The key field takes the OAuth token "
        "the Codex CLI stores locally — experimental, may break on provider changes.",
    ),
    ProviderOption(
        id="ollama",
        label="Ollama (local, no key)",
        kind="ollama",
        base_urls=("http://localhost:11434/v1",),
        models=("qwen2.5vl:7b", "llava:13b"),
        requires_key=False,
        note="Fully local — no key, no cloud. Pull a vision-capable model for the "
        "perception fallback; UI understanding is weaker than the cloud options.",
    ),
    ProviderOption(
        id="custom",
        label="Custom (OpenAI-compatible)",
        kind="openai_compat",
        base_urls=(),
        models=(),
        requires_key=True,
        note="Type your own endpoint and model — anything speaking the OpenAI "
        "chat-completions dialect (vLLM, LM Studio, a proxy...).",
    ),
)

NOT_CONFIGURED_LABEL = "— not configured (preview mode) —"

_BY_ID: Final[dict[str, ProviderOption]] = {p.id: p for p in PROVIDERS}


def provider_by_id(provider_id: str) -> ProviderOption | None:
    return _BY_ID.get(provider_id)


def provider_for(kind: str, base_url: str) -> ProviderOption | None:
    """The catalog entry matching a stored config (kind + base_url), for
    pre-selecting the dropdowns. Exact base-URL match wins; kind alone is
    ambiguous (several providers share openai_compat) → None unless unique."""
    if kind == "fake":
        return None
    if base_url:
        for p in PROVIDERS:
            if base_url in p.base_urls:
                return p
    matches = [p for p in PROVIDERS if p.kind == kind]
    if len(matches) == 1:
        return matches[0]
    return None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


def probe_provider(
    base_url: str,
    model: str,
    api_key: str | None,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    """One tiny chat completion against the endpoint; never raises.

    The settings dialog runs this in a worker thread and reports the verdict;
    transport is injectable so tests can drive it with a MockTransport."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            response = client.post(url, json=body, headers=headers)
    except Exception as exc:
        return ProbeResult(ok=False, detail=f"connection failed: {exc!r}")
    if response.status_code == 200:
        return ProbeResult(ok=True, detail="connection ok — the provider answered")
    return ProbeResult(
        ok=False,
        detail=f"HTTP {response.status_code}: {response.text[:200]}",
    )
