"""Cloud LLM orchestrator v1 — the M2 brain (01-architecture §3.5).

Implements the M1 ``Planner`` protocol (``tasks.manager.Planner``): a blocking
``run(instruction, execute)`` that TaskManager hops to a worker thread. The
tool loop inside is asyncio so provider calls stay cancellable by the
wall-clock timeout; the sync facade marshals work onto ONE resident loop
thread via ``run_coroutine_threadsafe``. The resident loop matters: the
provider's pooled HTTP client is bound to the loop it first runs on, so a
fresh loop per call would intermittently fail with
``RuntimeError('Event loop is closed')`` on the second task.

M2 tool set: ``open_app`` · ``wait`` · ``ask_user`` · ``finish`` — offered as
JSON-schema function specs and mapped onto the executor seam as
``ActionRequest``s. No UI tools yet (M3): any other tool name is answered with
an error observation and never executed.

Verify-lite: a ``finish`` bundled into the same model turn as pending actions
is deferred — the model must see the fresh ``ACTION → RESULT`` observations
and then call ``finish`` alone, so the spoken summary reflects what actually
happened.

Task classification (§3.5) is behavioral: a reply produced without any screen
action is an *immediate reply*; the first shell/wait/ask action makes the task
*foreground*. The mode is logged, never spoken.

Tool results travel back as user-role messages ("ACTION … → RESULT …") instead
of role="tool": plain chat turns are valid on every OpenAI-compatible endpoint
(no orphan tool messages), so contracts and providers stay untouched.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging
import threading
from collections.abc import Callable, Sequence
from typing import Final

from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    Instruction,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    TaskResult,
)
from marshmellow.llm.base import LLMProvider

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]

MAX_STEPS: Final = 12
TIMEOUT_S: Final = 60.0
MAX_TOKENS: Final = 1024
PROMPT_VERSION: Final = "v1"

TIMEOUT_SUMMARY: Final = "Sorry — that was taking too long, so I stopped."
STEP_BUDGET_SUMMARY: Final = (
    "Sorry — that task needed too many steps, so I stopped before doing anything rash."
)
FAILURE_SUMMARY: Final = "Sorry — something went wrong on my side, so I stopped."

_FINISH = "finish"


def _tool(name: str, description: str, parameters: dict[str, object]) -> dict[str, object]:
    """OpenAI function-calling spec — JSON schema constrains every action."""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


_TOOL_NAMES: Final[tuple[str, ...]] = ("open_app", "wait", "ask_user", "finish")

M2_TOOLS: Final[tuple[dict[str, object], ...]] = (
    _tool(
        "open_app",
        "Open an installed application: a name or alias (notepad, calculator, explorer, "
        "terminal, browser), an ms-settings: URI, or a URL.",
        {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "App name, alias or URI"}},
            "required": ["target"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "wait",
        "Pause 50-5000 ms so a just-launched app can finish appearing before the next step.",
        {
            "type": "object",
            "properties": {"ms": {"type": "integer", "minimum": 50, "maximum": 5000}},
            "required": ["ms"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "ask_user",
        "Ask the user one short spoken clarifying question; their answer arrives as the "
        "tool result.",
        {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "One spoken sentence"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "finish",
        "End the task and speak the summary. Always finish after acting — never stop silently.",
        {
            "type": "object",
            "properties": {
                "ok": {
                    "type": "boolean",
                    "description": "true when the instruction was accomplished",
                },
                "summary_to_speak": {
                    "type": "string",
                    "description": "Up to two spoken sentences, no markdown",
                },
            },
            "required": ["ok", "summary_to_speak"],
            "additionalProperties": False,
        },
    ),
)


def load_system_prompt(version: str = PROMPT_VERSION) -> str:
    """Load a versioned orchestrator prompt from ``planner/prompts/`` (§3.5)."""
    ref = (
        importlib.resources.files("marshmellow")
        / "planner"
        / "prompts"
        / (f"orchestrator_{version}.md")
    )
    return ref.read_text(encoding="utf-8")


class LLMPlanner:
    """Tool-calling loop over an ``LLMProvider``; implements the M1 Planner protocol."""

    def __init__(
        self,
        provider: LLMProvider,
        tools: Sequence[dict[str, object]] | None = None,
        max_steps: int = MAX_STEPS,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._provider = provider
        self._tools: list[dict[str, object]] = list(M2_TOOLS if tools is None else tools)
        self._max_steps = max_steps
        self._timeout_s = timeout_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()

    # -- M1 Planner protocol (tasks.manager.Planner) ---------------------------

    def run(self, instruction: Instruction, execute: Executor) -> TaskResult:
        """Blocking entry point — TaskManager calls this on a worker thread."""
        if threading.current_thread() is self._loop_thread:
            raise RuntimeError("LLMPlanner.run() cannot be called from its own loop thread")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # normal path: a worker thread with no loop
        else:
            raise RuntimeError(
                "LLMPlanner.run() blocks; call it through TaskManager (asyncio.to_thread), "
                "never from a running event loop"
            )
        future = asyncio.run_coroutine_threadsafe(
            self._run(instruction, execute), self._ensure_loop()
        )
        return future.result()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """The one loop this planner ever uses.

        Providers hold pooled HTTP clients bound to the loop they first run on
        (AsyncOpenAI builds its client in __init__); a fresh loop per task
        would hand the pool a dead loop on the next call.
        """
        with self._loop_lock:
            if (
                self._loop is None
                or self._loop.is_closed()
                or (self._loop_thread is not None and not self._loop_thread.is_alive())
            ):
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="llm-planner-loop", daemon=True
                )
                thread.start()
                self._loop = loop
                self._loop_thread = thread
            assert self._loop is not None
            return self._loop

    # -- loop ------------------------------------------------------------------

    async def _run(self, instruction: Instruction, execute: Executor) -> TaskResult:
        try:
            return await asyncio.wait_for(
                self._tool_loop(instruction, execute), timeout=self._timeout_s
            )
        except TimeoutError:
            logger.warning("task timed out after %.1fs", self._timeout_s)
            return TaskResult(
                ok=False,
                summary_to_speak=TIMEOUT_SUMMARY,
                details=f"wall-clock timeout ({self._timeout_s:.0f}s)",
            )
        except Exception as exc:
            logger.exception("planner loop crashed")
            return TaskResult(ok=False, summary_to_speak=FAILURE_SUMMARY, details=repr(exc))

    async def _tool_loop(self, instruction: Instruction, execute: Executor) -> TaskResult:
        messages: list[LLMMessage] = self._opening_messages(instruction)
        trace: list[str] = []
        for _step in range(1, self._max_steps + 1):
            response = await self._provider.complete(
                LLMRequest(messages=list(messages), tools=self._tools, max_tokens=MAX_TOKENS)
            )
            if not response.tool_calls:
                return self._immediate_reply(response, trace)
            pending_finish: LLMToolCall | None = None
            acted = False
            for call in response.tool_calls:
                if call.name == _FINISH:
                    pending_finish = call
                    continue
                result = self._execute_tool(call, execute)
                trace.append(f"{call.name}: {result.observation}")
                messages.append(self._observation_message(call, result))
                acted = True
            if pending_finish is not None:
                if not acted:
                    return self._finish(pending_finish, trace)  # results already seen
                # verify-lite: the fresh results above must be reviewed first
                messages.append(
                    LLMMessage(
                        role="user",
                        content=(
                            "NOTE: your finish call was ignored — this round's tool results "
                            "were still pending above. Review them, then call finish alone."
                        ),
                    )
                )
        logger.warning("step budget exhausted (%d provider calls)", self._max_steps)
        return self._result(ok=False, summary=STEP_BUDGET_SUMMARY, trace=trace)

    # -- helpers ---------------------------------------------------------------

    def _result(
        self, *, ok: bool, summary: str, trace: list[str], details: str | None = None
    ) -> TaskResult:
        logger.info(
            "task classified: %s (%d executed action(s))",
            "foreground" if trace else "immediate_reply",
            len(trace),
        )
        return TaskResult(
            ok=ok,
            summary_to_speak=summary,
            details=details if details is not None else ("\n".join(trace) or None),
        )

    def _immediate_reply(self, response: LLMResponse, trace: list[str]) -> TaskResult:
        text = response.text.strip()
        if text:
            return self._result(ok=True, summary=text, trace=trace)
        return self._result(
            ok=False, summary="Sorry — I couldn't come up with an answer.", trace=trace
        )

    def _finish(self, call: LLMToolCall, trace: list[str]) -> TaskResult:
        raw_ok = call.arguments.get("ok", True)
        ok = raw_ok if isinstance(raw_ok, bool) else str(raw_ok).strip().lower() != "false"
        summary = str(call.arguments.get("summary_to_speak", "")).strip()
        if not summary:
            summary = "Done." if ok else "Sorry — I couldn't finish that."
        return self._result(ok=ok, summary=summary, trace=trace)

    def _execute_tool(self, call: LLMToolCall, execute: Executor) -> ActionResult:
        request = self._as_request(call)
        if request is None:
            return ActionResult(
                ok=False,
                observation=(
                    f"unknown or invalid tool call {call.name!r} — allowed tools: "
                    f"{', '.join(_TOOL_NAMES)}"
                ),
            )
        try:
            return execute(request)
        except Exception as exc:
            logger.exception("executor crashed for %s", call.name)
            return ActionResult(
                ok=False, observation="the action executor crashed", error=repr(exc)
            )

    @staticmethod
    def _as_request(call: LLMToolCall) -> ActionRequest | None:
        """Map a schema-constrained tool call onto the executor seam (or None)."""
        args = call.arguments
        if call.name == "open_app":
            target = args.get("target")
            if not isinstance(target, str) or not target.strip():
                return None
            return ActionRequest(
                kind="shell",
                payload={"open_app": target.strip()},
                rationale=f"orchestrator: open_app({target.strip()!r})",
                risk="L1",
            )
        if call.name == "wait":
            ms = args.get("ms")
            if not isinstance(ms, int) or not 50 <= ms <= 5000:
                return None
            return ActionRequest(
                kind="wait",
                payload={"ms": ms},
                rationale=f"orchestrator: wait({ms} ms)",
                risk="L0",
            )
        if call.name == "ask_user":
            question = args.get("question")
            if not isinstance(question, str) or not question.strip():
                return None
            return ActionRequest(
                kind="ask_user",
                payload={"question": question.strip()},
                rationale=f"orchestrator: ask_user({question.strip()!r})",
                risk="L0",
            )
        return None  # no UI tools yet (M3)

    @staticmethod
    def _observation_message(call: LLMToolCall, result: ActionResult) -> LLMMessage:
        outcome = (
            result.observation if result.ok else f"ERROR: {result.error or result.observation}"
        )
        args = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        return LLMMessage(role="user", content=f"ACTION {call.name}({args})\nRESULT: {outcome}")

    def _opening_messages(self, instruction: Instruction) -> list[LLMMessage]:
        return [
            LLMMessage(role="system", content=load_system_prompt()),
            LLMMessage(role="user", content=self._user_content(instruction)),
        ]

    @staticmethod
    def _user_content(instruction: Instruction) -> str:
        ctx = instruction.context
        lines = [f'The user said: "{instruction.text}"']
        if ctx.active_window is not None:
            lines.append(f"Active window: {ctx.active_window.app} — {ctx.active_window.title}")
        lines.append(f"Local time: {ctx.time.strftime('%Y-%m-%d %H:%M %A')}")
        if ctx.clipboard is not None:
            lines.append(f"Clipboard: {ctx.clipboard}")
        return "\n".join(lines)
