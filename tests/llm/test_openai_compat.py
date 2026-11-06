"""Offline provider tests (httpx MockTransport), BYOK secrets, log-scrub guarantee."""

import json
from pathlib import Path

import httpx
import pytest

from marshmellow.core import logging as mlog
from marshmellow.core.contracts import LLMMessage, LLMRequest
from marshmellow.core.logging import setup_logging
from marshmellow.core.secrets import load_api_key, store_api_key
from marshmellow.llm.openai_compat import OpenAICompatProvider


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, password: str) -> None:
        self.store[(service, account)] = password

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))


@pytest.fixture(autouse=True)
def _fresh_logging():
    mlog._reset_for_tests()
    yield
    mlog._reset_for_tests()


def test_secrets_roundtrip() -> None:
    fake = FakeKeyring()
    store_api_key("abc-123", backend=fake)
    assert load_api_key(backend=fake) == "abc-123"
    assert load_api_key(backend=FakeKeyring()) is None


def test_api_key_never_reaches_logs(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    key = "super-secret-key-xyz"
    store_api_key(key, backend=FakeKeyring())  # registers with the scrubber
    logger.error("auth failed for %s — retrying", key)
    content = "\n".join(
        f.read_text(encoding="utf-8") for f in tmp_path.glob("session-*.jsonl")
    )
    assert key not in content
    assert "[REDACTED]" in content


def _tool_call_response(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer test-key-123"
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "open_app",
                                "arguments": json.dumps({"target": "notepad"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    return httpx.Response(200, json=body, request=request)


async def test_golden_tool_call_round_trip() -> None:
    provider = OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        model="test-model",
        get_key=lambda: "test-key-123",
        transport=httpx.MockTransport(_tool_call_response),
    )
    response = await provider.complete(
        LLMRequest(
            messages=[LLMMessage(role="user", content="open notepad")],
            tools=[{"type": "function", "function": {"name": "open_app"}}],
        )
    )
    assert response.stop_reason == "tool_calls"
    assert response.model == "test-model"
    assert response.tool_calls[0].name == "open_app"
    assert response.tool_calls[0].arguments == {"target": "notepad"}
    assert response.text == ""


async def test_images_map_to_image_url_content_parts() -> None:
    seen: list[httpx.Request] = []

    def plain_response(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {
            "id": "chatcmpl-2",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
        }
        return httpx.Response(200, json=body, request=request)

    provider = OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        model="vision-model",
        get_key=lambda: "test-key-123",
        transport=httpx.MockTransport(plain_response),
    )
    data_url = "data:image/png;base64,aGVsbG8gcHluZw=="
    await provider.complete(
        LLMRequest(
            messages=[LLMMessage(role="user", content="find buttons", images=[data_url])]
        )
    )

    assert len(seen) == 1
    payload = json.loads(seen[0].read())
    content = payload["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "find buttons"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == data_url


async def test_vision_flag_sets_caps() -> None:
    """M5 T1: `vision=True` advertises the D5 fallback through the caps seam."""
    provider = OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        model="vision-model",
        get_key=lambda: "test-key-123",
        transport=httpx.MockTransport(_tool_call_response),
        vision=True,
    )
    assert provider.capabilities.vision is True
    assert provider.capabilities.tool_call is True


async def test_vision_flag_defaults_off() -> None:
    provider = OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        model="m",
        get_key=lambda: "k",
        transport=httpx.MockTransport(_tool_call_response),
    )
    assert provider.capabilities.vision is False


async def test_message_without_images_stays_a_plain_string() -> None:
    seen: list[httpx.Request] = []

    def plain_response(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c3",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
            request=request,
        )

    provider = OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        model="m",
        get_key=lambda: "k",
        transport=httpx.MockTransport(plain_response),
    )
    await provider.complete(
        LLMRequest(messages=[LLMMessage(role="user", content="just text")])
    )

    payload = json.loads(seen[0].read())
    assert payload["messages"][0]["content"] == "just text"
