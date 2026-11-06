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
import threading
import time
from collections.abc import Callable

import pytest

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActionKind,
    ActionRequest,
    ActionResult,
    ContextSnapshot,
    Instruction,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    PerceptionHint,
    PerceptionSnapshot,
    ProviderCaps,
    TaskEvent,
    TaskResult,
    TaskSpec,
    Transcript,
)
from marshmellow.perception.registry import ElementRegistry
from marshmellow.planner.orchestrator import LLMPlanner
from marshmellow.planner.tools import ScreenToolbox
from marshmellow.safety.gate import Confirmer
from marshmellow.tasks.foreground import READ_KINDS, ForegroundLease
from marshmellow.tasks.manager import Executor, Planner, TaskManager

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


def _instruction(text: str) -> Instruction:
    return Instruction(
        text=text,
        transcript=Transcript(text=text, source="fixture"),
        context=ContextSnapshot(),
    )


def _lease(confirmer: Confirmer, mascot: FakeMascot, bus: EventBus) -> ForegroundLease:
    return ForegroundLease(bus=bus, confirm=confirmer, mascot=mascot)


def _lease_manager(
    bus: EventBus,
    planner: Planner,
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


# -- review defect 1: the act tool must not bypass the per-turn executor --------


class ScriptedLLM:
    """Fake LLMProvider: replays scripted LLMResponses (the tests/planner
    pattern, local copy — the tests directory is not a package)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(vision=False, tool_call=True, json_schema=False)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        return self._responses.pop(0)


def _act_then_finish() -> list[LLMResponse]:
    return [
        LLMResponse(
            tool_calls=[LLMToolCall(name="act", arguments={"kind": "click",
                                                      "rationale": PURPOSE})],
            stop_reason="tool_calls",
        ),
        LLMResponse(
            tool_calls=[LLMToolCall(
                name="finish",
                arguments={"ok": False, "summary_to_speak": "No consent, stopping."},
            )],
            stop_reason="tool_calls",
        ),
    ]


class _NoPerception:
    """`act` never snapshots; this stub fails loudly if any read tool fires."""

    def snapshot(
        self, hint: PerceptionHint | None = None, hwnd: int | None = None
    ) -> PerceptionSnapshot:
        raise AssertionError("the act path must not snapshot")


def _recording_executor() -> tuple[Executor, list[ActionRequest]]:
    executed: list[ActionRequest] = []

    def execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        return ActionResult(ok=True, observation=f"did {req.kind}")

    return execute, executed


def test_act_routes_through_the_per_turn_executor() -> None:
    """Defect (review): LLMPlanner routed the act tool to the toolbox's
    CONSTRUCTOR-TIME executor, bypassing the per-turn executor TaskManager
    hands run() — the only seam that carries the kill check and the D6 lease.
    An act must flow through the run's own executor, never the raw seam."""
    per_turn, per_turn_executed = _recording_executor()
    raw, raw_executed = _recording_executor()
    toolbox = ScreenToolbox(_NoPerception(), ElementRegistry(), raw)

    result = LLMPlanner(
        provider=ScriptedLLM(_act_then_finish()), toolbox=toolbox
    ).run(_instruction("click play"), per_turn)

    assert [r.kind for r in per_turn_executed] == ["click"]
    assert raw_executed == [], "the constructor-time seam must never see acts"
    assert result.ok is False  # the planner observed the (test) refusal path


def test_act_fails_closed_for_a_toolbox_without_executor_rebinding() -> None:
    """A duck-typed PlannerTools that cannot rebind (no with_executor) must not
    act through its constructor-time executor either: the orchestrator routes
    the act straight through the per-turn executor (D6, fail closed)."""
    per_turn, per_turn_executed = _recording_executor()
    raw, raw_executed = _recording_executor()

    class DuckToolbox:
        """PlannerTools without with_executor (like the review's StubToolbox)."""

        def __init__(self) -> None:
            self.acts: list[ActionRequest] = []

        def get_elements(self, query: str | None) -> str:
            return "no elements"

        def screenshot(self) -> str:
            return ""

        def act(self, req: ActionRequest) -> ActionResult:
            self.acts.append(req)
            return ActionResult(ok=True, observation=f"did {req.kind}")

    duck = DuckToolbox()
    result = LLMPlanner(
        provider=ScriptedLLM(_act_then_finish()), toolbox=duck
    ).run(_instruction("click play"), per_turn)

    assert [r.kind for r in per_turn_executed] == ["click"]
    assert duck.acts == [], "the raw toolbox act() must be bypassed, fail closed"
    assert raw_executed == []
    assert result.ok is False


async def test_background_llm_act_asks_for_the_lease() -> None:
    """End to end over the REAL orchestrator routing (the reviewer's bypass
    demo): a background_job whose LLM planner acts through ScreenToolbox must
    hit the lease enforcement — a "no" refuses the act (lease_denied), the raw
    executor never sees it, the mascot cycled attention → idle."""
    bus = EventBus()
    raw, raw_executed = _recording_executor()
    confirmer = ScriptedConfirmer(False)
    mascot = FakeMascot()
    lease = _lease(confirmer, mascot, bus)
    manager = TaskManager(
        bus=bus,
        planner=LLMPlanner(
            provider=ScriptedLLM(_act_then_finish()),
            toolbox=ScreenToolbox(_NoPerception(), ElementRegistry(), raw),
        ),
        execute=raw,
        lease=lease,
    )

    handle = manager.register(TaskSpec(id="llm1", instruction="click play",
                                       mode="background_job"))
    await _until(lambda: handle.state in ("completed", "failed"))

    assert raw_executed == [], "a refused lease must never reach the executor"
    assert confirmer.questions == [f"I need the screen to {PURPOSE} — OK?"]
    assert mascot.states == ["attention", "idle"]
    assert handle.result is not None and not handle.result.ok
    manager.stop()
    bus.stop()


# -- review defect 2: release() must never block on an in-flight consent --------


class _ParkedConfirmer:
    """Consent that parks mid-ask until the test opens the gate."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.entered = threading.Event()
        self.gate = threading.Event()

    def confirm(self, question: str) -> bool:
        self.entered.set()
        self.gate.wait(timeout=8.0)  # bounded: a red run must stay finite
        return self.answer


def test_release_never_blocks_during_another_consent() -> None:
    """Defect (review): release() took the lock that request() holds across the
    whole spoken ask — and release() runs on EVENT-LOOP threads (the turn's
    finally), freezing the voice/resident loop for the length of the ask."""
    bus = EventBus()
    confirmer = _ParkedConfirmer(answer=False)
    lease = ForegroundLease(bus=bus, confirm=confirmer)

    asker = threading.Thread(
        target=lambda: lease.request("asker", PURPOSE), daemon=True
    )
    asker.start()
    assert confirmer.entered.wait(timeout=5.0), "the consent question never went out"

    started = time.perf_counter()
    lease.release("bystander")  # must not wait behind the parked ask
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"release blocked {elapsed:.2f}s behind an in-flight consent"
    assert lease.held_by() is None
    confirmer.gate.set()
    asker.join(timeout=5.0)
    bus.stop()


def test_release_during_own_consent_prevents_the_grant() -> None:
    """Kill race: the turn is cancelled while its OWN consent question is up —
    the user's "yes" must not grant a lease to a turn that is already gone
    (a holder leak that would refuse every later request without asking)."""
    bus = EventBus()
    confirmer = _ParkedConfirmer(answer=True)
    mascot = FakeMascot()
    lease = ForegroundLease(bus=bus, confirm=confirmer, mascot=mascot)

    holder: list[bool] = []
    asker = threading.Thread(
        target=lambda: holder.append(lease.request("t1", PURPOSE)), daemon=True
    )
    asker.start()
    assert confirmer.entered.wait(timeout=5.0)
    lease.release("t1")  # the turn's finally, its own consent still in flight
    confirmer.gate.set()
    asker.join(timeout=5.0)

    assert holder == [False], "a dead turn must not be granted the lease"
    assert lease.held_by() is None
    assert mascot.states == ["attention", "idle"]
    bus.stop()


class _DispatchPlanner:
    """click on one instruction, wait on the other; records per instruction."""

    def __init__(self) -> None:
        self.results: dict[str, list[ActionResult]] = {}

    def run(self, instruction: Instruction, execute: Executor) -> TaskResult:
        kind: ActionKind = "click" if "click" in instruction.text else "wait"
        result = execute(ActionRequest(kind=kind, rationale=PURPOSE, risk="L1"))
        self.results.setdefault(instruction.text, []).append(result)
        return TaskResult(ok=result.ok, summary_to_speak="turn over")


async def test_turn_end_does_not_stall_behind_another_tasks_consent() -> None:
    """Defect (review), at the manager level: task B's turn finally called
    lease.release() on the RESIDENT loop while task A's spoken consent was in
    flight — the loop froze (monitor checks, control() stalls) until the ask
    resolved. B must complete while A's question is still pending."""
    bus = EventBus()
    confirmer = _ParkedConfirmer(answer=False)
    mascot = FakeMascot()
    executed: list[ActionRequest] = []
    planner = _DispatchPlanner()
    lease = _lease(confirmer, mascot, bus)
    manager = _lease_manager(bus, planner, executed, lease)

    handle_a = manager.register(TaskSpec(id="ta", instruction="click the button",
                                         mode="background_job"))
    await _until(lambda: confirmer.entered.is_set())  # A's consent is pending
    handle_b = manager.register(TaskSpec(id="tb", instruction="wait a moment",
                                         mode="background_job"))
    await _until(lambda: handle_b.state == "completed")  # …and B still finishes
    assert confirmer.gate.is_set() is False, "B finished while A's ask was pending"

    confirmer.gate.set()  # A's consent resolves: "no"
    await _until(lambda: handle_a.state in ("completed", "failed"))
    assert planner.results["click the button"][0].error == "lease_denied"
    assert [r.kind for r in executed] == ["wait"], "only B's read kind ran"
    assert lease.held_by() is None
    manager.stop()
    bus.stop()
