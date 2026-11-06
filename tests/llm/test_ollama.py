"""Offline Ollama provider tests (httpx MockTransport, D4) + composition wiring.

The provider speaks Ollama's NATIVE `/api/chat` dialect: images travel as a
raw-base64 `images` list on the message, tool-call arguments are a JSON OBJECT
(never OpenAI's string), `stream:false`, one POST per turn. Non-2xx raises
`httpx.HTTPStatusError` (the retryable predicate upstream consumes it).
"""

import json
import logging

import httpx
import pytest

from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.core.contracts import LLMMessage, LLMRequest, LLMToolCall
from marshmellow.llm.ollama import DEFAULT_OLLAMA_URL, OllamaProvider
from marshmellow.planner.fake import FakePlanner
from marshmellow.planner.orchestrator import LLMPlanner


def _capture(status: int = 200, body: dict | None = None):
    """MockTransport handler that records every request, replies with `body`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status, json=body if body is not None else {"error": "boom"}, request=request
        )

    return seen, handler


def _provider(handler) -> OllamaProvider:
    return OllamaProvider(
        model="test-model", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _chat_body(content: str = "Opening Notepad.", tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "model": "test-model",
        "created_at": "2026-11-07T00:00:00Z",
        "message": message,
        "done": True,
        "done_reason": "stop",
    }


def test_ollama_caps_declare_vision_and_tools() -> None:
    caps = _provider(_capture()[1]).capabilities
    assert caps.vision is True
    assert caps.tool_call is True
    assert caps.json_schema is False


async def test_ollama_round_trip() -> None:
    """Scripted /api/chat JSON → LLMResponse; request speaks the Ollama dialect."""
    body = _chat_body(
        tool_calls=[{"function": {"name": "open_app", "arguments": {"target": "notepad"}}}]
    )
    seen, handler = _capture(body=body)
    provider = _provider(handler)

    req = LLMRequest(
        messages=[
            LLMMessage(role="system", content="you are marshmellow"),
            LLMMessage(
                role="user", content="find buttons", images=["data:image/png;base64,aGVsbG8="]
            ),
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[LLMToolCall(name="open_app", arguments={"target": "notepad"})],
            ),
            LLMMessage(role="tool", content="opened notepad"),
        ],
        tools=[{"type": "function", "function": {"name": "open_app"}}],
        max_tokens=512,
    )
    response = await provider.complete(req)

    # -- response mapping ------------------------------------------------------
    assert response.text == "Opening Notepad."
    assert response.model == "test-model"
    assert response.stop_reason == "stop"
    assert response.tool_calls[0].name == "open_app"
    assert response.tool_calls[0].arguments == {"target": "notepad"}  # object arrives as object

    # -- request mapping -------------------------------------------------------
    assert len(seen) == 1
    assert str(seen[0].url) == f"{DEFAULT_OLLAMA_URL}/api/chat"
    payload = json.loads(seen[0].read())
    assert payload["stream"] is False
    assert payload["model"] == "test-model"
    assert payload["options"]["num_predict"] == 512
    assert payload["tools"] == req.tools  # passed through verbatim
    assert payload["messages"][1]["images"] == ["aGVsbG8="]  # data-URL prefix stripped
    # assistant tool calls: arguments as an OBJECT, never OpenAI's JSON string
    assert payload["messages"][2]["tool_calls"] == [
        {"function": {"name": "open_app", "arguments": {"target": "notepad"}}}
    ]
    # tool results pass through verbatim
    assert payload["messages"][3] == {"role": "tool", "content": "opened notepad"}


async def test_ollama_vision_message() -> None:
    """Images map to Ollama's raw-base64 `images` list on the same message."""
    seen, handler = _capture(body=_chat_body(content="two buttons"))
    provider = _provider(handler)

    await provider.complete(
        LLMRequest(
            messages=[
                LLMMessage(
                    role="user",
                    content="what is on screen",
                    images=["data:image/png;base64,QUJD", "data:image/png;base64,REVG"],
                )
            ]
        )
    )

    payload = json.loads(seen[0].read())
    assert payload["messages"][0]["images"] == ["QUJD", "REVG"]
    assert payload["messages"][0]["content"] == "what is on screen"  # content stays a string


async def test_ollama_http_error_raises() -> None:
    seen, handler = _capture(status=500, body={"error": "model exploded"})
    provider = _provider(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await provider.complete(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        )
    assert len(seen) == 1  # no silent retry in the provider (Task 2 owns retries)


# -- composition: _build_providers wires the D4 planner + D5 vision provider ----


def _ollama_settings() -> Settings:
    settings = Settings()
    settings.providers.llm.kind = "ollama"
    settings.providers.llm.model = "test-model"
    return settings


def _headless_app(settings: Settings, tmp_path) -> App:
    return App(
        settings=settings,
        headless=True,
        log_dir=tmp_path,
        task_store_path=tmp_path / "tasks.json",
    )


def test_composition_ollama_kind_wires_provider_and_vision(tmp_path) -> None:
    app = _headless_app(_ollama_settings(), tmp_path)
    try:
        provider = app._providers.planner
        assert isinstance(provider, OllamaProvider)
        # the D5 vision fallback shares the vision-capable planner provider
        assert app._providers.vision is provider
        assert isinstance(app.planner, LLMPlanner)
    finally:
        app.stop()


def test_composition_fake_kind_has_no_vision(tmp_path) -> None:
    app = _headless_app(Settings(), tmp_path)
    try:
        assert app._providers.planner is None
        assert app._providers.vision is None  # never the fake planner
        assert isinstance(app.planner, FakePlanner)
    finally:
        app.stop()


def test_composition_ollama_without_model_falls_back_to_fake(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings()
    settings.providers.llm.kind = "ollama"  # model left empty
    with caplog.at_level(logging.WARNING):
        app = _headless_app(settings, tmp_path)
    try:
        assert app._providers.planner is None
        assert app._providers.vision is None
        assert isinstance(app.planner, FakePlanner)
        assert any("model" in r.getMessage() for r in caplog.records)
    finally:
        app.stop()
