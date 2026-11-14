"""Offline Ollama provider tests (httpx MockTransport, D4) + composition wiring.

The provider speaks Ollama's NATIVE `/api/chat` dialect: images travel as a
raw-base64 `images` list on the message, tool-call arguments are a JSON OBJECT
(never OpenAI's string), `stream:false`, one POST per turn. Non-2xx raises
`httpx.HTTPStatusError` (the retryable predicate upstream consumes it).

One test uses a REAL loopback HTTP server instead of MockTransport: the pool
is exactly what MockTransport bypasses, and the loop-binding defect (M5 T1
review fix) lived in the pool.
"""

import asyncio
import json
import logging
import threading
from collections.abc import Iterator

import httpx
import pytest

from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.core.contracts import LLMMessage, LLMRequest, LLMToolCall
from marshmellow.llm.ollama import DEFAULT_OLLAMA_URL, OllamaProvider
from marshmellow.llm.openai_compat import OpenAICompatProvider
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


def test_composition_ollama_kind_wires_dedicated_vision_provider(tmp_path) -> None:
    """M5 T1 review fix: vision gets its OWN provider instance, never the planner's.

    The planner drives its provider from the resident llm-planner-loop while
    VisionPerceiver drives its own from the vision-provider-loop — and httpx
    pools are loop-bound, so one shared instance would fail the second user's
    first pooled call (the D5 fallback would never survive a planner turn).
    """
    app = _headless_app(_ollama_settings(), tmp_path)
    try:
        planner = app._providers.planner
        assert isinstance(planner, OllamaProvider)
        vision = app._providers.vision
        assert isinstance(vision, OllamaProvider)
        assert vision is not planner  # dedicated client/pool per resident loop
        # the perceiver actually got the dedicated instance, not the planner's
        assert app.perception._vision is not None
        assert app.perception._vision._provider is vision
        assert isinstance(app.planner, LLMPlanner)
    finally:
        app.stop()


def test_composition_openai_compat_vision_wires_dedicated_instance(tmp_path) -> None:
    """The dedicated-instance rule holds for the cloud vision fallback too."""
    settings = Settings()
    settings.providers.llm.kind = "openai_compat"
    settings.providers.llm.base_url = "https://api.example.com/v1"
    settings.providers.llm.model = "vision-model"
    settings.providers.llm.vision = True
    app = _headless_app(settings, tmp_path)
    try:
        planner = app._providers.planner
        assert isinstance(planner, OpenAICompatProvider)
        vision = app._providers.vision
        assert isinstance(vision, OpenAICompatProvider)
        assert vision is not planner
        assert vision.capabilities.vision is True
        assert app.perception._vision is not None
        assert app.perception._vision._provider is vision
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


# -- the loop-binding mechanism itself (real sockets; MockTransport can't see it)


@pytest.fixture()
def chat_server() -> Iterator[str]:
    """A local keep-alive /api/chat server — the POOL is what binds to a loop."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = json.dumps(
        {
            "model": "test-model",
            "created_at": "2026-11-07T00:00:00Z",
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
            "done_reason": "stop",
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: the pool actually pools

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    class QuietServer(ThreadingHTTPServer):
        # The provider's pooled connections are closed when the test closes
        # its resident loops (shutdown-hygiene fix below), so handler threads
        # mid-readline() see a connection reset — expected teardown noise,
        # not a server bug; print nothing.
        def handle_error(self, request, client_address) -> None:
            pass

    server = QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _resident_loop(name: str) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Mimics orchestrator._ensure_loop / vision._ensure_loop: one alive loop."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name=name, daemon=True)
    thread.start()
    return loop, thread


def test_one_provider_instance_cannot_serve_two_resident_loops(chat_server) -> None:
    """The mechanism behind the M5 T1 review fix, at real-socket level.

    One shared provider instance: the planner loop's first turn binds the
    pool, and the vision loop's first turn then hangs or raises (observed
    shapes: pool deadlock → TimeoutError, or RuntimeError "bound to a
    different event loop"). One DEDICATED instance per loop works — which is
    what _build_providers must keep building.
    """
    req = LLMRequest(messages=[LLMMessage(role="user", content="hi")])

    async def one_turn(provider: OllamaProvider) -> str:
        return (await provider.complete(req)).text

    planner_loop, planner_thread = _resident_loop("test-planner-loop")
    vision_loop, vision_thread = _resident_loop("test-vision-loop")
    try:
        shared = OllamaProvider(base_url=chat_server, model="test-model")
        first = asyncio.run_coroutine_threadsafe(one_turn(shared), planner_loop)
        assert first.result(30) == "ok"  # planner's first HTTP turn binds the pool

        second = asyncio.run_coroutine_threadsafe(one_turn(shared), vision_loop)
        with pytest.raises((RuntimeError, TimeoutError)):
            second.result(10)  # pool bound to the planner loop → hang or loop error

        # the composition fix: a dedicated instance per resident loop
        vision_provider = OllamaProvider(base_url=chat_server, model="test-model")
        replan = asyncio.run_coroutine_threadsafe(one_turn(shared), planner_loop)
        assert replan.result(30) == "ok"  # planner loop still healthy
        on_vision = asyncio.run_coroutine_threadsafe(one_turn(vision_provider), vision_loop)
        assert on_vision.result(30) == "ok"  # dedicated instance serves the vision loop
    finally:
        # Stop the resident loops AND close them. A stopped-but-unclosed
        # proactor loop is only close()d from BaseEventLoop.__del__ at some
        # later GC pass, and on Windows that finalizer can park forever in
        # IocpProactor._poll waiting for cancelled overlapped ops to report —
        # the v0.1.2 cleanroom's run 1 of 3 hung >20 min at [100%] exactly
        # there (docs/plan/CLEANROOM-VERIFY-v012.md §2).
        for loop, thread in ((planner_loop, planner_thread), (vision_loop, vision_thread)):
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            if not loop.is_running():
                loop.close()
