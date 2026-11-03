"""TaskManager — M1: the foreground path only (01-architecture §3.12).

One coroutine per submitted task: planner runs (blocking work goes to a
thread), every action passes through the executor seam (the future home of
the safety gate), and the full TaskEvent trace lands on the EventBus.
"""

from __future__ import annotations

import asyncio
import logging
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

    async def submit(self, spec: TaskSpec) -> TaskHandle:
        handle = TaskHandle(spec=spec)
        self._emit(spec.id, "started", spec.instruction)
        try:
            result = await asyncio.to_thread(
                self._planner.run, self._instruction(spec), self._executing(spec.id)
            )
        except Exception as exc:
            logger.exception("task %s crashed", spec.id)
            result = TaskResult(ok=False, summary_to_speak="Something went wrong.",
                                details=repr(exc))
            handle.result = result
            self._emit(spec.id, "failed", result.summary_to_speak)
            return handle
        handle.result = result
        self._emit(spec.id, "result" if result.ok else "failed", result.summary_to_speak)
        return handle

    def submit_text_background(self, instruction: str) -> asyncio.Task[TaskHandle]:
        """Fire-and-forget submission for the live loop (M2+)."""
        spec = TaskSpec(id=uuid.uuid4().hex[:8], instruction=instruction)
        return asyncio.get_running_loop().create_task(self.submit(spec))

    @staticmethod
    def _instruction(spec: TaskSpec) -> Instruction:
        return Instruction(
            text=spec.instruction,
            transcript=Transcript(text=spec.instruction, source="fixture"),
            context=ContextSnapshot(),
        )

    def _executing(self, task_id: str) -> Executor:
        def run(req: ActionRequest) -> ActionResult:
            self._emit(task_id, "step", f"{req.kind}: {req.rationale}")
            return self._execute(req)

        return run

    def _emit(self, task_id: str, event: TaskEventName, payload: str) -> None:
        self._bus.emit(TaskEvent(task_id=task_id, type=event, payload=payload))
