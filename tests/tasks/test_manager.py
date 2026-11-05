
import asyncio
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    Instruction,
    MonitorSpec,
    TaskEvent,
    TaskResult,
    TaskSpec,
)
from marshmellow.planner.fake import FakePlanner
from marshmellow.tasks.foreground import ForegroundLease
from marshmellow.tasks.manager import BACKGROUND_MODES, Executor, TaskManager
from marshmellow.tasks.monitors import CheckResult, MonitorCheck


class GrantedConfirmer:
    """D6 consent stub: the user always says yes (the lease ask itself is
    covered in test_foreground_lease.py)."""

    def confirm(self, question: str) -> bool:
        return True


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


# ---------------------------------------------------------------------------
# TaskManager v2 (M4 Task 2): long-lived loops, TaskState, control API
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 6, 12, 0, 0)
_URL = "http://example.com"


class FakeClock:
    """The injected clock seam: tests advance it, the manager only reads it."""

    def __init__(self) -> None:
        self._now = NOW

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingSleeper:
    """Instant sleeper seam: records every chunk; advances the FakeClock by
    ``advance_ratio * seconds`` (1.0 = waiting moves the clock by itself, 0.0 =
    only the test's advance() moves it, so nothing becomes due on its own)."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []
        self.advance_ratio = 1.0

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds * self.advance_ratio)


class ScriptedCheck:
    """Canned CheckResult per call (the last one repeats); records baselines seen."""

    def __init__(self, results: list[CheckResult]) -> None:
        self.results = results
        self.baselines: list[str | None] = []

    def check(self, baseline: str | None) -> CheckResult:
        self.baselines.append(baseline)
        return self.results[min(len(self.baselines) - 1, len(self.results) - 1)]


def _background_manager(
    bus: EventBus,
    executed: list[ActionRequest],
    checks: dict[str, ScriptedCheck],
    clock: FakeClock,
    sleeper: RecordingSleeper,
    lease: ForegroundLease | None = None,
) -> TaskManager:
    """Full seam set: FakeClock, recording sleeper, scripted checks, fixed rng=0.0."""

    def factory(spec: MonitorSpec) -> MonitorCheck:
        return checks[spec.target]

    def execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        return ActionResult(ok=True, observation="window appeared")

    return TaskManager(
        bus=bus,
        planner=FakePlanner(),
        execute=execute,
        clock=clock,
        rng=lambda: 0.0,
        sleeper=sleeper,
        check_factory=factory,
        lease=lease,
    )


def _monitor_spec(task_id: str, interval_s: int = 60) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        instruction="watch the page",
        mode="background_monitor",
        monitor=MonitorSpec(
            kind="url_diff", target=_URL, interval_s=interval_s, condition="the page changes"
        ),
    )


def _unchanged(value: str) -> CheckResult:
    return CheckResult(
        ok=True, value=value, changed=False, summary="the watched page is unchanged"
    )


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition was not reached in time")
        await asyncio.sleep(0.01)


async def test_monitor_fires_once_then_completes() -> None:
    """A 2nd-tick change triggers once: result event with the summary, then completed."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    check = ScriptedCheck(
        [
            _unchanged("v1"),
            CheckResult(ok=True, value="v2", changed=True, summary="the watched page changed"),
        ]
    )
    manager = _background_manager(bus, [], {_URL: check}, clock, sleeper)

    handle = manager.register(_monitor_spec("m1"))

    await _until(lambda: handle.state == "completed")

    assert handle.result is not None and handle.result.ok
    assert handle.result.summary_to_speak == "the watched page changed"
    assert check.baselines == [None, "v1"], "first check captured, second compared"
    assert [e.type for e in events if e.task_id == "m1"] == ["started", "result"]
    assert [e.payload for e in events if e.task_id == "m1"][-1] == "the watched page changed"
    assert sleeper.calls and all(s > 0 for s in sleeper.calls), "waiting uses the sleeper seam"
    manager.stop()
    bus.stop()


