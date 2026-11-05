"""OpenAI-compatible cloud provider (D7 BYOK).

The `openai` SDK keeps us endpoint-agnostic: GLM/Z.AI, OpenAI, Anthropic's
compat shim, vLLM and Ollama's OpenAI server all speak this dialect. The key
arrives via a getter (Credential Manager) — it is never stored on this object
longer than client construction, and it is registered with the log scrubber.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
from openai import AsyncOpenAI

from marshmellow.core.contracts import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    ProviderCaps,
)

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        get_key: Callable[[], str | None],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        key = get_key()
        kwargs: dict[str, Any] = {"base_url": base_url, "api_key": key or "missing"}
        if transport is not None:
            kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        self._client = AsyncOpenAI(**kwargs)
        self._caps = ProviderCaps(vision=False, tool_call=True, json_schema=False)

    @property
    def capabilities(self) -> ProviderCaps:
        return self._caps

    async def complete(self, req: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message(m) for m in req.messages],
            "max_tokens": req.max_tokens,
        }
        if req.tools:
            kwargs["tools"] = req.tools
        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        tool_calls: list[LLMToolCall] = []
        for call in choice.message.tool_calls or []:
            raw = call.function.arguments or "{}"
            arguments: dict[str, object] = (
                json.loads(raw) if isinstance(raw, str) else dict(raw)
            )
            tool_calls.append(LLMToolCall(name=call.function.name, arguments=arguments))
        return LLMResponse(
            text=choice.message.content or "",
            tool_calls=tool_calls,
            model=response.model or self._model,
            stop_reason=choice.finish_reason,
        )

    @staticmethod
    def _message(m: LLMMessage) -> dict[str, object]:
        d: dict[str, object] = {"role": m.role, "content": m.content}
        if m.images:
            # Multimodal content parts (OpenAI dialect): text + one image_url part
            # per base64 PNG data-URL (vision fallback path, D5).
            parts: list[dict[str, object]] = [{"type": "text", "text": m.content}]
            parts.extend(
                {"type": "image_url", "image_url": {"url": image}} for image in m.images
            )
            d["content"] = parts
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            d["name"] = m.name
        return d
