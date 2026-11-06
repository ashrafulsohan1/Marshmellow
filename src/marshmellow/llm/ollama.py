"""Ollama local provider (D4) — the local leg of the §3.4 LLM abstraction.

One adapter over Ollama's NATIVE ``/api/chat`` (not its OpenAI-compat server):
that is the dialect where the D5 vision fallback and tool calls work without
the compat shim's quirks — images travel as a raw-base64 ``images`` list on the
message, and tool-call arguments are a JSON **object**, never OpenAI's string.
Every turn is exactly one ``POST {base_url}/api/chat`` with ``stream: false``;
non-2xx raises ``httpx.HTTPStatusError`` (the reliability layer's retryable
predicate consumes it — the provider itself never retries).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from marshmellow.core.contracts import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    ProviderCaps,
)

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

_DATA_URL_PREFIX = "data:image/png;base64,"

#: Local inference (often CPU-only) routinely exceeds httpx's 5 s default —
#: model load alone can take tens of seconds on the first turn.
DEFAULT_TIMEOUT_S = 120.0


class OllamaProvider:
    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        *,
        model: str,
        client: httpx.AsyncClient | None = None,  # MockTransport seam
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        # An injected client keeps its own settings; base_url only builds the
        # request URLs (tests inject MockTransport clients without one).
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(base_url=self._base_url, timeout=DEFAULT_TIMEOUT_S)
        )
        self._caps = ProviderCaps(vision=True, tool_call=True, json_schema=False)

    @property
    def capabilities(self) -> ProviderCaps:
        return self._caps

    async def complete(self, req: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message(m) for m in req.messages],
            "stream": False,
            "options": {"num_predict": req.max_tokens},
        }
        if req.tools:
            payload["tools"] = req.tools
        response = await self._client.post(f"{self._base_url}/api/chat", json=payload)
        response.raise_for_status()  # non-2xx → httpx.HTTPStatusError (retryable)
        data = _as_dict(response.json())
        message = _as_dict(data.get("message"))
        content = message.get("content")
        model = data.get("model")
        done_reason = data.get("done_reason")
        return LLMResponse(
            text=content if isinstance(content, str) else "",
            tool_calls=[
                self._tool_call(raw) for raw in (message.get("tool_calls") or [])
            ],
            model=model if isinstance(model, str) else self._model,
            stop_reason=done_reason if isinstance(done_reason, str) else None,
        )

    @staticmethod
    def _message(m: LLMMessage) -> dict[str, Any]:
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.images:
            # Ollama wants raw base64, not data-URLs (D5 vision path).
            d["images"] = [image.removeprefix(_DATA_URL_PREFIX) for image in m.images]
        if m.role == "assistant" and m.tool_calls:
            # Ollama dialect: arguments are an OBJECT — never OpenAI's JSON string.
            d["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.arguments}}
                for call in m.tool_calls
            ]
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            d["name"] = m.name
        return d

    @staticmethod
    def _tool_call(raw: Any) -> LLMToolCall:
        function = _as_dict(_as_dict(raw).get("function"))
        name = function.get("name")
        raw_args = function.get("arguments")
        # Arguments are documented as an object; older daemons string-encode
        # them — parse defensively (the planner needs a dict either way).
        if isinstance(raw_args, str):
            raw_args = json.loads(raw_args) if raw_args.strip() else {}
        arguments: dict[str, object] = (
            dict(raw_args) if isinstance(raw_args, dict) else {}
        )
        return LLMToolCall(name=name if isinstance(name, str) else "", arguments=arguments)


def _as_dict(value: Any) -> dict[str, Any]:
    """Narrow untyped JSON to a dict; anything else degrades to empty."""
    return value if isinstance(value, dict) else {}