async def test_first_check_captures_baseline_without_triggering() -> None:
    """changed=True on the very FIRST check is ignored: it only captures the baseline."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    sleeper.advance_ratio = 0.0  # the test drives the clock: nothing becomes due on its own
    check = ScriptedCheck([CheckResult(ok=True, value="v1", changed=True, summary="changed!")])
    manager = _background_manager(bus, [], {_URL: check}, clock, sleeper)

    handle = manager.register(_monitor_spec("m1", interval_s=60))
    await _until(lambda: handle.state == "waiting" and handle.last_check is not None)

    assert handle.result is None
    assert not [e for e in events if e.task_id == "m1" and e.type == "result"]
    assert check.baselines == [None]

    clock.advance(4000)  # the first due instant passes: now the baseline is compared
    await _until(lambda: handle.state == "completed")

    assert check.baselines == [None, "v1"], "the captured value became the baseline"
    assert handle.result is not None and handle.result.summary_to_speak == "changed!"
    assert [e.type for e in events if e.task_id == "m1"] == ["started", "result"]
    manager.stop()
    bus.stop()


async def test_control_pause_resume_run_now() -> None:
    """pause skips the due check; resume recomputes next_due; run_now checks immediately."""
    bus = EventBus()
    executed: list[ActionRequest] = []
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    sleeper.advance_ratio = 0.0  # deterministic: nothing is due until the test says so
    check = ScriptedCheck([_unchanged("v1")])
    manager = _background_manager(bus, executed, {_URL: check}, clock, sleeper)

    handle = manager.register(_monitor_spec("m1", interval_s=60))
    await _until(lambda: handle.state == "waiting" and handle.last_check is not None)
    assert handle.next_due == handle.last_check + timedelta(seconds=54)  # rng 0.0 → 0.9·60

    assert manager.control("m1", "pause") is True
    assert handle.state == "paused"
    clock.advance(120)  # far past due — the paused loop must skip the check
    await asyncio.sleep(0.05)
    assert len(check.baselines) == 1, "a paused monitor must not check even when due"

    assert manager.control("m1", "run_now") is True
    await _until(lambda: len(check.baselines) == 2)

    await _until(lambda: handle.next_due is not None and clock() < handle.next_due)
    frozen = clock()  # the loop is parked (paused): the clock is frozen until resume
    assert manager.control("m1", "resume") is True
    assert handle.next_due == frozen + timedelta(seconds=54), "resume recomputes from the clock"

    sleeper.advance_ratio = 1.0  # let waiting move the clock again
    # An unchanged monitor keeps cycling once the clock moves, so the exact count
    # races past between polls: any check ≥3 is necessarily post-resume (there
    # were exactly 2 while paused), proving the recomputed interval elapsed.
    await _until(lambda: len(check.baselines) >= 3)

    assert manager.control("nope", "pause") is False
    manager.stop()
    bus.stop()


async def test_check_errors_wait_then_fail() -> None:
    """5 consecutive check errors: a waiting event each, then exactly one failed."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    check = ScriptedCheck(
        [
            CheckResult(
                ok=False, value="", changed=False,
                summary="page check failed: HTTP 500", error="page check failed: HTTP 500",
            )
        ]
    )
    manager = _background_manager(bus, [], {_URL: check}, clock, sleeper)

    handle = manager.register(_monitor_spec("m1", interval_s=60))
    await _until(lambda: handle.state == "failed")

    trace = [e.type for e in events if e.task_id == "m1"]
    assert trace == ["started", *(["waiting"] * 5), "failed"]
    assert sum(e.type == "failed" for e in events if e.task_id == "m1") == 1
    assert handle.error == "page check failed: HTTP 500"
    assert handle.result is None
    manager.stop()
    bus.stop()


