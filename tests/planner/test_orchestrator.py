"""Golden traces for the cloud LLM orchestrator v1 (M2 Task 7) — offline, scripted provider.

(a) one-shot shell action → finish · (b) ask_user round trip · (c) max-steps
safety cut · (d) provider exception → spoken failure, task marked failed.
Interface extras: immediate_reply vs foreground classification, verify-lite
(the model must see results before finishing), wall-clock timeout, wait
mapping, and the no-UI-tools guarantee.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    ContextSnapshot,
    Instruction,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    ProviderCaps,
    Transcript,
    WindowInfo,
)
from marshmellow.llm.base import ProviderUnavailableError
from marshmellow.planner.orchestrator import (
    M2_TOOLS,
    M3_TOOLS,
    PROMPT_VERSION,
    PROVIDER_UNAVAILABLE_SUMMARY,
    LLMPlanner,
    load_system_prompt,
)

M2_TOOL_NAMES = {"open_app", "wait", "ask_user", "finish"}


class ScriptedLLM:
    """Fake LLMProvider: replays scripted LLMResponses, records every request.

    repeat_last=True replays the single scripted response forever (drives the
    max-steps cut); fail_on_call raises `error` from that call onwards;
    delay simulates provider latency for the wall-clock timeout.
    """

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        repeat_last: bool = False,
        fail_on_call: int | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.requests: list[LLMRequest] = []
        self._responses = list(responses)
        self._repeat_last = repeat_last
        self._fail_on_call = fail_on_call
        self._error = error or RuntimeError("provider exploded")
        self._delay = delay

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(vision=False, tool_call=True, json_schema=False)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail_on_call is not None and len(self.requests) >= self._fail_on_call:
            raise self._error
        if not self._responses:
            raise AssertionError("ScriptedLLM script exhausted")
        if self._repeat_last and len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


def _instruction(text: str, window: WindowInfo | None = None) -> Instruction:
    return Instruction(
        text=text,
        transcript=Transcript(text=text, source="fixture"),
        context=ContextSnapshot(active_window=window),
    )


def _call(name: str, **arguments: object) -> LLMToolCall:
    return LLMToolCall(name=name, arguments=dict(arguments))


def _executor(observation: str = "window appeared", ok: bool = True):
    executed: list[ActionRequest] = []

    def execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        if req.kind == "ask_user":
            return ActionResult(ok=True, observation="notepad")
        return ActionResult(ok=ok, observation=observation)

    return execute, executed


def _offered_names(req: LLMRequest) -> set[str]:
    return {str(tool["function"]["name"]) for tool in req.tools}


class LoopBoundLLM:
    """Mimics a pooled HTTP client's loop affinity — the committed OpenAICompatProvider
    builds one long-lived AsyncOpenAI/httpx2 client in __init__, so reusing it on a
    different event loop raises RuntimeError('Event loop is closed') exactly like this.
    """

    def __init__(self) -> None:
        self.loops: list[asyncio.AbstractEventLoop] = []

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(vision=False, tool_call=True, json_schema=False)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        loop = asyncio.get_running_loop()
        if any(seen is not loop for seen in self.loops):
            raise RuntimeError("Event loop is closed")
        self.loops.append(loop)
        return LLMResponse(text="Done.", stop_reason="stop")


def test_sequential_tasks_share_one_resident_loop() -> None:
    llm = LoopBoundLLM()
    execute, executed = _executor()
    planner = LLMPlanner(provider=llm)

    first = planner.run(_instruction("hello one"), execute)
    second = planner.run(_instruction("hello two"), execute)

    assert first.ok and second.ok  # pre-fix: second died on the first loop's connection
    assert len(llm.loops) == 2
    assert llm.loops[0] is llm.loops[1]
    assert executed == []


def test_concurrent_tasks_share_one_resident_loop() -> None:
    llm = LoopBoundLLM()
    execute, _ = _executor()
    planner = LLMPlanner(provider=llm)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(planner.run, _instruction(f"hello {i}"), execute) for i in range(2)]
        results = [f.result(timeout=10) for f in futures]

    assert all(r.ok for r in results)
    assert len(llm.loops) == 2
    assert all(loop is llm.loops[0] for loop in llm.loops)


async def test_run_rejected_inside_event_loop() -> None:
    llm = ScriptedLLM([LLMResponse(text="Done.", stop_reason="stop")])
    planner = LLMPlanner(provider=llm)
    execute, _ = _executor()

    with pytest.raises(RuntimeError, match="blocks"):
        planner.run(_instruction("hello"), execute)


def test_one_shot_shell_action_then_finish() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[_call("open_app", target="notepad")], stop_reason="tool_calls"),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Opened Notepad.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("open notepad"), execute)

    assert len(executed) == 1
    assert executed[0].kind == "shell"
    assert executed[0].payload == {"open_app": "notepad"}
    assert executed[0].risk == "L1"
    assert result.ok
    assert result.summary_to_speak == "Opened Notepad."
    # verify-lite: the observation reached the model's context before finish
    assert len(llm.requests) == 2
    assert "window appeared" in llm.requests[1].messages[-1].content
    # JSON-schema tool set: exactly the M2 tools — no UI tools
    assert _offered_names(llm.requests[0]) == M2_TOOL_NAMES


def test_ask_user_round_trip_feeds_answer_back() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[_call("ask_user", question="Which app should I open?")],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Opened Notepad.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("open it"), execute)

    assert len(executed) == 1
    assert executed[0].kind == "ask_user"
    assert executed[0].payload == {"question": "Which app should I open?"}
    assert result.ok
    assert result.summary_to_speak == "Opened Notepad."
    # the user's answer reached the model before it finished
    assert "notepad" in llm.requests[1].messages[-1].content


def test_max_steps_safety_cut() -> None:
    llm = ScriptedLLM(
        [LLMResponse(tool_calls=[_call("open_app", target="notepad")], stop_reason="tool_calls")],
        repeat_last=True,
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm, max_steps=12).run(_instruction("open notepad"), execute)

    assert not result.ok
    assert result.summary_to_speak  # speaks a failure, never hangs
    assert len(llm.requests) == 12
    assert len(executed) == 12


def test_provider_exception_marks_task_failed() -> None:
    llm = ScriptedLLM([], fail_on_call=1, error=RuntimeError("connection reset by peer"))
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("open notepad"), execute)

    assert not result.ok
    assert result.summary_to_speak  # spoken failure
    assert executed == []
    assert "connection reset" in (result.details or "")


def test_provider_unavailable_speaks_distinct_summary() -> None:
    """M5 T2: every provider exhausted → the unavailable summary, not the generic one."""
    llm = ScriptedLLM([], fail_on_call=1, error=ProviderUnavailableError("all providers failed"))
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("open notepad"), execute)

    assert not result.ok
    assert result.summary_to_speak == PROVIDER_UNAVAILABLE_SUMMARY
    assert executed == []
    assert "all providers failed" in (result.details or "")


def test_classification_immediate_reply_vs_foreground(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        reply_llm = ScriptedLLM(
            [LLMResponse(text="The capital of France is Paris.", stop_reason="stop")]
        )
        execute, executed = _executor()
        result = LLMPlanner(provider=reply_llm).run(
            _instruction("what is the capital of france"), execute
        )
        assert result.ok
        assert result.summary_to_speak == "The capital of France is Paris."
        assert executed == []  # immediate replies never touch the screen
        assert "immediate_reply" in caplog.text

        action_llm = ScriptedLLM(
            [
                LLMResponse(
                    tool_calls=[_call("open_app", target="notepad")], stop_reason="tool_calls"
                ),
                LLMResponse(
                    tool_calls=[_call("finish", ok=True, summary_to_speak="Opened Notepad.")],
                    stop_reason="tool_calls",
                ),
            ]
        )
        execute2, executed2 = _executor()
        result2 = LLMPlanner(provider=action_llm).run(_instruction("open notepad"), execute2)
        assert result2.ok
        assert executed2  # a screen action ran → foreground
        assert "foreground" in caplog.text


def test_wall_clock_timeout_cuts_slow_provider() -> None:
    llm = ScriptedLLM(
        [LLMResponse(tool_calls=[_call("open_app", target="notepad")], stop_reason="tool_calls")],
        repeat_last=True,
        delay=0.2,
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm, timeout_s=0.05).run(_instruction("open notepad"), execute)

    assert not result.ok
    assert "too long" in result.summary_to_speak.lower()
    assert executed == []  # cut before any action ran


def test_wait_tool_maps_to_wait_action() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[_call("wait", ms=250)], stop_reason="tool_calls"),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Done.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("wait a moment"), execute)

    assert executed[0].kind == "wait"
    assert executed[0].payload == {"ms": 250}
    assert executed[0].risk == "L0"
    assert result.ok
    assert result.summary_to_speak == "Done."


def test_ui_tools_do_not_exist_yet() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[_call("type_text", text="hello")], stop_reason="tool_calls"),
            LLMResponse(
                tool_calls=[_call("finish", ok=False, summary_to_speak="I can't type text yet.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("type hello in notepad"), execute)

    assert executed == []  # no UI tools in M2 — nothing reached the executor
    fed_back = llm.requests[1].messages[-1].content
    assert "type_text" in fed_back and "ERROR" in fed_back  # model is told, not crashed
    assert not result.ok
    assert result.summary_to_speak == "I can't type text yet."


def test_finish_is_deferred_until_actions_are_observed() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    _call("open_app", target="notepad"),
                    _call("finish", ok=True, summary_to_speak="Opened."),
                ],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Opened Notepad.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("open notepad"), execute)

    assert len(executed) == 1  # the bundled finish never bypassed verification
    assert len(llm.requests) == 2
    second_round = "\n".join(m.content for m in llm.requests[1].messages)
    assert "window appeared" in second_round
    assert result.summary_to_speak == "Opened Notepad."  # came from the second finish


def test_system_prompt_is_versioned_and_wired() -> None:
    prompt = load_system_prompt()
    assert prompt.strip()
    # M3 Task 5 bumped the orchestrator prompt to v2 (perceive/act/verify tools)
    assert PROMPT_VERSION == "v2"
    for tool in sorted(M2_TOOL_NAMES):
        assert tool in prompt

    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Done.")],
                stop_reason="tool_calls",
            )
        ]
    )
    window = WindowInfo(hwnd=1, title="orchestrator.py — Marshmellow", app="Code")
    execute, _ = _executor()
    LLMPlanner(provider=llm).run(_instruction("open notepad", window=window), execute)

    first = llm.requests[0]
    assert first.messages[0].role == "system"
    assert first.messages[0].content == prompt
    user_msg = first.messages[1].content
    assert "open notepad" in user_msg  # the instruction text
    assert "Code" in user_msg  # context: active window app


# -- M4 Task 4: create_task — the LLM registers background tasks -----------------


def _spec_names(tools: tuple[dict[str, object], ...]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(str(function["name"]))
    return names


def _find_spec(name: str) -> dict[object, object]:
    for tool in M3_TOOLS:
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") == name:
            return function
    raise AssertionError(f"{name} not offered by M3_TOOLS")


def test_create_task_tool_joins_the_m3_set_only() -> None:
    assert "create_task" in _spec_names(M3_TOOLS)
    assert "create_task" not in _spec_names(M2_TOOLS)  # M2 traces stay unchanged

    spec = _find_spec("create_task")
    parameters = spec["parameters"]
    assert isinstance(parameters, dict)
    properties = parameters["properties"]
    assert isinstance(properties, dict)
    assert parameters["required"] == ["instruction", "mode"]
    mode = properties["mode"]
    assert isinstance(mode, dict)
    assert mode["enum"] == ["background_monitor", "background_job", "scheduled"]
    monitor = properties["monitor"]
    assert isinstance(monitor, dict)
    monitor_properties = monitor["properties"]
    assert isinstance(monitor_properties, dict)
    kind = monitor_properties["kind"]
    assert isinstance(kind, dict)
    assert kind["enum"] == ["url_diff", "api_field", "file_hash", "rss"]
    interval = monitor_properties["interval_s"]
    assert isinstance(interval, dict)
    assert interval["minimum"] == 30  # no hot loops from the model (M4 pinned)


@pytest.mark.parametrize(
    ("arguments", "expected_payload"),
    [
        (
            {
                "instruction": "watch this channel and tell me when they upload",
                "mode": "background_monitor",
                "monitor": {
                    "kind": "rss",
                    "target": "https://example.com/feed.xml",
                    "interval_s": 300,
                    "condition": "a new upload",
                },
            },
            {
                "instruction": "watch this channel and tell me when they upload",
                "mode": "background_monitor",
                "monitor": {
                    "kind": "rss",
                    "target": "https://example.com/feed.xml",
                    "interval_s": 300,
                    "condition": "a new upload",
                },
            },
        ),
        (
            {
                "instruction": "open calculator in an hour",
                "mode": "scheduled",
                "schedule": "2026-11-06T13:00:00",
            },
            {
                "instruction": "open calculator in an hour",
                "mode": "scheduled",
                "schedule": "2026-11-06T13:00:00",
            },
        ),
    ],
    ids=["monitor-mode", "scheduled-mode"],
)
def test_create_task_call_maps_to_the_request_payload_intact(
    arguments: dict[str, object], expected_payload: dict[str, object]
) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[_call("create_task", **arguments)], stop_reason="tool_calls"
            ),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Watching it.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("watch this channel"), execute)

    assert len(executed) == 1
    req = executed[0]
    assert req.kind == "create_task"
    assert req.payload == expected_payload  # monitor dict / schedule string intact
    assert req.risk == "L1"
    assert result.ok
    # verify-lite: the registration result reached the model before finish
    assert "window appeared" in llm.requests[1].messages[-1].content


@pytest.mark.parametrize(
    "arguments",
    [
        {"mode": "background_monitor"},  # instruction missing
        {"instruction": "watch", "mode": "yesterday"},  # mode outside the enum
        {"instruction": "watch", "mode": "background_monitor", "monitor": "notepad"},
    ],
    ids=["no-instruction", "bad-mode", "monitor-not-an-object"],
)
def test_create_task_bad_args_are_invalid_observations(arguments: dict[str, object]) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[_call("create_task", **arguments)], stop_reason="tool_calls"
            ),
            LLMResponse(
                tool_calls=[
                    _call("finish", ok=False, summary_to_speak="I couldn't set that up.")
                ],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(provider=llm).run(_instruction("watch something"), execute)

    assert executed == []  # nothing reached the executor
    fed_back = llm.requests[1].messages[-1].content
    assert "ERROR" in fed_back and "invalid" in fed_back  # the model is told, not crashed
    assert not result.ok
