"""Foreground lease — consented screen access for background tasks (D6, §3.12).

D6 DoD, test-enforced: background tasks never synthesize input without a
granted foreground lease. Enforcement lives in the TaskManager executor seam
BEFORE the safety gate, so a refused action never reaches the executor (and a
granted one still passes the unchanged gate). One consent attempt per turn;
the lease is auto-released in the turn's finally; READ_KINDS run leaseless;
lease=None fails closed (``lease_required``); foreground/immediate-reply
turns are unaffected.
"""

import asyncio
import time
from collections.abc import Callable

import pytest

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActionKind,
    ActionRequest,
    ActionResult,
    Instruction,
    TaskEvent,
    TaskResult,
    TaskSpec,
)
from marshmellow.tasks.foreground import READ_KINDS, ForegroundLease
from marshmellow.tasks.manager import Executor, TaskManager

PURPOSE = "update the slide deck"


class ScriptedConfirmer:
    """Canned confirm() answer; records every question it was asked."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.questions: list[str] = []

    def confirm(self, question: str) -> bool:
        self.questions.append(question)
        return self.answer


class FakeMascot:
    """Records the state sequence the lease drives."""

    def __init__(self) -> None:
        self.states: list[str] = []

    def set_state(self, state: str) -> None:
        self.states.append(state)


class ScriptedPlanner:
    """Issues each kind in order; records every ActionResult it observes."""

    def __init__(self, *kinds: ActionKind) -> None:
        self.kinds = list(kinds)
        self.results: list[ActionResult] = []

    def run(self, instruction: Instruction, execute: Executor) -> TaskResult:
        for kind in self.kinds:
            self.results.append(
                execute(ActionRequest(kind=kind, rationale=PURPOSE, risk="L1"))
            )
        return TaskResult(ok=all(r.ok for r in self.results), summary_to_speak="turn over")


def _lease(confirmer: ScriptedConfirmer, mascot: FakeMascot, bus: EventBus) -> ForegroundLease:
    return ForegroundLease(bus=bus, confirm=confirmer, mascot=mascot)


def _lease_manager(
    bus: EventBus,
    planner: ScriptedPlanner,
    executed: list[ActionRequest],
    lease: ForegroundLease | None,
) -> TaskManager:
    def execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        return ActionResult(ok=True, observation=f"did {req.kind}")

    return TaskManager(bus=bus, planner=planner, execute=execute, lease=lease)


def _job(task_id: str) -> TaskSpec:
    return TaskSpec(id=task_id, instruction="update the deck", mode="background_job")


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition was not reached in time")
        await asyncio.sleep(0.01)


async def test_background_action_without_lease_refused() -> None:
    """D6: a background_job acting on screen asks first; a "no" refuses the
    action with error=lease_denied — the executor never sees the request."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    confirmer = ScriptedConfirmer(False)
    mascot = FakeMascot()
    executed: list[ActionRequest] = []
    planner = ScriptedPlanner("click")
    lease = _lease(confirmer, mascot, bus)
    manager = _lease_manager(bus, planner, executed, lease)

    handle = manager.register(_job("t1"))
    await _until(lambda: handle.state in ("completed", "failed"))

    assert not executed, "a refused lease must never reach the executor"
    assert len(planner.results) == 1
    assert planner.results[0].ok is False
    assert planner.results[0].error == "lease_denied"
    assert confirmer.questions == [f"I need the screen to {PURPOSE} — OK?"]
    assert mascot.states == ["attention", "idle"], "attention while asking, idle on refusal"
    assert lease.held_by() is None
    assert [e.type for e in events if e.task_id == "t1"] == [
        "started", "lease_request", "failed",
    ]
    assert [e.payload for e in events if e.task_id == "t1" and e.type == "lease_request"] == [
        PURPOSE
    ]
    assert handle.result is not None and not handle.result.ok
    manager.stop()
    bus.stop()


