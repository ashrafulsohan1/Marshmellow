
import asyncio
import threading
import time

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    Instruction,
    TaskEvent,
    TaskResult,
    TaskSpec,
)
from marshmellow.planner.fake import FakePlanner
from marshmellow.tasks.manager import Executor, TaskManager


def _manager(bus: EventBus, executed: list[ActionRequest], ok: bool = True) -> TaskManager:
    def execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        return ActionResult(ok=ok, observation="window appeared" if ok else "no such app")

    return TaskManager(bus=bus, planner=FakePlanner(), execute=execute)


async def test_submit_runs_planner_and_emits_event_trace() -> None:
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    executed: list[ActionRequest] = []
    manager = _manager(bus, executed)

    handle = await manager.submit(TaskSpec(id="t1", instruction="open notepad"))

    assert handle.result is not None and handle.result.ok
    assert executed and executed[0].payload["open_app"] == "notepad"
    assert [e.type for e in events if e.task_id == "t1"] == ["started", "step", "result"]
    bus.stop()


async def test_failed_task_emits_failed_event_and_speaks_failure() -> None:
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    manager = _manager(bus, executed=[], ok=False)

    handle = await manager.submit(TaskSpec(id="t2", instruction="open zappt"))

    assert handle.result is not None and not handle.result.ok
    assert [e.type for e in events if e.task_id == "t2"] == ["started", "step", "failed"]
    bus.stop()


async def test_chat_instruction_has_no_step_event() -> None:
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    manager = _manager(bus, executed=[])

    handle = await manager.submit(TaskSpec(id="t3", instruction="tell me a joke"))

    assert handle.result is not None and handle.result.ok
    assert [e.type for e in events if e.task_id == "t3"] == ["started", "result"]
    bus.stop()


class RepeatPlanner:
    """Acts over and over — the shape of a real LLM tool loop mid-turn."""

    def run(self, instruction: Instruction, execute: Executor) -> TaskResult:
        for i in range(100):
            outcome = execute(
                ActionRequest(
                    kind="shell", payload={"open_app": f"app{i}"}, rationale="loop", risk="L1"
                )
            )
            if not outcome.ok:
                break  # a real planner reacts to a refused action by winding down
        return TaskResult(ok=True, summary_to_speak="done")


async def test_cancel_all_stops_in_flight_actions() -> None:
    """§3.13: the kill switch aborts ACTIONS, not just the awaiting coroutine —
    cancelling cannot interrupt the planner worker thread, so the executor seam
    must refuse everything after the kill (no more actions, no step events)."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    executed: list[str] = []

    def execute(req: ActionRequest) -> ActionResult:
        time.sleep(0.01)  # real actions take time
        executed.append(str(req.payload.get("open_app")))
        return ActionResult(ok=True, observation=f"did {req.payload.get('open_app')}")

    manager = TaskManager(bus=bus, planner=RepeatPlanner(), execute=execute)
    task = manager.submit_text_background("open many apps")
    await asyncio.sleep(0.05)  # the turn is mid-loop
    assert executed, "the planner must be acting before the kill"

    manager.cancel_all()
    await asyncio.gather(task, return_exceptions=True)  # cancelled: expect CancelledError

    # The one action already past the kill check may complete (a worker thread
    # is uninterruptible); everything after must be refused — wait for the
    # worker to quiesce (two consecutive quiet readings), then prove it stays put.
    executed_at_kill = len(executed)
    stable = 0
    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(executed) == executed_at_kill:
            stable += 1
            if stable >= 2:
                break
        else:
            executed_at_kill = len(executed)
            stable = 0
    await asyncio.sleep(0.05)

    assert len(executed) == executed_at_kill, "no action may run after the kill"
    killed = [e.task_id for e in events if e.type == "cancelled"]
    assert killed, "the kill must emit 'cancelled'"
    trace = [e.type for e in events if e.task_id == killed[0]]
    assert trace[-1] == "cancelled"
    assert "step" not in trace[trace.index("cancelled") + 1:], (
        "no step events may follow the cancelled event"
    )
    bus.stop()


async def test_current_kill_exposes_the_active_turns_kill_event() -> None:
    """Task 4 (§3.13 ask channel): the L3 confirmation wait needs the active
    turn's kill event so a kill during a 20 s ask aborts within ~250 ms."""
    bus = EventBus()

    class BlockingPlanner:
        def __init__(self) -> None:
            self.seen_kill: threading.Event | None = None
            self.started = threading.Event()
            self.release = threading.Event()

        def run(self, instruction: Instruction, execute: Executor) -> TaskResult:
            self.seen_kill = manager.current_kill()
            self.started.set()
            self.release.wait(timeout=5.0)
            return TaskResult(ok=True, summary_to_speak="done")

    planner = BlockingPlanner()
    manager = TaskManager(
        bus=bus,
        planner=planner,
        execute=lambda req: ActionResult(ok=True, observation="did it"),
    )
    assert manager.current_kill() is None, "no active turn → no kill event"

    task = manager.submit_text_background("wait a moment")
    # Let the loop start the turn (blocking .wait() here would starve it): poll
    # with awaits until the planner is running, condition-bounded, never a sleep.
    deadline = time.monotonic() + 5.0
    while not planner.started.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert planner.started.is_set(), "the turn must be running before the kill"
    kill = manager.current_kill()
    assert kill is not None and kill is planner.seen_kill, "the turn's own kill event"

    manager.cancel_all()  # the kill switch sets the active turn's kill synchronously
    assert kill.is_set()
    planner.release.set()
    await asyncio.gather(task, return_exceptions=True)

    assert manager.current_kill() is None, "turn over → no active kill event"
    bus.stop()
