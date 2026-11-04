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
from marshmellow.planner.orchestrator import (
    PROMPT_VERSION,
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

    result = LLMPlanner(provider=llm).run(_instruction("open notepad"), execute)  # budget 12

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
    assert PROMPT_VERSION == "v1"
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