async def test_cancel_all_stops_long_lived_loops() -> None:
    """§3.13: the kill switch reaches long-lived monitor loops, not just foreground turns."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    check = ScriptedCheck([_unchanged("v1")])
    manager = _background_manager(bus, [], {_URL: check}, clock, sleeper)

    handle = manager.register(_monitor_spec("m1", interval_s=3600))
    await _until(lambda: handle.state == "waiting")

    manager.cancel_all()
    await _until(lambda: handle.state == "cancelled")

    assert [e.type for e in events if e.task_id == "m1"][-1] == "cancelled"
    checks_at_kill = len(check.baselines)
    await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)
    assert len(check.baselines) == checks_at_kill, "no check may run after the kill"
    manager.stop()
    bus.stop()


async def test_list_tasks_filters_by_state() -> None:
    """list_tasks filters by TaskState; handle/task_mode resolve registered ids."""
    bus = EventBus()
    executed: list[ActionRequest] = []
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    checks = {
        "http://m1": ScriptedCheck(
            [
                _unchanged("v1"),
                CheckResult(ok=True, value="v2", changed=True, summary="changed"),
            ]
        ),
        "http://m2": ScriptedCheck([_unchanged("v1")]),
    }
    manager = _background_manager(bus, executed, checks, clock, sleeper)
    h1 = manager.register(
        TaskSpec(
            id="m1", instruction="watch m1", mode="background_monitor",
            monitor=MonitorSpec(kind="url_diff", target="http://m1", interval_s=60, condition="c"),
        )
    )
    h2 = manager.register(
        TaskSpec(
            id="m2", instruction="watch m2", mode="background_monitor",
            monitor=MonitorSpec(
                kind="url_diff", target="http://m2", interval_s=100000, condition="c"
            ),
        )
    )
    await _until(lambda: h1.state == "completed")
    await _until(lambda: h2.state == "waiting")

    assert {h.spec.id for h in manager.list_tasks()} == {"m1", "m2"}
    assert [h.spec.id for h in manager.list_tasks("completed")] == ["m1"]
    assert [h.spec.id for h in manager.list_tasks("waiting")] == ["m2"]
    assert manager.list_tasks("paused") == []
    assert manager.handle("m1") is h1
    assert manager.handle("nope") is None
    assert manager.task_mode("m1") == "background_monitor"
    assert manager.task_mode("nope") is None
    manager.stop()
    bus.stop()


async def test_scheduled_task_runs_at_due_time() -> None:
    """Past-due schedule → the planner turn runs exactly like submit; result on the handle."""
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    executed: list[ActionRequest] = []
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    # D6 (§3.12): a scheduled action touches the screen only through a granted
    # foreground lease — consent is granted here; the ask/refusal flow itself
    # is covered in test_foreground_lease.py.
    manager = _background_manager(
        bus, executed, {}, clock, sleeper,
        lease=ForegroundLease(bus=bus, confirm=GrantedConfirmer()),
    )

    schedule = clock() + timedelta(seconds=90)
    handle = manager.register(
        TaskSpec(id="s1", instruction="open notepad", mode="scheduled", schedule=schedule)
    )

    await _until(lambda: handle.result is not None)

    assert handle.state == "completed"
    assert handle.result is not None and handle.result.ok
    assert handle.result.summary_to_speak == "Opened notepad."
    assert handle.next_due == schedule
    assert executed and executed[0].payload["open_app"] == "notepad"
    assert [e.type for e in events if e.task_id == "s1"] == [
        "started", "lease_request", "step", "result",
    ]
    manager.stop()
    bus.stop()


def test_register_rejects_foreground_modes() -> None:
    """register() is for background task classes only; foreground modes are ValueError."""
    bus = EventBus()
    manager = TaskManager(
        bus=bus,
        planner=FakePlanner(),
        execute=lambda req: ActionResult(ok=True, observation="x"),
    )
    for mode in ("immediate_reply", "foreground"):
        with pytest.raises(ValueError, match="background"):
            manager.register(TaskSpec(id="f1", instruction="hi", mode=mode))
    assert BACKGROUND_MODES == frozenset({"background_monitor", "background_job", "scheduled"})
    bus.stop()


async def test_register_with_unparseable_monitor_target_raises_and_registers_nothing() -> None:
    """make_check's ValueError for a bad api_field target must surface SYNCHRONOUSLY
    from register() — never die inside the marshaled spawn coroutine and leave the
    handle a silent 'pending' zombie (review fix: the spawn future was dropped)."""
    bus = EventBus()
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    manager = TaskManager(  # default check_factory = the real make_check
        bus=bus,
        planner=FakePlanner(),
        execute=lambda req: ActionResult(ok=True, observation="x"),
        clock=clock,
        rng=lambda: 0.0,
        sleeper=sleeper,
    )
    bad = TaskSpec(
        id="bad",
        instruction="watch api",
        mode="background_monitor",
        monitor=MonitorSpec(
            kind="api_field", target="http://x/no-fragment", interval_s=60, condition="c"
        ),
    )

    with pytest.raises(ValueError, match="api_field"):
        manager.register(bad)

    assert manager.list_tasks() == [], "a failed registration must not register anything"
    manager.stop()
    bus.stop()
