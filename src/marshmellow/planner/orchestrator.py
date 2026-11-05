"""Cloud LLM orchestrator v2 — the M3 brain (01-architecture §3.5).

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
``ActionRequest``s. With a ``toolbox`` (``planner.tools.PlannerTools``) the M3
perceive/act/verify set is offered instead: ``open_app`` · ``get_elements`` ·
``screenshot`` · ``act`` · ``wait`` · ``ask_user`` · ``finish``. ``open_app``
stays as M2 sugar so M2 traces remain green; ``get_elements``/``screenshot``
are perception reads that call the toolbox directly (NOT executor actions);
``act`` maps onto an ``ActionRequest`` whose declared ``risk`` is carried but
re-classified by the safety gate at the executor seam (§3.13).

Verify loop (§3.5): a ``finish`` bundled into the same model turn as pending
actions is deferred (M2 verify-lite) — the model must see the fresh
``ACTION → RESULT`` observations and then call ``finish`` alone. On top of
that, failed / same-observation acts are tracked per target ref: 3 consecutive
ones inject the escalation user-message telling the model to ask the user or
finish(ok=false) instead of hammering the same element.

Screenshot purge (§3.15): at task end (finally) the toolbox's screenshot /
marked-image files are deleted, success, failure or timeout alike.

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
from typing import Final, cast, get_args

from marshmellow.core.contracts import (
    ActionKind,
    ActionRequest,
    ActionResult,
    Instruction,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    RiskLevel,
    TaskResult,
)
from marshmellow.llm.base import LLMProvider
from marshmellow.planner.tools import ArtifactPurger, PlannerTools

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]

MAX_STEPS: Final = 25
TIMEOUT_S: Final = 180.0
MAX_TOKENS: Final = 1024
PROMPT_VERSION: Final = "v2"

TIMEOUT_SUMMARY: Final = "Sorry — that was taking too long, so I stopped."
STEP_BUDGET_SUMMARY: Final = (
    "Sorry — that task needed too many steps, so I stopped before doing anything rash."
)
FAILURE_SUMMARY: Final = "Sorry — something went wrong on my side, so I stopped."

ESCALATION_MESSAGE: Final = (
    "verification failed 3× on this element — ask the user or finish(ok=false)"
)

_FINISH = "finish"

# Perception reads ride the toolbox and are NOT executor actions: they never
# produce an ActionRequest and never defer a bundled finish (§3.5 verify duty
# is about acts).
_TOOLBOX_TOOLS: Final[frozenset[str]] = frozenset({"get_elements", "screenshot", "act"})
_READ_TOOLS: Final[frozenset[str]] = frozenset({"get_elements", "screenshot"})

_ACTION_KINDS: Final[frozenset[str]] = frozenset(get_args(ActionKind))
_RISK_LEVELS: Final[frozenset[str]] = frozenset(get_args(RiskLevel))


def _tool(name: str, description: str, parameters: dict[str, object]) -> dict[str, object]:
    """OpenAI function-calling spec — JSON schema constrains every action."""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _spec_name(tool: dict[str, object]) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str):
            return name
    return ""


_OPEN_APP_TOOL: Final[dict[str, object]] = _tool(
    "open_app",
    "Open an installed application: a name or alias (notepad, calculator, explorer, "
    "terminal, browser), an ms-settings: URI, or a URL.",
    {
        "type": "object",
        "properties": {"target": {"type": "string", "description": "App name, alias or URI"}},
        "required": ["target"],
        "additionalProperties": False,
    },
)
_WAIT_TOOL: Final[dict[str, object]] = _tool(
    "wait",
    "Pause 50-5000 ms so a just-launched app can finish appearing before the next step.",
    {
        "type": "object",
        "properties": {"ms": {"type": "integer", "minimum": 50, "maximum": 5000}},
        "required": ["ms"],
        "additionalProperties": False,
    },
)
_ASK_USER_TOOL: Final[dict[str, object]] = _tool(
    "ask_user",
    "Ask the user one short spoken clarifying question; their answer arrives as the "
    "tool result.",
    {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "One spoken sentence"}},
        "required": ["question"],
        "additionalProperties": False,
    },
)
_FINISH_TOOL: Final[dict[str, object]] = _tool(
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
)

M2_TOOLS: Final[tuple[dict[str, object], ...]] = (
    _OPEN_APP_TOOL,
    _WAIT_TOOL,
    _ASK_USER_TOOL,
    _FINISH_TOOL,
)

_GET_ELEMENTS_TOOL: Final[dict[str, object]] = _tool(
    "get_elements",
    "Read the interactive elements of the active window as a compact ref list, e.g. "
    "e12 Button 'Play' (920,540 60x24) [invoke]. Call this before acting: refs are "
    "valid only from the LATEST list. The optional query narrows what to look for.",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look for"}},
        "additionalProperties": False,
    },
)
_SCREENSHOT_TOOL: Final[dict[str, object]] = _tool(
    "screenshot",
    "Get the path of a marked screenshot (grid-annotated) of the active window for "
    "surfaces the element list cannot describe. Empty result when none is available.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
_ACT_TOOL: Final[dict[str, object]] = _tool(
    "act",
    "Perform ONE UI action on an element from the latest get_elements list. payload "
    "carries kind-specific arguments (type_text/uia_set_value: {'text': ...}; hotkey: "
    "{'combo': 'ctrl+s'}; scroll: {'lines': n}). risk is ADVISORY only: the safety "
    "gate re-classifies every action and may refuse it or ask the user to confirm "
    "before it runs.",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(_ACTION_KINDS)},
            "target_ref": {
                "type": "string",
                "description": "Element ref (e.g. 'e12') from the latest get_elements list",
            },
            "payload": {
                "type": "object",
                "description": "Kind-specific arguments",
            },
            "rationale": {"type": "string", "description": "Why this action, one phrase"},
            "risk": {
                "type": "string",
                "enum": ["L0", "L1", "L2", "L3"],
                "description": "Advisory risk guess; the gate decides",
            },
        },
        "required": ["kind", "rationale"],
        "additionalProperties": False,
    },
)

M3_TOOLS: Final[tuple[dict[str, object], ...]] = (
    _OPEN_APP_TOOL,
    _GET_ELEMENTS_TOOL,
    _SCREENSHOT_TOOL,
    _ACT_TOOL,
    _WAIT_TOOL,
    _ASK_USER_TOOL,
    _FINISH_TOOL,
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


class _VerifyTracker:
    """Failed / same-observation acts per target ref (§3.5 verify loop).

    An act counts against its ref when it FAILED or when its observation is
    identical to the previous act on that ref (nothing changed on screen).
    Three consecutive ones cross the escalation threshold; the counter resets
    after an escalation so a persistent failure re-escalates, not spams.
    """

    THRESHOLD: Final = 3

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._last_observation: dict[str, str] = {}

    def observe(self, ref: str | None, result: ActionResult) -> bool:
        """Record one act result; True when the escalation threshold is crossed."""
        if ref is None:
            return False
        unchanged = result.observation == self._last_observation.get(ref)
        failed_or_same = not result.ok or unchanged
        count = self._counts.get(ref, 0) + 1 if failed_or_same else 0
        self._counts[ref] = count
        self._last_observation[ref] = result.observation
        if count >= self.THRESHOLD:
            self._counts[ref] = 0
            return True
        return False


class LLMPlanner:
    """Tool-calling loop over an ``LLMProvider``; implements the M1 Planner protocol.

    With ``toolbox`` (a ``PlannerTools``) the M3 perceive/act/verify tool set is
    offered and the toolbox carries get_elements/screenshot/act; without one the
    M2 set is offered unchanged so M2 traces stay green.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: Sequence[dict[str, object]] | None = None,
        toolbox: PlannerTools | None = None,
        max_steps: int = MAX_STEPS,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._provider = provider
        self._toolbox: PlannerTools | None = toolbox
        if tools is not None:
            self._tools: list[dict[str, object]] = list(tools)
        else:
            self._tools = list(M3_TOOLS if toolbox is not None else M2_TOOLS)
        self._tool_names: tuple[str, ...] = tuple(_spec_name(t) for t in self._tools)
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
        finally:
            self._purge_artifacts()

    def _purge_artifacts(self) -> None:
        """§3.15: screenshots live only while the task is active — purge at task end."""
        toolbox = self._toolbox
        if toolbox is None or not isinstance(toolbox, ArtifactPurger):
            return
        try:
            toolbox.purge()
        except Exception:
            logger.exception("screenshot purge failed")

    async def _tool_loop(self, instruction: Instruction, execute: Executor) -> TaskResult:
        messages: list[LLMMessage] = self._opening_messages(instruction)
        trace: list[str] = []
        verify = _VerifyTracker()
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
                if verify.observe(self._acted_ref(call), result):
                    messages.append(LLMMessage(role="user", content=ESCALATION_MESSAGE))
                acted = acted or call.name not in _READ_TOOLS
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
        if self._toolbox is not None and call.name in _TOOLBOX_TOOLS:
            return self._run_toolbox_tool(call)
        request = self._as_request(call)
        if request is None:
            return ActionResult(
                ok=False,
                observation=(
                    f"unknown or invalid tool call {call.name!r} — allowed tools: "
                    f"{', '.join(self._tool_names)}"
                ),
            )
        try:
            return execute(request)
        except Exception as exc:
            logger.exception("executor crashed for %s", call.name)
            return ActionResult(
                ok=False, observation="the action executor crashed", error=repr(exc)
            )

    def _run_toolbox_tool(self, call: LLMToolCall) -> ActionResult:
        """get_elements/screenshot/act through the toolbox; failures are observations."""
        toolbox = self._toolbox
        if toolbox is None:  # only reachable when the offered tool set was overridden
            return ActionResult(
                ok=False, observation=f"tool {call.name!r} has no toolbox", error="no_toolbox"
            )
        try:
            if call.name == "get_elements":
                query = call.arguments.get("query")
                if query is not None and not isinstance(query, str):
                    return ActionResult(
                        ok=False, observation="get_elements query must be a string",
                        error="invalid_query",
                    )
                listing = toolbox.get_elements(query if query else None)
                return ActionResult(ok=True, observation=listing or "no elements found")
            if call.name == "screenshot":
                path = toolbox.screenshot()
                return ActionResult(
                    ok=True,
                    observation=path or "no screenshot available — rely on get_elements",
                )
            request = self._act_request(call)
            if request is None:
                return ActionResult(
                    ok=False,
                    observation=(
                        "invalid act arguments — kind must be one of "
                        f"{', '.join(sorted(_ACTION_KINDS))} and rationale a string; "
                        "payload (when given) must be an object"
                    ),
                    error="invalid_act",
                )
            return toolbox.act(request)
        except Exception as exc:
            logger.exception("toolbox tool %s crashed", call.name)
            return ActionResult(
                ok=False, observation="the perception toolbox crashed", error=repr(exc)
            )

    @staticmethod
    def _acted_ref(call: LLMToolCall) -> str | None:
        """The target ref of an act call (verify tracking is per element ref)."""
        if call.name != "act":
            return None
        ref = call.arguments.get("target_ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
        return None

    @staticmethod
    def _act_request(call: LLMToolCall) -> ActionRequest | None:
        """Map the act tool call onto an ActionRequest (None when invalid).

        The declared risk is CARRIED on the request; the safety gate re-classifies
        it authoritatively from kind + element state (§3.13).
        """
        args = call.arguments
        kind = args.get("kind")
        if not isinstance(kind, str) or kind not in _ACTION_KINDS:
            return None
        rationale = args.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            return None
        ref = args.get("target_ref")
        if ref is not None and (not isinstance(ref, str) or not ref.strip()):
            return None
        payload = args.get("payload", {})
        if not isinstance(payload, dict):
            return None
        risk = args.get("risk")
        declared = risk if isinstance(risk, str) and risk in _RISK_LEVELS else "L1"
        return ActionRequest(
            kind=cast(ActionKind, kind),
            target_ref=ref.strip() if isinstance(ref, str) else None,
            payload=dict(payload),
            rationale=rationale.strip(),
            risk=cast(RiskLevel, declared),
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
        return None  # everything else is not an M2 executor action (UI tools ride the toolbox)

    @staticmethod
    def _observation_message(call: LLMToolCall, result: ActionResult) -> LLMMessage:
        if result.ok:
            outcome = result.observation
        else:
            # the OBSERVATION is the actionable text ("re-snapshot", "refused…");
            # the error code rides along as a suffix for programmatic matching
            outcome = f"ERROR: {result.observation}"
            if result.error:
                outcome += f" (error: {result.error})"
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