async def test_lease_grant_lets_action_run() -> None:
    """A "yes" grants the turn: the action executes (the unchanged safety gate
    still sees it downstream), the mascot shows acting while held, and the
    lease is auto-released in the turn's finally."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    confirmer = ScriptedConfirmer(True)
    mascot = FakeMascot()
    executed: list[ActionRequest] = []
    planner = ScriptedPlanner("click")
    lease = _lease(confirmer, mascot, bus)
    manager = _lease_manager(bus, planner, executed, lease)

    handle = manager.register(_job("t2"))
    await _until(lambda: handle.state == "completed")

    assert [r.kind for r in executed] == ["click"]
    assert handle.result is not None and handle.result.ok
    assert lease.held_by() is None, "the turn's finally must give the screen back"
    assert mascot.states == ["attention", "acting", "idle"]
    assert len(confirmer.questions) == 1
    manager.stop()
    bus.stop()


async def test_denial_remembered_for_turn() -> None:
    """One consent attempt per turn: after a refusal, further non-read actions
    in the SAME turn refuse without re-asking (no repeated interrogation)."""
    bus = EventBus()
    confirmer = ScriptedConfirmer(False)
    mascot = FakeMascot()
    executed: list[ActionRequest] = []
    planner = ScriptedPlanner("click", "click")
    lease = _lease(confirmer, mascot, bus)
    manager = _lease_manager(bus, planner, executed, lease)

    handle = manager.register(_job("t3"))
    await _until(lambda: handle.state in ("completed", "failed"))

    assert len(confirmer.questions) == 1, "exactly one ask per turn"
    assert [r.error for r in planner.results] == ["lease_denied", "lease_denied"]
    assert not executed
    assert mascot.states == ["attention", "idle"], "no re-ask flicker"
    manager.stop()
    bus.stop()


async def test_read_actions_need_no_lease() -> None:
    """READ_KINDS (wait/ask_user/done/fail/create_task) are the §3.12 D6
    leaseless executor actions: a background turn running them never asks.
    (Monitors never run planner actions at all; a background_job exercises
    the read-kind exemption through the same enforcement seam.)"""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    confirmer = ScriptedConfirmer(False)  # would refuse — must never be consulted
    mascot = FakeMascot()
    executed: list[ActionRequest] = []
    planner = ScriptedPlanner("wait", "done")
    lease = _lease(confirmer, mascot, bus)
    manager = _lease_manager(bus, planner, executed, lease)

    handle = manager.register(_job("t4"))
    await _until(lambda: handle.state == "completed")

    assert [r.kind for r in executed] == ["wait", "done"]
    assert confirmer.questions == []
    assert not [e for e in events if e.type == "lease_request"]
    assert mascot.states == []
    manager.stop()
    bus.stop()


async def test_lease_none_fails_closed() -> None:
    """No lease wired + a background non-read action → error=lease_required
    (fail closed: never execute, never synthesize input)."""
    bus = EventBus()
    executed: list[ActionRequest] = []
    planner = ScriptedPlanner("click")
    manager = _lease_manager(bus, planner, executed, lease=None)

    handle = manager.register(_job("t5"))
    await _until(lambda: handle.state in ("completed", "failed"))

    assert not executed
    assert len(planner.results) == 1
    assert planner.results[0].ok is False
    assert planner.results[0].error == "lease_required"
    manager.stop()
    bus.stop()


async def test_foreground_unaffected() -> None:
    """Foreground/immediate-reply turns own the screen by definition: they run
    without a lease (lease=None) and never trigger the lease flow when one is
    wired."""
    bus = EventBus()
    executed: list[ActionRequest] = []
    planner = ScriptedPlanner("click")
    manager = _lease_manager(bus, planner, executed, lease=None)

    handle = await manager.submit(TaskSpec(id="f1", instruction="click the button"))
    assert [r.kind for r in executed] == ["click"]
    assert handle.result is not None and handle.result.ok

    confirmer = ScriptedConfirmer(False)
    mascot = FakeMascot()
    manager2 = _lease_manager(
        bus, ScriptedPlanner("click"), [], _lease(confirmer, mascot, bus)
    )
    await manager2.submit(TaskSpec(id="f2", instruction="click the button"))
    assert confirmer.questions == [], "a foreground turn never asks for its own screen"
    assert mascot.states == []
    bus.stop()


def test_read_kinds_contract() -> None:
    """The D6 leaseless set, pinned: these never synthesize input."""
    assert READ_KINDS == frozenset({"wait", "ask_user", "done", "fail", "create_task"})
