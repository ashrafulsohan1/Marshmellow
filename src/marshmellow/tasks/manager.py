"""TaskManager — one coroutine per task (01-architecture §3.12).

The planner runs on a worker thread (blocking work never touches a loop),
every action passes through the executor seam (the future home of the safety
gate), and the full TaskEvent trace lands on the EventBus. Running turns are
tracked so the kill switch (M2 Task 8: "stop everything", §3.13) can cancel
them; a cancelled turn emits ``cancelled`` and never a result. Cancellation
also engages a per-task kill flag that the executor seam checks BEFORE every
action: cancelling the coroutine alone cannot stop the planner worker thread
(``asyncio.to_thread`` is not interruptible), so without the flag a mid-turn
kill would keep executing real actions — the flag is what makes "aborts
actions instantly" true. The one action already past the check may complete;
everything after is refused.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    ContextSnapshot,
    Instruction,
    TaskEvent,
    TaskResult,
    TaskSpec,
    Transcript,
)

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]

TaskEventName = Literal["started", "step", "waiting", "lease_request", "result", "failed",
                        "cancelled"]


@dataclass
class TaskHandle:
    spec: TaskSpec
    result: TaskResult | None = None


class Planner(Protocol):
    def run(self, instruction: Instruction, execute: Executor) -> TaskResult: ...


class TaskManager:
    def __init__(self, bus: EventBus, planner: Planner, execute: Executor) -> None:
        self._bus = bus
        self._planner = planner
        self._execute = execute
        self._running: dict[str, asyncio.Task[TaskHandle]] = {}
        self._kills: dict[str, threading.Event] = {}
        self._current_id: str | None = None  # the most recently started turn

    def current_kill(self) -> threading.Event | None:
        """The ACTIVE turn's kill event (None when no turn is running).

        The §3.13 ask channel waits on this during an L3 confirmation: a kill
        during a 20 s spoken question must abort the ask within ~250 ms, and
        ``asyncio.to_thread`` is not interruptible — the event is the fast
        channel. M2's pre-action kill check in the executor seam remains the
        second net. With concurrent turns this is the most recent one (the
        voice loop runs a single turn).
        """
        if self._current_id is None:
            return None
        return self._kills.get(self._current_id)

    async def submit(self, spec: TaskSpec) -> TaskHandle:
        handle = TaskHandle(spec=spec)
        current = asyncio.current_task()
        kill = threading.Event()
        if current is not None:
            self._running[spec.id] = current
        self._kills[spec.id] = kill
        self._current_id = spec.id
        self._emit(spec.id, "started", spec.instruction)
        try:
            result = await asyncio.to_thread(
                self._planner.run, self._instruction(spec), self._executing(spec.id, kill)
            )
        except asyncio.CancelledError:
            # Kill switch / barge-in: the turn dies quietly — no result, no failure.
            # The flag makes the kill visible to the worker thread immediately, even
            # if this coroutine has not processed the cancellation yet.
            kill.set()
            logger.info("task %s cancelled", spec.id)
            self._emit(spec.id, "cancelled", spec.instruction)
            raise
        except Exception as exc:
            logger.exception("task %s crashed", spec.id)
            result = TaskResult(ok=False, summary_to_speak="Something went wrong.",
                                details=repr(exc))
            handle.result = result
            self._emit(spec.id, "failed", result.summary_to_speak)
            return handle
        finally:
            self._running.pop(spec.id, None)
            self._kills.pop(spec.id, None)
            if self._current_id == spec.id:
                self._current_id = None  # no active turn → current_kill() is None again
        handle.result = result
        self._emit(spec.id, "result" if result.ok else "failed", result.summary_to_speak)
        return handle

    def submit_text_background(self, instruction: str) -> asyncio.Task[TaskHandle]:
        """Fire-and-forget submission for the live loop (M2+)."""
        spec = TaskSpec(id=uuid.uuid4().hex[:8], instruction=instruction)
        return asyncio.get_running_loop().create_task(self.submit(spec))

    def cancel_all(self) -> None:
        """Kill switch (§3.13): cancel every running turn and stop its actions NOW."""
        for task_id, task in list(self._running.items()):
            kill = self._kills.get(task_id)
            if kill is not None:
                kill.set()  # before task.cancel(): visible to the worker thread instantly
            if not task.done():
                task.cancel()

    def cancel_task(self, task: asyncio.Task[TaskHandle]) -> None:
        """Cancel one running turn (barge-in / cancel intent) with an instant kill."""
        for task_id, running in list(self._running.items()):
            if running is task:
                kill = self._kills.get(task_id)
                if kill is not None:
                    kill.set()
                if not task.done():
                    task.cancel()
                return

    @staticmethod
    def _instruction(spec: TaskSpec) -> Instruction:
        return Instruction(
            text=spec.instruction,
            transcript=Transcript(text=spec.instruction, source="fixture"),
            context=ContextSnapshot(),
        )

    def _executing(self, task_id: str, kill: threading.Event) -> Executor:
        def run(req: ActionRequest) -> ActionResult:
            if kill.is_set():
                # The turn was killed: refuse instead of executing (and never emit
                # a step event for an action that did not run).
                return ActionResult(
                    ok=False, observation="cancelled: kill switch engaged",
                    error="cancelled",
                )
            self._emit(task_id, "step", f"{req.kind}: {req.rationale}")
            return self._execute(req)

        return run

    def _emit(self, task_id: str, event: TaskEventName, payload: str) -> None:
        self._bus.emit(TaskEvent(task_id=task_id, type=event, payload=payload))
